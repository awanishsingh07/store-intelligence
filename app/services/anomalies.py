import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.event import Event
from app.models.store_baseline import StoreBaseline
from app.schemas.events import (
    Anomaly,
    AnomalySeverity,
    AnomalyType,
    StoreAnomalies,
)

logger = logging.getLogger(__name__)
settings = get_settings()

_ENTRY           = "ENTRY"
_REENTRY         = "REENTRY"
_ZONE_ENTER      = "ZONE_ENTER"
_BILLING_JOIN    = "BILLING_QUEUE_JOIN"
_BILLING_ABANDON = "BILLING_QUEUE_ABANDON"

# Queue depth at or above this value triggers WARN; double triggers CRITICAL
_QUEUE_SPIKE_WARN     = 5
_QUEUE_SPIKE_CRITICAL = 10

# Conversion rate must drop below this fraction of baseline to fire CONVERSION_DROP
_CONVERSION_DROP_THRESHOLD = 0.70


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _today_window() -> tuple[datetime, datetime]:
    now = _now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0), now


def _base_filters(store_id: str, window_start: datetime, window_end: datetime) -> list:
    return [
        Event.store_id == store_id,
        Event.timestamp >= window_start,
        Event.timestamp <= window_end,
        Event.is_staff.is_(False),
    ]


async def _detect_queue_spike(
    db: AsyncSession,
    store_id: str,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> Anomaly | None:
    """
    Fires when the most recent BILLING_QUEUE_JOIN event has queue_depth at or
    above the spike threshold. WARN at >= 5, CRITICAL at >= 10.
    Returns None when no billing queue events exist.
    """
    result = await db.execute(
        select(Event.queue_depth, Event.timestamp)
        .where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _BILLING_JOIN,
            Event.queue_depth.is_not(None),
        )
        .order_by(Event.timestamp.desc())
        .limit(1)
    )
    row = result.one_or_none()
    if row is None or row.queue_depth is None:
        return None

    depth = row.queue_depth

    if depth >= _QUEUE_SPIKE_CRITICAL:
        severity = AnomalySeverity.CRITICAL
    elif depth >= _QUEUE_SPIKE_WARN:
        severity = AnomalySeverity.WARN
    else:
        return None

    return Anomaly(
        anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
        severity=severity,
        description=(
            f"Billing queue depth is {depth} "
            f"(threshold: WARN={_QUEUE_SPIKE_WARN}, CRITICAL={_QUEUE_SPIKE_CRITICAL}). "
            f"Last observed at {row.timestamp.isoformat()}."
        ),
        suggested_action="Open an additional billing counter to reduce wait time.",
        detected_at=now,
        zone_id="BILLING",
        current_value=float(depth),
        baseline_value=float(_QUEUE_SPIKE_WARN),
    )


async def _detect_conversion_drop(
    db: AsyncSession,
    store_id: str,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> Anomaly | None:
    """
    Fires when live conversion rate drops below 70% of the store's baseline.
    Baseline is read from store_baselines table; falls back to settings constant
    if no row exists (store has never been seeded).
    Returns None when there are no visitors yet today (avoids false positives
    during the first few minutes of the day).
    """
    # --- Fetch baseline ---
    baseline_result = await db.execute(
        select(StoreBaseline.baseline_conversion_rate).where(
            StoreBaseline.store_id == store_id
        )
    )
    baseline_rate = baseline_result.scalar_one_or_none()
    if baseline_rate is None:
        baseline_rate = settings.baseline_conversion_rate

    # --- Compute live conversion rate ---
    unique_result = await db.execute(
        select(func.count(distinct(Event.visitor_id))).where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _ENTRY,
        )
    )
    unique_visitors = unique_result.scalar_one() or 0

    # Not enough data yet — suppress to avoid false positives
    if unique_visitors < 10:
        return None

    joined_result = await db.execute(
        select(distinct(Event.visitor_id)).where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _BILLING_JOIN,
        )
    )
    joined = {row[0] for row in joined_result.fetchall()}

    abandoned_result = await db.execute(
        select(distinct(Event.visitor_id)).where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _BILLING_ABANDON,
            Event.visitor_id.in_(joined) if joined else Event.visitor_id.is_(None),
        )
    )
    abandoned = {row[0] for row in abandoned_result.fetchall()}

    converted = len(joined - abandoned)
    live_rate = converted / unique_visitors

    drop_threshold = baseline_rate * _CONVERSION_DROP_THRESHOLD
    if live_rate >= drop_threshold:
        return None

    # Severity scales with depth of the drop
    drop_pct = ((baseline_rate - live_rate) / baseline_rate) * 100
    if drop_pct >= 50:
        severity = AnomalySeverity.CRITICAL
    elif drop_pct >= 30:
        severity = AnomalySeverity.WARN
    else:
        severity = AnomalySeverity.INFO

    return Anomaly(
        anomaly_type=AnomalyType.CONVERSION_DROP,
        severity=severity,
        description=(
            f"Live conversion rate {live_rate:.1%} is {drop_pct:.0f}% below "
            f"the 7-day baseline of {baseline_rate:.1%}."
        ),
        suggested_action=(
            "Review staff deployment and zone engagement. "
            "Check whether a high-traffic zone has low billing queue progression."
        ),
        detected_at=now,
        current_value=round(live_rate, 4),
        baseline_value=round(baseline_rate, 4),
    )


