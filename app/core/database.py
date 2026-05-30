import logging
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# SQLite with WAL mode for concurrent reads during async operation
engine = create_async_engine(
    settings.database_url,
    echo=False,
    # SQLite-specific: enable WAL journal mode for better concurrency
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    """Create all tables on startup."""
    # Import models so Base picks them up before create_all
    from app.models import event, store_baseline  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Enable WAL mode for SQLite after connection
    async with AsyncSessionLocal() as session:
        await session.execute(
            __import__("sqlalchemy").text("PRAGMA journal_mode=WAL")
        )
        await session.execute(
            __import__("sqlalchemy").text("PRAGMA synchronous=NORMAL")
        )
        await session.commit()

    logger.info("database_initialized", extra={"database_url": settings.database_url})


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
