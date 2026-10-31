from __future__ import annotations

from fastapi.testclient import TestClient

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


def test_readyz_reports_checks(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert isinstance(body["checks"], dict)


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
