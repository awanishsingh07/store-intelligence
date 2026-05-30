import logging
from datetime import datetime, timezone

from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.event import Event
from app.schemas.events import StoreMetrics, ZoneDwellStat

logger = logging.getLogger(__name__)
settings = get_settings()

_ENTRY          = "ENTRY"
_ZONE_DWELL     = "ZONE_DWELL"
_BILLING_JOIN   = "BILLING_QUEUE_JOIN"
_BILLING_ABANDON = "BILLING_QUEUE_ABANDON"


def _today_window() -> tuple[datetime, datetime]:
    now = datetime.now(tz=timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


def _customer_filter(window_start, window_end, store_id):
    return [
        Event.store_id == store_id,
        Event.timestamp >= window_start,
        Event.timestamp <= window_end,
        Event.is_staff.is_(False),
    ]


async def _unique_visitors(db, store_id, window_start, window_end) -> int:
    result = await db.execute(
        select(func.count(distinct(Event.visitor_id))).where(
            *_customer_filter(window_start, window_end, store_id),
            Event.event_type == _ENTRY,
        )
    )
    return result.scalar_one() or 0


async def _conversion_rate(db, store_id, window_start, window_end, unique_visitors) -> float:
    if unique_visitors == 0:
        return 0.0

    joined_result = await db.execute(
        select(distinct(Event.visitor_id)).where(
            *_customer_filter(window_start, window_end, store_id),
            Event.event_type == _BILLING_JOIN,
        )
    )
    joined = {row[0] for row in joined_result.fetchall()}
    if not joined:
        return 0.0

    abandoned_result = await db.execute(
        select(distinct(Event.visitor_id)).where(
            *_customer_filter(window_start, window_end, store_id),
            Event.event_type == _BILLING_ABANDON,
        )
    )
    abandoned = {row[0] for row in abandoned_result.fetchall()}

    converted = len(joined - abandoned)
    return max(0.0, min(1.0, converted / unique_visitors))


async def _avg_dwell_per_zone(db, store_id, window_start, window_end) -> list[ZoneDwellStat]:
    result = await db.execute(
        select(
            Event.zone_id,
            func.avg(Event.dwell_ms).label("avg_dwell_ms"),
            func.count(Event.event_id).label("visit_count"),
        )
        .where(
            *_customer_filter(window_start, window_end, store_id),
            Event.event_type == _ZONE_DWELL,
            Event.zone_id.is_not(None),
        )
        .group_by(Event.zone_id)
        .order_by(func.count(Event.event_id).desc())
    )
    return [
        ZoneDwellStat(
            zone_id=row.zone_id,
            avg_dwell_seconds=round((row.avg_dwell_ms or 0) / 1000, 2),
            visit_count=row.visit_count or 0,
        )
        for row in result.fetchall()
        if row.zone_id
    ]


async def _current_queue_depth(db, store_id, window_start, window_end) -> int:
    result = await db.execute(
        select(Event.queue_depth)
        .where(
            *_customer_filter(window_start, window_end, store_id),
            Event.event_type == _BILLING_JOIN,
            Event.queue_depth.is_not(None),
        )
        .order_by(Event.timestamp.desc())
        .limit(1)
    )
    value = result.scalar_one_or_none()
    return int(value) if value is not None else 0


async def _abandonment_rate(db, store_id, window_start, window_end) -> float:
    result = await db.execute(
        select(
            func.count(distinct(Event.visitor_id)).filter(
                Event.event_type == _BILLING_JOIN
            ).label("joined"),
            func.count(distinct(Event.visitor_id)).filter(
                Event.event_type == _BILLING_ABANDON
            ).label("abandoned"),
        ).where(*_customer_filter(window_start, window_end, store_id))
    )
    row = result.one()
    joined = row.joined or 0
    abandoned = row.abandoned or 0
    if joined == 0:
        return 0.0
    return max(0.0, min(1.0, abandoned / joined))


async def compute_metrics(db: AsyncSession, store_id: str) -> StoreMetrics:
    window_start, window_end = _today_window()

    logger.debug("computing_metrics", extra={
        "store_id": store_id,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    })

    unique_v    = await _unique_visitors(db, store_id, window_start, window_end)
    conversion  = await _conversion_rate(db, store_id, window_start, window_end, unique_v)
    dwell_stats = await _avg_dwell_per_zone(db, store_id, window_start, window_end)
    queue_depth = await _current_queue_depth(db, store_id, window_start, window_end)
    abandonment = await _abandonment_rate(db, store_id, window_start, window_end)

    logger.info("metrics_computed", extra={
        "store_id": store_id,
        "unique_visitors": unique_v,
        "conversion_rate": round(conversion, 4),
        "zones_with_dwell": len(dwell_stats),
        "queue_depth": queue_depth,
        "abandonment_rate": round(abandonment, 4),
    })

    return StoreMetrics(
        store_id=store_id,
        unique_visitors=unique_v,
        conversion_rate=round(conversion, 4),
        avg_dwell_per_zone=dwell_stats,
        current_queue_depth=queue_depth,
        abandonment_rate=round(abandonment, 4),
        window_start=window_start,
        window_end=window_end,
    )