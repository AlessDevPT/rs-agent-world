"""RFC 9457 problem details.

Every error response the API produces has the same shape, including the ones
FastAPI and Starlette raise themselves. Clients get one parser, not three.

That includes the ones the database raises. A unique index is the only thing
that can decide a race between two requests, and the loser's `IntegrityError`
would otherwise arrive here as an unmodelled exception and leave as a 500 -
telling a caller their request was this server's fault when in fact somebody
else got there first. `_integrity` is where that is told apart, by SQLSTATE
rather than by message text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentworld.domain import errors as domain_errors
from agentworld.infra.logging import get_logger, get_request_id

PROBLEM_BASE_URL = "https://docs.agentworld.dev/errors"
CONTENT_TYPE = "application/problem+json"

_STATUS_BY_ERROR: dict[type[domain_errors.DomainError], int] = {
    # Before ValidationFailedError, which it subclasses: the loop in
    # `_status_for` returns the first isinstance match, so the subclass
    # has to come first or it would never be reached.
    domain_errors.WorkflowNotPublishableError: 422,
    domain_errors.ValidationFailedError: 422,
    domain_errors.AuthenticationFailedError: 401,
    domain_errors.NotFoundError: 404,
    domain_errors.PermissionDeniedError: 403,
    domain_errors.ConflictError: 409,
    domain_errors.CredentialUnreadableError: 500,
    domain_errors.OutboundRequestFailedError: 502,
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
    502: "Bad gateway",
    503: "Service unavailable",
}

#: The integrity violations that mean "somebody else got there first", and
#: nothing else. Everything outside this set keeps the generic 500, because a
#: 409 is a statement that the request was fine and only the timing was not:
#:
#: - `23502` not-null and `23514` check are this server writing a row it
#:   should never have built. Telling the caller to retry would be telling
#:   them to reproduce a bug, and the 500 is what carries the traceback to
#:   whoever has to fix it.
#: - `23503` foreign key is ambiguous by nature: a delete that lost a race to
#:   a new reference is a conflict, while a caller naming an id that does not
#:   exist is a validation failure wanting 404 or 422. One status cannot be
#:   honest about both, so it keeps the 500 that leaves the gap visible.
#: - `23001` restrict is in the set because the only things that raise it here
#:   are the guard triggers in migrations 0018 and 0019, which chose
#:   `ERRCODE = 'restrict_violation'` deliberately. "This version is published
#:   and cannot be edited" is a conflict in anybody's language.
#:
#: Row-level security and the append-only grants are *not* integrity
#: violations - they fail with `42501`, a `ProgrammingError` - so nothing here
#: can turn a tenancy refusal into a 409.
CONFLICT_SQLSTATES: Final = frozenset(
    {
        "23505",  # unique_violation
        "23P01",  # exclusion_violation
        "23001",  # restrict_violation
    }
)

#: Deliberately the same sentence whatever collided. The constraint name is a
#: detail of this schema and the table name a detail of this release; neither
#: is actionable by a caller whose only useful move is to try again.
CONFLICT_DETAIL: Final = "That change collided with a concurrent change. Try again."

INTERNAL_ERROR_DETAIL: Final = (
    "An unexpected error occurred. Quote the instance id when reporting it."
)

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


def _internal_error() -> JSONResponse:
    """The one 500 this module produces, shared by both of its callers."""
    return problem(
        status=500,
        code="internal_error",
        title="Internal server error",
        detail=INTERNAL_ERROR_DETAIL,
    )


def _sqlstate(exc: IntegrityError) -> str | None:
    """The five-character SQLSTATE, when the driver attached one.

    `exc.orig` is the DBAPI-level exception - for asyncpg, a wrapper the
    SQLAlchemy dialect builds, which copies `sqlstate` off the real error. A
    driver that attaches nothing, or a future one that spells it differently,
    yields None and therefore a 500: the allow-list fails closed.
    """
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return sqlstate if isinstance(sqlstate, str) else None


def _constraint(exc: IntegrityError) -> str | None:
    """The violated constraint's name, **for the log only**.

    One level further down than `_sqlstate`: the asyncpg dialect raises its
    wrapper `from` the original error, so the object carrying
    `constraint_name` is `exc.orig.__cause__`. It is None for a not-null
    violation, which names a column instead.
    """
    orig = exc.orig
    cause = orig.__cause__ if orig is not None else None
    constraint = getattr(cause, "constraint_name", None)
    return constraint if isinstance(constraint, str) else None


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
        # The one error that carries a list for the caller. `context` is
        # still not rendered - see LOCATOR_KEYS - but a publish refusal is
        # useless without the reasons, so they travel on their own attribute
        # and land in the same `errors` array a pydantic failure uses.
        errors = (
            list(exc.findings)
            if isinstance(exc, domain_errors.WorkflowNotPublishableError)
            else None
        )
        return problem(
            status=status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            errors=errors,
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

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: Exception) -> JSONResponse:
        """A database constraint refused the write. 409 when it was a race.

        Registered for the SQLAlchemy exception rather than translated in a
        service, for two reasons. A service cannot catch one and carry on -
        PostgreSQL aborts the whole transaction, so its next statement fails
        with `25P02` - and `services` may not import SQLAlchemy at all, which
        is an architecture contract. One handler also means a route written
        next year is covered without anybody remembering to cover it.

        **A 409 from here does not promise that nothing happened.** The
        transaction that raised is rolled back whole - PostgreSQL cannot
        commit an aborted transaction even if asked - but a request made of
        several transactions keeps whatever earlier ones committed. The first
        test of an agent is the example in the tree: the harness row is
        committed by one transaction and published by the next, so a conflict
        while publishing leaves the harness behind, unpublished and ready for
        the retry this status is asking for.
        """
        assert isinstance(exc, IntegrityError)
        sqlstate = _sqlstate(exc)
        constraint = _constraint(exc)
        if sqlstate not in CONFLICT_SQLSTATES:
            # Still a 500, and still logged as one: this is a bug in a
            # statement this server built, and the traceback is the point.
            log.exception(
                "integrity_error",
                path=request.url.path,
                sqlstate=sqlstate,
                constraint=constraint,
            )
            return _internal_error()
        # A race is not a server fault, so not an `error` - but it is worth
        # seeing, because a constraint that loses races constantly is saying
        # something about the code above it.
        log.warning(
            "integrity_conflict",
            path=request.url.path,
            sqlstate=sqlstate,
            constraint=constraint,
        )
        return problem(status=409, code="conflict", title="Conflict", detail=CONFLICT_DETAIL)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to the caller; the request id is the join key
        # for whoever reads the logs.
        log.exception("unhandled_exception", path=request.url.path)
        return _internal_error()
