import logging
import sys
import uuid
import time
from contextlib import asynccontextmanager
from pythonjsonlogger import jsonlogger
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Quiet noisy libs
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every request with:
      trace_id, store_id, endpoint, latency_ms, event_count, status_code
    Matches the structured logging requirement in Part C.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id

        start = time.monotonic()
        response = await call_next(request)
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        # Extract store_id from path params if present (/stores/{store_id}/...)
        store_id = request.path_params.get("store_id", None)

        # event_count is set by the ingest endpoint on request.state
        event_count = getattr(request.state, "event_count", None)

        log_record: dict = {
            "trace_id": trace_id,
            "method": request.method,
            "endpoint": request.url.path,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
        }
        if store_id:
            log_record["store_id"] = store_id
        if event_count is not None:
            log_record["event_count"] = event_count

        logger.info("request_completed", extra=log_record)

        # Propagate trace_id in response headers for debugging
        response.headers["X-Trace-ID"] = trace_id
        return response
