import logging
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings, Settings
from app.schemas.events import HealthResponse, StoreFeedStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])
settings = get_settings()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    description=(
        "Returns database connectivity, last event timestamp per store, "
        "and STALE_FEED warning if any store's last event is older than 10 minutes. "
        "This is the first endpoint an on-call engineer checks."
    ),
)
async def health_check(
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    """
    Real DB connectivity check on every call — never cached.
    Queries last event per store from the events table.
    """
    now = datetime.now(tz=timezone.utc)
    db_status = "connected"
    store_statuses: list[StoreFeedStatus] = []

    try:
        # Verify DB is reachable with a lightweight query
        await db.execute(text("SELECT 1"))

        # Query last event timestamp per store using ORM-safe approach
        from sqlalchemy import select, func
        from app.models.event import Event

        result = await db.execute(
            select(Event.store_id, func.max(Event.timestamp).label("last_event_at"))
            .group_by(Event.store_id)
            .order_by(Event.store_id)
        )
        rows = result.fetchall()

        stale_threshold = now - timedelta(
            seconds=settings.stale_feed_threshold_seconds
        )

        for store_id, last_event_at in rows:
            if last_event_at is None:
                status = "NO_DATA"
            else:
                if last_event_at.tzinfo is None:
                    last_event_at = last_event_at.replace(tzinfo=timezone.utc)
                    if last_event_at < stale_threshold:
                        status = "STALE_FEED"
                    else : 
                        status = "OK"

            store_statuses.append(
                StoreFeedStatus(
                    store_id=store_id,
                    last_event_at=last_event_at,
                    status=status,
                )
            )

    except Exception as exc:
        logger.error("health_check_db_error", extra={"error": str(exc)})
        db_status = "unavailable"

    overall_status = (
        "healthy"
        if db_status == "connected"
        and all(s.status == "OK" for s in store_statuses)
        else "degraded"
    )

    return HealthResponse(
        status=overall_status,
        version=settings.app_version,
        database=db_status,
        stores=store_statuses,
        checked_at=now,
    )
