"""RFC 9457 problem details.

Every error response the API produces has the same shape, including the ones
FastAPI and Starlette raise themselves. Clients get one parser, not three.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentworld.domain import errors as domain_errors
from agentworld.infra.logging import get_logger, get_request_id

PROBLEM_BASE_URL = "https://docs.agentworld.dev/errors"
CONTENT_TYPE = "application/problem+json"

_STATUS_BY_ERROR: dict[type[domain_errors.DomainError], int] = {
    domain_errors.ValidationFailedError: 422,
    domain_errors.AuthenticationFailedError: 401,
    domain_errors.NotFoundError: 404,
    domain_errors.PermissionDeniedError: 403,
    domain_errors.ConflictError: 409,
    domain_errors.CredentialUnreadableError: 500,
    domain_errors.RateLimitedError: 429,
    domain_errors.DependencyUnavailableError: 503,
}

_TITLE_BY_STATUS: dict[int, str] = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Payload too large",
    422: "Validation failed",
    429: "Rate limited",
    500: "Internal server error",
    503: "Service unavailable",
}

log = get_logger(__name__)


def problem(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    locator: Mapping[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE_URL}/{code}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    if (rid := get_request_id()) is not None:
        body["instance"] = rid
    if errors:
        body["errors"] = errors
    if locator:
        body.update(locator)
    return JSONResponse(status_code=status, content=body, headers=headers, media_type=CONTENT_TYPE)


#: The only context keys a problem document carries. They say *where* in the
#: request the problem is, which is the one thing a caller cannot work out for
#: themselves - and nothing about what the server knows.
#:
#: Deliberately not the whole of `DomainError.context`. That is written for
#: logs, where the trust boundary is different, and splatting it into a
#: response would make every future error author's choice of keyword a
#: disclosure decision they did not know they were taking. `value` is left out
#: for the same reason it looks tempting: the caller already sent it, so
#: echoing it adds nothing and creates a reflection surface.
LOCATOR_KEYS: Final = ("field", "index")


def _locator(exc: domain_errors.DomainError) -> dict[str, Any]:
    """Where in the request the problem is, when the error knows.

    This exists because of the ingest plane. A caller posting 500 events and
    told only "an occurred_at is an ISO-8601 timestamp" has to bisect their
    own batch to find out which one - and the parser already knew.
    """
    return {
        key: exc.context[key]
        for key in LOCATOR_KEYS
        if key in exc.context and exc.context[key] is not None
    }


def _json_pointer(loc: tuple[int | str, ...], code: str) -> str:
    """Turn a pydantic error location into an RFC 6901 JSON pointer.

    loc[0] is the location kind ("body", "query", "path"), which the pointer
    does not express, so it is dropped. For a malformed document pydantic
    reports a character offset rather than a field path, and pointing at
    "/137" would be nonsense - the empty pointer means the whole document.
    """
    if code == "json_invalid":
        return ""
    parts = loc[1:]
    # RFC 6901 escaping: "~" -> "~0", "/" -> "~1".
    escaped = (str(p).replace("~", "~0").replace("/", "~1") for p in parts)
    return "/" + "/".join(escaped) if parts else ""


def _status_for(exc: domain_errors.DomainError) -> int:
    for error_type, status in _STATUS_BY_ERROR.items():
        if isinstance(exc, error_type):
            return status
    return 500


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(domain_errors.DomainError)
    async def _domain(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, domain_errors.DomainError)
        status = _status_for(exc)
        headers = None
        if isinstance(exc, domain_errors.RateLimitedError):
            headers = {"Retry-After": str(exc.retry_after_seconds)}
        elif isinstance(exc, domain_errors.AuthenticationFailedError) and exc.challenge:
            # RFC 9110: a 401 states the scheme it wants. The ingest plane
            # uses a custom header rather than Authorization, so the two
            # planes send different challenges.
            headers = {"WWW-Authenticate": exc.challenge}
        if status >= 500:
            log.error("domain_error", code=exc.code, detail=exc.detail, **exc.context)
        return problem(
            status=status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            headers=headers,
            locator=_locator(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        errors = [
            {
                "pointer": _json_pointer(err["loc"], err["type"]),
                "code": err["type"],
                "detail": err["msg"],
            }
            for err in exc.errors()
        ]
        return problem(
            status=422,
            code="validation_failed",
            title="Validation failed",
            detail=f"{len(errors)} field(s) failed validation",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        status = exc.status_code
        title = _TITLE_BY_STATUS.get(status, "Error")
        return problem(
            status=status,
            code=title.lower().replace(" ", "_"),
            title=title,
            detail=str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to the caller; the request id is the join key
        # for whoever reads the logs.
        log.exception("unhandled_exception", path=request.url.path)
        return problem(
            status=500,
            code="internal_error",
            title="Internal server error",
            detail="An unexpected error occurred. Quote the instance id when reporting it.",
        )
