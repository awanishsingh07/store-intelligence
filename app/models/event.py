from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Event(Base):
    """
    Persisted store event.

    Mirrors the required event schema from the challenge spec.
    PRIMARY KEY on event_id makes POST /events/ingest naturally idempotent
    (INSERT OR IGNORE / on_conflict_do_nothing).
    """

    __tablename__ = "events"

    # --- Identity ---
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    visitor_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # --- Classification ---
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    is_staff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # --- Timing ---
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    dwell_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Location ---
    zone_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # --- Metadata (flattened from nested metadata object) ---
    queue_depth: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sku_zone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    session_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # --- Record housekeeping ---
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )

    __table_args__ = (
        # Fast queries for: metrics (store + time), funnel (store + visitor),
        # heatmap (store + zone), anomalies (store + zone + time)
        Index("ix_events_store_timestamp", "store_id", "timestamp"),
        Index("ix_events_store_visitor", "store_id", "visitor_id"),
        Index("ix_events_store_zone_timestamp", "store_id", "zone_id", "timestamp"),
        Index("ix_events_store_type_timestamp", "store_id", "event_type", "timestamp"),
    )

    def __repr__(self) -> str:
        return (
            f"<Event {self.event_type} store={self.store_id} "
            f"visitor={self.visitor_id} ts={self.timestamp}>"
        )
