"""Every error path must produce the same document shape (docs/03-API.md section 2)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from agentworld.domain import errors as domain_errors
from agentworld.settings import Settings

REQUIRED_KEYS = {"type", "title", "status", "detail"}


class ProgressBody(BaseModel):
    """Module level on purpose.

    A model defined inside a test function is not resolvable by FastAPI's type
    hint evaluation, and the request is then shaped differently enough that
    error locations come out wrong - which is a property of the test, not of
    the application.
    """

    percent: int


def test_unknown_route_is_a_problem_document(client: TestClient) -> None:
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body.keys() >= REQUIRED_KEYS
    assert body["status"] == 404
    assert body["instance"].startswith("req_")


def test_method_not_allowed_is_a_problem_document(client: TestClient) -> None:
    r = client.post("/healthz")
    assert r.status_code == 405
    assert r.json().keys() >= REQUIRED_KEYS


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (domain_errors.NotFoundError("no such project"), 404, "not_found"),
        (domain_errors.PermissionDeniedError("viewer cannot write"), 403, "permission_denied"),
        (domain_errors.ConflictError("slug taken"), 409, "conflict"),
        (domain_errors.ValidationFailedError("bad timestamp"), 422, "validation_failed"),
        (domain_errors.DependencyUnavailableError("redis down"), 503, "dependency_unavailable"),
    ],
)
def test_domain_errors_map_to_status_codes(
    app: FastAPI, exc: domain_errors.DomainError, status: int, code: str
) -> None:
    @app.get("/_boom")
    async def boom() -> None:
        raise exc

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/_boom")

    assert r.status_code == status
    body = r.json()
    assert body["type"].endswith(f"/{code}")
    assert body["detail"] == exc.detail


def test_rate_limited_sets_retry_after(app: FastAPI) -> None:
    @app.get("/_limited")
    async def limited() -> None:
        raise domain_errors.RateLimitedError("slow down", retry_after_seconds=12)

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/_limited")

    assert r.status_code == 429
    assert r.headers["Retry-After"] == "12"


def test_unhandled_exception_leaks_nothing(app: FastAPI) -> None:
    @app.get("/_explode")
    async def explode() -> None:
        raise RuntimeError("database password is hunter2")

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/_explode")

    assert r.status_code == 500
    body = r.json()
    assert "hunter2" not in r.text
    assert body["type"].endswith("/internal_error")
    assert body["instance"].startswith("req_")


def _with_validated_route(app: FastAPI) -> FastAPI:
    @app.post("/_validate")
    async def validate(body: ProgressBody) -> None:
        return None

    return app


def test_validation_error_points_at_the_field(app: FastAPI) -> None:
    with TestClient(_with_validated_route(app), raise_server_exceptions=False) as client:
        r = client.post("/_validate", json={"percent": "not-a-number"})

    assert r.status_code == 422
    body = r.json()
    assert body["type"].endswith("/validation_failed")
    assert body["errors"][0]["pointer"] == "/percent"
    assert body["errors"][0]["code"] == "int_parsing"


def test_malformed_json_points_at_the_whole_document(app: FastAPI) -> None:
    # pydantic reports a character offset here, not a field path. An empty
    # pointer is RFC 6901 for "the whole document"; "/137" would be nonsense.
    with TestClient(_with_validated_route(app), raise_server_exceptions=False) as client:
        r = client.post(
            "/_validate",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )

    assert r.status_code == 422
    error = r.json()["errors"][0]
    assert error["code"] == "json_invalid"
    assert error["pointer"] == ""


# ------------------------------------------------------- database conflicts ---
#
# The exception these build is the shape the asyncpg dialect really produces:
# `exc.orig` is the wrapper the dialect raises, carrying `sqlstate`, and it is
# raised `from` asyncpg's own error, which carries `constraint_name`. That
# shape is asserted end to end - against a real violation, through a real
# route - by `test_a_publish_that_collides_is_a_conflict` in
# `tests/integration/test_executions_api.py`. If these stubs ever stopped
# resembling the driver, that test would fail while these kept passing, which
# is the only reason it is safe to fabricate one here.


class AsyncpgLikeError(Exception):
    """Stands in for asyncpg's own exception: the one naming the constraint."""

    def __init__(self, message: str, constraint_name: str) -> None:
        super().__init__(message)
        self.constraint_name = constraint_name


class DriverLikeError(Exception):
    """Stands in for the DBAPI wrapper the dialect builds around it."""

    def __init__(self, message: str, sqlstate: str | None) -> None:
        super().__init__(message)
        self.sqlstate = self.pgcode = sqlstate


