from datetime import datetime
from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class StoreBaseline(Base):
    """
    Per-store baseline metrics used by /anomalies endpoint.

    Since we only have ~20 min of footage per store (not 7 days of real history),
    we seed a default baseline conversion rate on startup.
    A CONVERSION_DROP anomaly fires when the live rate deviates significantly
    from this baseline.

    Design decision documented in DESIGN.md: seeded constant rather than
    learned rolling average, acceptable given the dataset constraints.
    """

    __tablename__ = "store_baselines"

    store_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    baseline_conversion_rate: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )

    def __repr__(self) -> str:
        return (
            f"<StoreBaseline store={self.store_id} "
            f"conversion_rate={self.baseline_conversion_rate}>"
        )