async def _detect_dead_zones(
    db: AsyncSession,
    store_id: str,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> list[Anomaly]:
    """
    Fires one DEAD_ZONE anomaly per zone that has had no ZONE_ENTER events
    in the past dead_zone_threshold_seconds (default 1800s / 30 min).
    Only evaluates zones that have seen at least one visit today — avoids
    flagging zones that simply haven't opened yet.
    """
    threshold_ts = now - timedelta(seconds=settings.dead_zone_threshold_seconds)

    # All zones that had any activity today
    active_today_result = await db.execute(
        select(Event.zone_id)
        .where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _ZONE_ENTER,
            Event.zone_id.is_not(None),
        )
        .group_by(Event.zone_id)
    )
    zones_active_today = {row[0] for row in active_today_result.fetchall()}

    if not zones_active_today:
        return []

    # Most recent ZONE_ENTER per zone
    last_visit_result = await db.execute(
        select(Event.zone_id, func.max(Event.timestamp).label("last_visit"))
        .where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _ZONE_ENTER,
            Event.zone_id.in_(zones_active_today),
        )
        .group_by(Event.zone_id)
    )

    anomalies: list[Anomaly] = []
    for row in last_visit_result.fetchall():
        last_visit = row.last_visit
        if last_visit is None:
            continue

        # SQLite returns naive datetimes — attach UTC timezone if missing
        if last_visit.tzinfo is None:
            last_visit = last_visit.replace(tzinfo=timezone.utc)

        if last_visit < threshold_ts:
            silent_minutes = int((now - last_visit).total_seconds() / 60)
            anomalies.append(
                Anomaly(
                    anomaly_type=AnomalyType.DEAD_ZONE,
                    severity=AnomalySeverity.WARN,
                    description=(
                        f"Zone '{row.zone_id}' has had no visitor activity "
                        f"for {silent_minutes} minutes "
                        f"(threshold: {settings.dead_zone_threshold_seconds // 60} min). "
                        f"Last visit at {last_visit.isoformat()}."
                    ),
                    suggested_action=(
                        f"Check camera feed for zone '{row.zone_id}'. "
                        "Consider re-engaging customers with a staff member or promotion."
                    ),
                    detected_at=now,
                    zone_id=row.zone_id,
                    current_value=float(silent_minutes),
                    baseline_value=float(settings.dead_zone_threshold_seconds // 60),
                )
            )

    return anomalies


async def _detect_stale_feed(
    db: AsyncSession,
    store_id: str,
    now: datetime,
) -> Anomaly | None:
    """
    Fires when the most recent event for this store is older than
    stale_feed_threshold_seconds (default 600s / 10 min).
    Distinct from /health STALE_FEED — here it appears as an operational
    anomaly in the store's anomaly list, not just the health endpoint.
    """
    result = await db.execute(
        select(func.max(Event.timestamp)).where(Event.store_id == store_id)
    )
    last_event = result.scalar_one_or_none()

    if last_event is None:
        return None  # No events at all — not a stale feed, just no data

    if last_event.tzinfo is None:
        last_event = last_event.replace(tzinfo=timezone.utc)

    lag_seconds = (now - last_event).total_seconds()
    if lag_seconds <= settings.stale_feed_threshold_seconds:
        return None

    lag_minutes = int(lag_seconds / 60)

    return Anomaly(
        anomaly_type=AnomalyType.STALE_FEED,
        severity=AnomalySeverity.CRITICAL,
        description=(
            f"No events received from store '{store_id}' for {lag_minutes} minutes. "
            f"Last event at {last_event.isoformat()}."
        ),
        suggested_action=(
            "Check CCTV pipeline process and network connectivity. "
            "Verify the detection pipeline is running against the correct store."
        ),
        detected_at=now,
        current_value=float(lag_minutes),
        baseline_value=float(settings.stale_feed_threshold_seconds / 60),
    )


async def detect_anomalies(db: AsyncSession, store_id: str) -> StoreAnomalies:
    now = _now()
    window_start, window_end = _today_window()
    anomalies: list[Anomaly] = []

    logger.debug("detecting_anomalies", extra={"store_id": store_id})

    queue_anomaly = await _detect_queue_spike(
        db, store_id, window_start, window_end, now
    )
    if queue_anomaly:
        anomalies.append(queue_anomaly)

    conversion_anomaly = await _detect_conversion_drop(
        db, store_id, window_start, window_end, now
    )
    if conversion_anomaly:
        anomalies.append(conversion_anomaly)

    dead_zone_anomalies = await _detect_dead_zones(
        db, store_id, window_start, window_end, now
    )
    anomalies.extend(dead_zone_anomalies)

    stale_anomaly = await _detect_stale_feed(db, store_id, now)
    if stale_anomaly:
        anomalies.append(stale_anomaly)

    logger.info(
        "anomalies_detected",
        extra={
            "store_id": store_id,
            "total": len(anomalies),
            "critical": sum(1 for a in anomalies if a.severity == AnomalySeverity.CRITICAL),
            "warn": sum(1 for a in anomalies if a.severity == AnomalySeverity.WARN),
            "info": sum(1 for a in anomalies if a.severity == AnomalySeverity.INFO),
        },
    )

    return StoreAnomalies(
        store_id=store_id,
        anomalies=anomalies,
        checked_at=now,
    )