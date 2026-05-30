from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Application
    app_name: str = "Store Intelligence API"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/store_intelligence.db"

    # Business constants
    # How long (seconds) a visitor can be gone before re-appearing counts as re-entry
    reentry_window_seconds: int = 300
    # Zone dwell event emit interval (seconds)
    dwell_emit_interval_seconds: int = 30
    # POS correlation window — visitor must be in billing zone within this many
    # seconds before a transaction to count as converted
    pos_correlation_window_seconds: int = 300
    # Stale feed threshold for /health
    stale_feed_threshold_seconds: int = 600
    # Anomaly: dead zone if no visits in this many seconds
    dead_zone_threshold_seconds: int = 1800
    # Anomaly: seeded baseline conversion rate (used until real 7-day data exists)
    baseline_conversion_rate: float = 0.35
    # Heatmap: flag low-confidence if fewer than this many sessions in window
    heatmap_min_sessions: int = 20
    # Ingest: max batch size
    ingest_max_batch_size: int = 500

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
