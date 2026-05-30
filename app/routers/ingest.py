import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.schemas.events import EventIn, IngestResponse

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


@router.post(
    "/events/ingest",
    response_model=IngestResponse,
    summary="Ingest a batch of store events",
    description=(
        "Accepts up to 500 events per batch. "
        "Idempotent by event_id — duplicate events are silently ignored. "
        "Returns partial success: malformed events are rejected with reasons, "
        "valid events are accepted."
    ),
)
async def ingest_events(
    request: Request,
    events: list[EventIn],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IngestResponse:
    """
    Ingest endpoint.
    Business logic (dedup, persistence) will be added in the ingestion service.
    Stub returns accepted=len(events), rejected=0 until then.
    """
    if len(events) > settings.ingest_max_batch_size:
        # Trim silently and log — will be enforced properly in business logic
        logger.warning(
            "batch_size_exceeded",
            extra={"received": len(events), "limit": settings.ingest_max_batch_size},
        )
        events = events[: settings.ingest_max_batch_size]

    # Expose event_count for the logging middleware
    request.state.event_count = len(events)

    # --- Business logic placeholder ---
    # TODO: call ingestion service to dedup + persist
    from app.services.ingestion import ingest_batch
    return await ingest_batch(db, events)
