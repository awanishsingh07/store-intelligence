import logging
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError, OperationalError

logger = logging.getLogger(__name__)


async def sqlalchemy_error_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    """
    Catches all SQLAlchemy errors and returns a structured 503.
    Ensures no raw stack traces leak into API responses (Part C requirement).
    """
    trace_id = getattr(request.state, "trace_id", "unknown")

    logger.error(
        "database_error",
        extra={
            "trace_id": trace_id,
            "endpoint": request.url.path,
            "error_type": type(exc).__name__,
            "error": str(exc),
        },
    )

    return JSONResponse(
        status_code=503,
        content={
            "error": "service_unavailable",
            "message": "Database is temporarily unavailable. Please retry shortly.",
            "trace_id": trace_id,
        },
    )


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions — never expose internals."""
    trace_id = getattr(request.state, "trace_id", "unknown")

    logger.exception(
        "unhandled_error",
        extra={
            "trace_id": trace_id,
            "endpoint": request.url.path,
            "error_type": type(exc).__name__,
        },
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred.",
            "trace_id": trace_id,
        },
    )