#: Everything a well-behaved handler must keep to itself: the constraint, the
#: table, the statement and the parameters.
LEAKED_STRINGS = (
    "workflow_steps_key_key",
    "workflow_steps",
    "duplicate key",
    "INSERT INTO",
    "writer",
)


def integrity_error(sqlstate: str | None) -> IntegrityError:
    """A violation that looks exactly like one from the real driver."""
    driver = DriverLikeError(
        'duplicate key value violates unique constraint "workflow_steps_key_key"', sqlstate
    )
    driver.__cause__ = AsyncpgLikeError(
        'duplicate key value violates unique constraint "workflow_steps_key_key"',
        "workflow_steps_key_key",
    )
    return IntegrityError(
        "INSERT INTO workflow_steps (workflow_version_id, step_key) VALUES ($1, $2)",
        {"step_key": "writer"},
        driver,
    )


def _with_colliding_route(app: FastAPI, exc: IntegrityError) -> FastAPI:
    @app.get("/_collide")
    async def collide() -> None:
        raise exc

    return app


@pytest.mark.parametrize(
    ("sqlstate", "status", "code"),
    [
        # Somebody else got there first.
        ("23505", 409, "conflict"),
        ("23P01", 409, "conflict"),
        ("23001", 409, "conflict"),
        # This server built a row it should not have, or named an id that does
        # not exist. A 409 would be telling the caller to retry a bug.
        ("23502", 500, "internal_error"),
        ("23514", 500, "internal_error"),
        ("23503", 500, "internal_error"),
        # Not an integrity code at all, and nothing at all. Both fail closed.
        ("42501", 500, "internal_error"),
        ("", 500, "internal_error"),
        (None, 500, "internal_error"),
    ],
)
def test_an_integrity_violation_is_a_conflict_only_for_the_allow_listed_codes(
    app: FastAPI, sqlstate: str | None, status: int, code: str
) -> None:
    """One SQLSTATE at a time, because the allow-list is the whole design.

    A handler that mapped every `IntegrityError` to 409 would pass the first
    three rows and fail the rest, which is what those rows are for.
    """
    with TestClient(
        _with_colliding_route(app, integrity_error(sqlstate)), raise_server_exceptions=False
    ) as client:
        r = client.get("/_collide")

    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body.keys() >= REQUIRED_KEYS
    assert body["status"] == status
    assert body["type"].endswith(f"/{code}")


def test_a_conflict_says_what_to_do_and_nothing_about_the_database(app: FastAPI) -> None:
    """The one sentence a caller can act on, and not one word of schema.

    `detail` is fixed rather than built from the error: the constraint name is
    a detail of this schema, the table name a detail of this release, and the
    statement is not the caller's business at all.
    """
    with TestClient(
        _with_colliding_route(app, integrity_error("23505")), raise_server_exceptions=False
    ) as client:
        r = client.get("/_collide")

    assert r.status_code == 409
    assert r.json()["detail"] == "That change collided with a concurrent change. Try again."
    for leaked in LEAKED_STRINGS:
        assert leaked not in r.text, f"the response named {leaked!r}"


def test_a_500_from_an_integrity_violation_leaks_nothing_either(app: FastAPI) -> None:
    """The branch that keeps its 500 renders the same body as any other 500."""
    with TestClient(
        _with_colliding_route(app, integrity_error("23502")), raise_server_exceptions=False
    ) as client:
        r = client.get("/_collide")

    assert r.status_code == 500
    assert r.json()["detail"].startswith("An unexpected error occurred")
    for leaked in LEAKED_STRINGS:
        assert leaked not in r.text, f"the response named {leaked!r}"


def test_a_conflict_is_traceable_and_readable_by_a_browser(
    app: FastAPI, settings: Settings
) -> None:
    """A 409 is handled inside the request middleware; a 500 is not.

    This is the half of the defect a browser notices. `docs/03-API.md` section 2 says
    every response carries `X-Request-Id`, and an unmodelled exception cannot:
    it is answered by `ServerErrorMiddleware`, which sits outside both the
    request-context middleware and CORS, so the header is missing and a
    browser caller cannot even read the body. Being a modelled status puts the
    response back inside both.
    """
    origin = settings.cors_origin_list[0]
    with TestClient(
        _with_colliding_route(app, integrity_error("23505")), raise_server_exceptions=False
    ) as client:
        r = client.get("/_collide", headers={"Origin": origin})

    assert r.status_code == 409
    assert r.headers["X-Request-Id"] == r.json()["instance"]
    assert r.headers["access-control-allow-origin"] == origin
