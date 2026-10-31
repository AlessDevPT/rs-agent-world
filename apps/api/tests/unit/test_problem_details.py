"""Every error path must produce the same document shape (docs/03-API.md section 2)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agentworld.domain import errors as domain_errors

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
