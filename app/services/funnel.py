import logging
from datetime import datetime, timezone

from sqlalchemy import select, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.schemas.events import FunnelStage, StoreFunnel

logger = logging.getLogger(__name__)

_ENTRY           = "ENTRY"
_REENTRY         = "REENTRY"
_ZONE_ENTER      = "ZONE_ENTER"
_BILLING_JOIN    = "BILLING_QUEUE_JOIN"
_BILLING_ABANDON = "BILLING_QUEUE_ABANDON"


def _today_window() -> tuple[datetime, datetime]:
    now = datetime.now(tz=timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


def _base_filters(store_id: str, window_start: datetime, window_end: datetime) -> list:
    return [
        Event.store_id == store_id,
        Event.timestamp >= window_start,
        Event.timestamp <= window_end,
        Event.is_staff.is_(False),
    ]


async def _stage_entry(
    db: AsyncSession,
    store_id: str,
    window_start: datetime,
    window_end: datetime,
) -> set[str]:
    result = await db.execute(
        select(distinct(Event.visitor_id)).where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type.in_([_ENTRY, _REENTRY]),
        )
    )
    return {row[0] for row in result.fetchall()}


async def _stage_zone_visit(
    db: AsyncSession,
    store_id: str,
    window_start: datetime,
    window_end: datetime,
) -> set[str]:
    result = await db.execute(
        select(distinct(Event.visitor_id)).where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _ZONE_ENTER,
            Event.zone_id.is_not(None),
        )
    )
    return {row[0] for row in result.fetchall()}


async def _stage_billing(
    db: AsyncSession,
    store_id: str,
    window_start: datetime,
    window_end: datetime,
) -> set[str]:
    result = await db.execute(
        select(distinct(Event.visitor_id)).where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _BILLING_JOIN,
        )
    )
    return {row[0] for row in result.fetchall()}


async def _stage_purchase(
    db: AsyncSession,
    store_id: str,
    window_start: datetime,
    window_end: datetime,
    billing_visitors: set[str],
) -> set[str]:
    if not billing_visitors:
        return set()

    result = await db.execute(
        select(distinct(Event.visitor_id)).where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _BILLING_ABANDON,
            Event.visitor_id.in_(billing_visitors),
        )
    )
    abandoned = {row[0] for row in result.fetchall()}
    return billing_visitors - abandoned


def _drop_off_pct(current: int, previous: int) -> float:
    if previous == 0:
        return 0.0
    return round(max(0.0, min(100.0, ((previous - current) / previous) * 100.0)), 2)


async def compute_funnel(db: AsyncSession, store_id: str) -> StoreFunnel:
    window_start, window_end = _today_window()

    logger.debug("computing_funnel", extra={
        "store_id": store_id,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    })

    entry_visitors    = await _stage_entry(db, store_id, window_start, window_end)
    zone_visitors     = await _stage_zone_visit(db, store_id, window_start, window_end)
    billing_visitors  = await _stage_billing(db, store_id, window_start, window_end)
    purchase_visitors = await _stage_purchase(
        db, store_id, window_start, window_end, billing_visitors
    )

    n_entry    = len(entry_visitors)
    n_zone     = len(zone_visitors)
    n_billing  = len(billing_visitors)
    n_purchase = len(purchase_visitors)

    logger.info("funnel_computed", extra={
        "store_id": store_id,
        "entry": n_entry,
        "zone_visit": n_zone,
        "billing_queue": n_billing,
        "purchase": n_purchase,
    })

    return StoreFunnel(
        store_id=store_id,
        stages=[
            FunnelStage(stage="ENTRY",         count=n_entry,    drop_off_pct=0.0),
            FunnelStage(stage="ZONE_VISIT",    count=n_zone,     drop_off_pct=_drop_off_pct(n_zone,     n_entry)),
            FunnelStage(stage="BILLING_QUEUE", count=n_billing,  drop_off_pct=_drop_off_pct(n_billing,  n_zone)),
            FunnelStage(stage="PURCHASE",      count=n_purchase, drop_off_pct=_drop_off_pct(n_purchase, n_billing)),
        ],
        total_sessions=n_entry,
        window_start=window_start,
        window_end=window_end,
    )