from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Event type catalogue (from challenge spec)
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


# ---------------------------------------------------------------------------
# Inbound event schema
# ---------------------------------------------------------------------------

class EventMetadata(BaseModel):
    queue_depth: Optional[int] = Field(None, ge=0)
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = Field(None, ge=0)


class EventIn(BaseModel):
    """
    Inbound event — matches the required output schema from the detection pipeline.
    Validated on every POST /events/ingest call.
    """

    event_id: UUID = Field(..., description="UUID v4, globally unique")
    store_id: str = Field(..., min_length=1, max_length=64)
    camera_id: str = Field(..., min_length=1, max_length=64)
    visitor_id: str = Field(..., min_length=1, max_length=64)
    event_type: EventType
    timestamp: datetime = Field(..., description="ISO-8601 UTC")
    zone_id: Optional[str] = Field(None, max_length=64)
    dwell_ms: int = Field(0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: Optional[EventMetadata] = None

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_have_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def zone_required_for_zone_events(self) -> "EventIn":
        zone_event_types = {
            EventType.ZONE_ENTER,
            EventType.ZONE_EXIT,
            EventType.ZONE_DWELL,
            EventType.BILLING_QUEUE_JOIN,
            EventType.BILLING_QUEUE_ABANDON,
        }
        if self.event_type in zone_event_types and not self.zone_id:
            raise ValueError(f"zone_id is required for event_type={self.event_type}")
        return self

    @model_validator(mode="after")
    def billing_queue_join_needs_depth(self) -> "EventIn":
        if self.event_type == EventType.BILLING_QUEUE_JOIN:
            depth = self.metadata.queue_depth if self.metadata else None
            if depth is None:
                raise ValueError(
                    "metadata.queue_depth is required for BILLING_QUEUE_JOIN"
                )
        return self


# ---------------------------------------------------------------------------
# Ingest response
# ---------------------------------------------------------------------------

class IngestError(BaseModel):
    event_id: str
    reason: str


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    errors: list[IngestError] = []


# ---------------------------------------------------------------------------
# /metrics response
# ---------------------------------------------------------------------------

class ZoneDwellStat(BaseModel):
    zone_id: str
    avg_dwell_seconds: float
    visit_count: int


class StoreMetrics(BaseModel):
    store_id: str
    unique_visitors: int
    conversion_rate: float = Field(..., ge=0.0, le=1.0)
    avg_dwell_per_zone: list[ZoneDwellStat]
    current_queue_depth: int
    abandonment_rate: float = Field(..., ge=0.0, le=1.0)
    window_start: datetime
    window_end: datetime


# ---------------------------------------------------------------------------
# /funnel response
# ---------------------------------------------------------------------------

class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float = Field(..., ge=0.0, le=100.0)


class StoreFunnel(BaseModel):
    store_id: str
    stages: list[FunnelStage]
    total_sessions: int
    window_start: datetime
    window_end: datetime


# ---------------------------------------------------------------------------
# /heatmap response
# ---------------------------------------------------------------------------

class ZoneHeatmapEntry(BaseModel):
    zone_id: str
    visit_frequency_normalised: float = Field(..., ge=0.0, le=100.0)
    avg_dwell_seconds: float
    data_confidence: bool = Field(
        ...,
        description="False if fewer than 20 sessions contributed to this zone's data",
    )


class StoreHeatmap(BaseModel):
    store_id: str
    zones: list[ZoneHeatmapEntry]
    window_start: datetime
    window_end: datetime


# ---------------------------------------------------------------------------
# /anomalies response
# ---------------------------------------------------------------------------

class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"
    STALE_FEED = "STALE_FEED"


class Anomaly(BaseModel):
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    description: str
    suggested_action: str
    detected_at: datetime
    zone_id: Optional[str] = None
    current_value: Optional[float] = None
    baseline_value: Optional[float] = None


class StoreAnomalies(BaseModel):
    store_id: str
    anomalies: list[Anomaly]
    checked_at: datetime


# ---------------------------------------------------------------------------
# /health response
# ---------------------------------------------------------------------------

class StoreFeedStatus(BaseModel):
    store_id: str
    last_event_at: Optional[datetime]
    status: str  # "OK" | "STALE_FEED" | "NO_DATA"


class HealthResponse(BaseModel):
    status: str  # "healthy" | "degraded"
    version: str
    database: str  # "connected" | "unavailable"
    stores: list[StoreFeedStatus]
    checked_at: datetime
