import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.services.metrics import compute_metrics
from app.services.funnel import compute_funnel
from app.schemas.events import (
    Anomaly,
    StoreAnomalies,
    StoreFunnel,
    FunnelStage,
    StoreHeatmap,
    StoreMetrics,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stores/{store_id}", tags=["stores"])
settings = get_settings()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@router.get(
    "/metrics",
    response_model=StoreMetrics,
    summary="Real-time store metrics",
    description=(
        "Returns unique visitors, conversion rate, average dwell per zone, "
        "current queue depth, and abandonment rate. "
        "Staff events (is_staff=true) are excluded. "
        "Always real-time — not cached."
    ),
)
async def get_metrics(
    store_id: Annotated[str, Path(description="Store identifier, e.g. STORE_BLR_002")],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoreMetrics:
    
    return await compute_metrics(db, store_id)


@router.get(
    "/funnel",
    response_model=StoreFunnel,
    summary="Conversion funnel",
    description=(
        "Entry → Zone Visit → Billing Queue → Purchase with counts and drop-off %. "
        "Session is the unit, not raw events. "
        "Re-entries do not double-count a visitor."
    ),
)
async def get_funnel(
    store_id: Annotated[str, Path()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoreFunnel:
    
    return await compute_funnel(db, store_id)

@router.get(
    "/heatmap",
    response_model=StoreHeatmap,
    summary="Zone visit heatmap",
    description=(
        "Zone visit frequency and average dwell, normalised 0–100 for grid rendering. "
        "data_confidence=false when fewer than 20 sessions contributed to a zone."
    ),
)
async def get_heatmap(
    store_id: Annotated[str, Path()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoreHeatmap:
    """
    Business logic: aggregate ZONE_ENTER counts + dwell_ms per zone,
    normalise visit frequency to 0-100 range.
    """
    # --- Business logic placeholder ---
    # TODO: from app.services.heatmap import compute_heatmap
    # return await compute_heatmap(db, store_id)

    now = _now()
    return StoreHeatmap(
        store_id=store_id,
        zones=[],
        window_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
        window_end=now,
    )


@router.get(
    "/anomalies",
    response_model=StoreAnomalies,
    summary="Active anomalies",
    description=(
        "Detects: BILLING_QUEUE_SPIKE, CONVERSION_DROP vs 7-day baseline, "
        "DEAD_ZONE (no visits in 30 min). "
        "Each anomaly has severity (INFO/WARN/CRITICAL) and suggested_action."
    ),
)
async def get_anomalies(
    store_id: Annotated[str, Path()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoreAnomalies:
    """
    Business logic: query last events per zone for dead zone detection,
    compute live conversion rate and compare to store_baselines table.
    """
    # --- Business logic placeholder ---
    # TODO: from app.services.anomalies import detect_anomalies
    # return await detect_anomalies(db, store_id)

    return StoreAnomalies(
        store_id=store_id,
        anomalies=[],
        checked_at=_now(),
    )
