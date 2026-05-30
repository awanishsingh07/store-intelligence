from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.database import init_db
from app.core.errors import generic_error_handler, sqlalchemy_error_handler
from app.core.logging import RequestLoggingMiddleware, setup_logging
from app.routers import health, ingest, stores

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: configure logging, initialise DB (creates tables if missing).
    Shutdown: nothing needed for SQLite.
    """
    setup_logging()
    await init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Store Intelligence API — real-time retail analytics from CCTV-derived events. "
        "Built for Apex Retail's offline store analytics challenge."
    ),
    lifespan=lifespan,
    # Disable default /docs redirect to keep responses clean in production
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url="/redoc" if settings.environment != "production" else None,
)

# --- Middleware ---
app.add_middleware(RequestLoggingMiddleware)

# --- Exception handlers ---
app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)
app.add_exception_handler(Exception, generic_error_handler)

# --- Routers ---
app.include_router(ingest.router)
app.include_router(stores.router)
app.include_router(health.router)
