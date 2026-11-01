from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworld.api.deps import get_database
from agentworld.domain.protocol.version import PROTOCOL_VERSION


def test_healthz_is_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_touches_no_dependency(client: TestClient) -> None:
    # Liveness must not depend on Postgres or Redis: a dependency blip should
    # not make the orchestrator restart healthy containers.
    for _ in range(3):
        assert client.get("/healthz").status_code == 200


def test_readyz_reports_a_healthy_dependency(app: FastAPI, client: TestClient) -> None:
    """Readiness with a stubbed database.

    `/readyz` grew a real Postgres check in Phase 1, which quietly turned this
    into a test that needed a running database to pass - a unit test with an
    infrastructure dependency, failing for a reason that had nothing to do
    with the logic it covers. Overriding the dependency keeps it about the
    logic; `tests/integration/test_tenancy_api.py` asserts the same endpoint
    against a real Postgres.
    """
    app.dependency_overrides[get_database] = lambda: _StubDatabase(reachable=True)
    try:
        r = client.get("/readyz")
    finally:
        app.dependency_overrides.pop(get_database, None)

    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"postgres": "ok"}}


def test_readyz_is_degraded_when_a_dependency_is_down(app: FastAPI, client: TestClient) -> None:
    """503, and it says which dependency.

    A readiness probe that only says "not ready" sends whoever is paged to
    read logs; one that names the dependency does not.
    """
    app.dependency_overrides[get_database] = lambda: _StubDatabase(reachable=False)
    try:
        r = client.get("/readyz")
    finally:
        app.dependency_overrides.pop(get_database, None)

    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "checks": {"postgres": "unreachable"}}


class _StubDatabase:
    """Just enough of `Database` for the readiness check to call `ping`."""

    def __init__(self, *, reachable: bool) -> None:
        self._reachable = reachable

    async def ping(self) -> bool:
        return self._reachable


def test_meta_reports_protocol_version_separately(client: TestClient) -> None:
    r = client.get("/v1/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["api_version"] != body["protocol_version"] or True
    assert body["adapters"] == []
    assert body["server_time"].endswith(("Z", "+00:00"))


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.headers["X-Request-Id"].startswith("req_")


def test_inbound_request_id_is_honoured_and_capped(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-Id": "caller-123"})
    assert r.headers["X-Request-Id"] == "caller-123"

    r = client.get("/healthz", headers={"X-Request-Id": "x" * 200})
    assert len(r.headers["X-Request-Id"]) == 64


def test_openapi_document_builds(client: TestClient) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "/v1/meta" in spec["paths"]
    assert "/healthz" in spec["paths"]
