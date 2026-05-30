import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.schemas.events import EventIn, IngestError, IngestResponse

logger = logging.getLogger(__name__)


def _event_to_row(event: EventIn) -> dict:
    """
    Map a validated EventIn onto a flat dict matching the Event ORM columns.
    Flattens the nested metadata object — queue_depth, sku_zone, session_seq
    are stored as top-level columns.
    """
    meta = event.metadata or {}
    if hasattr(meta, "queue_depth"):
        queue_depth = meta.queue_depth
        sku_zone = meta.sku_zone
        session_seq = meta.session_seq
    else:
        queue_depth = meta.get("queue_depth") if isinstance(meta, dict) else None
        sku_zone = meta.get("sku_zone") if isinstance(meta, dict) else None
        session_seq = meta.get("session_seq") if isinstance(meta, dict) else None

    return {
        "event_id": str(event.event_id),
        "store_id": event.store_id,
        "camera_id": event.camera_id,
        "visitor_id": event.visitor_id,
        "event_type": event.event_type.value,
        "is_staff": event.is_staff,
        "confidence": event.confidence,
        "timestamp": event.timestamp,
        "dwell_ms": event.dwell_ms,
        "zone_id": event.zone_id,
        "queue_depth": queue_depth,
        "sku_zone": sku_zone,
        "session_seq": session_seq,
        "ingested_at": datetime.now(tz=timezone.utc),
    }


async def ingest_batch(
    db: AsyncSession,
    events: list[EventIn],
) -> IngestResponse:
    """
    Persist a batch of validated events.

    Idempotency: uses SQLite's INSERT OR IGNORE semantics via
    `insert().prefix_with("OR IGNORE")`. An event whose event_id already
    exists in the table is silently skipped — not an error. This makes the
    endpoint safe to call twice with the same payload (Part C requirement).

    Partial success: each event is attempted individually so that one bad
    row (e.g. a DB constraint beyond the PK) does not roll back the entire
    batch. Valid events are committed; failed events are collected into the
    errors list with their event_id and reason.

    Returns an IngestResponse with accepted + rejected counts and structured
    error detail for every rejected event.
    """
    if not events:
        return IngestResponse(accepted=0, rejected=0, errors=[])

    accepted = 0
    errors: list[IngestError] = []

    for event in events:
        event_id_str = str(event.event_id)
        try:
            row = _event_to_row(event)

            # INSERT OR IGNORE: if event_id already exists, the row is skipped
            # and no error is raised. The affected rowcount will be 0 for dupes.
            stmt = sqlite_insert(Event).prefix_with("OR IGNORE").values(**row)
            result = await db.execute(stmt)
            await db.flush()

            if result.rowcount == 0:
                # Duplicate — already in DB. Idempotent: not an error,
                # but we do not count it as newly accepted.
                logger.debug(
                    "event_duplicate_skipped",
                    extra={"event_id": event_id_str, "store_id": event.store_id},
                )
            else:
                accepted += 1

        except SQLAlchemyError as exc:
            await db.rollback()
            reason = f"database error: {type(exc).__name__}"
            logger.warning(
                "event_ingest_failed",
                extra={
                    "event_id": event_id_str,
                    "store_id": event.store_id,
                    "reason": reason,
                    "error": str(exc),
                },
            )
            errors.append(IngestError(event_id=event_id_str, reason=reason))

        except Exception as exc:
            await db.rollback()
            reason = f"unexpected error: {type(exc).__name__}"
            logger.error(
                "event_ingest_unexpected_error",
                extra={
                    "event_id": event_id_str,
                    "store_id": event.store_id,
                    "reason": reason,
                    "error": str(exc),
                },
            )
            errors.append(IngestError(event_id=event_id_str, reason=reason))

    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(
            "ingest_commit_failed",
            extra={"accepted_before_commit": accepted, "error": str(exc)},
        )
        errors = [
            IngestError(
                event_id=str(e.event_id),
                reason=f"commit failed: {type(exc).__name__}",
            )
            for e in events
            if str(e.event_id) not in {err.event_id for err in errors}
        ]
        accepted = 0

    rejected = len(errors)

    logger.info(
        "ingest_batch_complete",
        extra={
            "total": len(events),
            "accepted": accepted,
            "rejected": rejected,
            "duplicate_skipped": len(events) - accepted - rejected,
        },
    )

    return IngestResponse(accepted=accepted, rejected=rejected, errors=errors)