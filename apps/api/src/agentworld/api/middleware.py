"""Request-scoped middleware."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agentworld.infra.logging import get_logger, set_request_id

REQUEST_ID_HEADER = "X-Request-Id"

log = get_logger("api.request")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind it for logging, echo it on the response.

    An inbound X-Request-Id is honoured so a caller can correlate across its
    own systems, but it is length-capped: it ends up in logs and in problem
    documents, and neither should be a place a client can write freely.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER, "").strip()
        request_id = inbound[:64] if inbound else f"req_{uuid.uuid4().hex[:20]}"

        set_request_id(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()

        # The id is deliberately never cleared. Clearing it on the way out
        # robbed the 500 handler of the one value worth having on a 500, since
        # that handler runs in ServerErrorMiddleware, outside this middleware.
        # Nothing can read a stale value either: every request sets it first.
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise

        response.headers[REQUEST_ID_HEADER] = request_id

        if request.url.path not in ("/healthz", "/readyz", "/metrics"):
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        return response
