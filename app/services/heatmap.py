import logging
from datetime import datetime, timezone

from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.event import Event
from app.schemas.events import StoreHeatmap, ZoneHeatmapEntry

logger = logging.getLogger(__name__)
settings = get_settings()

_ZONE_ENTER = "ZONE_ENTER"
_ZONE_DWELL = "ZONE_DWELL"


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
        Event.zone_id.is_not(None),
    ]


async def compute_heatmap(db: AsyncSession, store_id: str) -> StoreHeatmap:
    window_start, window_end = _today_window()

    logger.debug(
        "computing_heatmap",
        extra={
            "store_id": store_id,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
    )

    # --- visit_count per zone: count of ZONE_ENTER events per zone ---
    enter_result = await db.execute(
        select(
            Event.zone_id,
            func.count(Event.event_id).label("visit_count"),
            func.count(distinct(Event.visitor_id)).label("unique_visitors"),
        )
        .where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _ZONE_ENTER,
        )
        .group_by(Event.zone_id)
    )
    enter_rows = {
        row.zone_id: {
            "visit_count": row.visit_count,
            "unique_visitors": row.unique_visitors,
        }
        for row in enter_result.fetchall()
    }

    # --- avg_dwell_seconds per zone: from ZONE_DWELL events ---
    dwell_result = await db.execute(
        select(
            Event.zone_id,
            func.avg(Event.dwell_ms).label("avg_dwell_ms"),
        )
        .where(
            *_base_filters(store_id, window_start, window_end),
            Event.event_type == _ZONE_DWELL,
        )
        .group_by(Event.zone_id)
    )
    dwell_rows = {
        row.zone_id: row.avg_dwell_ms or 0.0
        for row in dwell_result.fetchall()
    }

    # Merge: all zones that appear in either query
    all_zone_ids = set(enter_rows.keys()) | set(dwell_rows.keys())

    if not all_zone_ids:
        logger.info("heatmap_no_data", extra={"store_id": store_id})
        return StoreHeatmap(
            store_id=store_id,
            zones=[],
            window_start=window_start,
            window_end=window_end,
        )

    # Build intermediate records before normalisation
    zone_records: list[dict] = []
    for zone_id in all_zone_ids:
        enter_data = enter_rows.get(zone_id, {"visit_count": 0, "unique_visitors": 0})
        avg_dwell_ms = dwell_rows.get(zone_id, 0.0)
        zone_records.append(
            {
                "zone_id": zone_id,
                "visit_count": enter_data["visit_count"],
                "unique_visitors": enter_data["unique_visitors"],
                "avg_dwell_seconds": round(avg_dwell_ms / 1000.0, 2),
            }
        )

    # Normalise visit_count to 0-100: highest zone = 100.0
    max_visits = max(z["visit_count"] for z in zone_records)

    zones: list[ZoneHeatmapEntry] = []
    for z in sorted(zone_records, key=lambda r: r["visit_count"], reverse=True):
        if max_visits > 0:
            normalised = round((z["visit_count"] / max_visits) * 100.0, 2)
        else:
            normalised = 0.0

        # data_confidence: False when fewer than heatmap_min_sessions unique
        # visitors contributed to this zone's data
        data_confidence = z["unique_visitors"] >= settings.heatmap_min_sessions

        zones.append(
            ZoneHeatmapEntry(
                zone_id=z["zone_id"],
                visit_frequency_normalised=normalised,
                avg_dwell_seconds=z["avg_dwell_seconds"],
                data_confidence=data_confidence,
            )
        )

    logger.info(
        "heatmap_computed",
        extra={
            "store_id": store_id,
            "zone_count": len(zones),
            "max_visits": max_visits,
            "low_confidence_zones": sum(1 for z in zones if not z.data_confidence),
        },
    )

    return StoreHeatmap(
        store_id=store_id,
        zones=zones,
        window_start=window_start,
        window_end=window_end,
    )