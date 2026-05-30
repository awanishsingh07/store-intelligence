# PROMPT: "Write a pytest conftest.py for a FastAPI app using SQLAlchemy async with
# SQLite. I need: an in-memory test DB that resets between tests, an async test client,
# and a fixture that provides a DB session. The app uses a lifespan context manager."
#
# CHANGES MADE: Added override_get_db fixture that injects the test session into the
# app's dependency injection. Switched to StaticPool so the same in-memory connection
# is reused across the session (aiosqlite creates a new DB per connection otherwise).
# Added event_factory fixture based on the actual required schema from the challenge spec.

from typing import AsyncGenerator
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """Fresh in-memory SQLite engine per test function."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Async DB session bound to the test engine."""
    session_factory = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """
    Async test client with DB dependency overridden to use the test session.
    No real network calls — uses ASGI transport.
    """

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

import uuid
from datetime import datetime, timezone


def make_event(**overrides) -> dict:
    """
    Returns a dict matching the required event schema.
    Override any field by passing it as a kwarg.
    """
    defaults = {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": "ENTRY",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.91,
        "metadata": {
            "queue_depth": None,
            "sku_zone": None,
            "session_seq": 1,
        },
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def sample_entry_event() -> dict:
    return make_event(event_type="ENTRY")


@pytest.fixture
def sample_zone_event() -> dict:
    return make_event(
        event_type="ZONE_ENTER",
        zone_id="SKINCARE",
        metadata={"queue_depth": None, "sku_zone": "MOISTURISER", "session_seq": 2},
    )


@pytest.fixture
def sample_billing_queue_event() -> dict:
    return make_event(
        event_type="BILLING_QUEUE_JOIN",
        zone_id="BILLING",
        metadata={"queue_depth": 3, "sku_zone": None, "session_seq": 4},
    )
