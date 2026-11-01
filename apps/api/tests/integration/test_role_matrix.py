"""Every control-plane endpoint, by every role, over real HTTP.

`docs/05-ROADMAP.md` asks Phase 1 for exactly this. `tests/unit/test_policy.py`
covers the matrix as pure logic, which is where it is cheap to be exhaustive;
this file covers the half that logic cannot: whether each *route* asks for the
action it should. A route wired to `PROJECT_READ` when it mutates would pass
every policy test and fail here.

The expectations below are transcribed from the role table in
`docs/03-API.md` section 3.2, not from the code, so that the two have to agree.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentworld.domain.enums import MemberRole
from agentworld.infra.db import Database
from helpers import Actor, FakeAuthProvider, seed_membership, seed_user

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Endpoint:
    """One route, the role it needs, and a body if it takes one."""

    method: str
    path: str
    minimum: MemberRole
    body: dict[str, Any] | None = None

    def __str__(self) -> str:  # keeps pytest ids readable
        return f"{self.method} {self.path}"


#: `{workspace}`, `{project}`, `{key}` and `{outsider}` are substituted with
#: the ids from the `scenario` fixture.
ENDPOINTS = [
    Endpoint("GET", "/v1/workspaces/{workspace}", MemberRole.VIEWER),
    Endpoint("PATCH", "/v1/workspaces/{workspace}", MemberRole.ADMIN, {"name": "Renamed"}),
    Endpoint("DELETE", "/v1/workspaces/{workspace}", MemberRole.OWNER),
    Endpoint("GET", "/v1/workspaces/{workspace}/members", MemberRole.VIEWER),
    Endpoint(
        "POST",
        "/v1/workspaces/{workspace}/members",
        MemberRole.ADMIN,
        {"email": "outsider@example.com", "role": "viewer"},
    ),
    Endpoint("GET", "/v1/workspaces/{workspace}/projects", MemberRole.VIEWER),
    Endpoint(
        "POST",
        "/v1/workspaces/{workspace}/projects",
        MemberRole.MEMBER,
        {"name": "Another Project"},
    ),
    Endpoint("GET", "/v1/projects/{project}", MemberRole.VIEWER),
    Endpoint("PATCH", "/v1/projects/{project}", MemberRole.MEMBER, {"name": "Renamed"}),
    Endpoint("DELETE", "/v1/projects/{project}", MemberRole.ADMIN),
    Endpoint("GET", "/v1/workspaces/{workspace}/api-keys", MemberRole.ADMIN),
    Endpoint(
        "POST",
        "/v1/workspaces/{workspace}/api-keys",
        MemberRole.ADMIN,
        {"name": "Another Key", "scope": "control"},
    ),
    Endpoint("POST", "/v1/api-keys/{key}/revoke", MemberRole.ADMIN),
    Endpoint("DELETE", "/v1/api-keys/{key}", MemberRole.ADMIN),
]


@dataclass
class Scenario:
    workspace_id: str
    project_id: str
    api_key_id: str
    actors: dict[MemberRole, Actor]

    def url(self, template: str) -> str:
        return template.format(
            workspace=self.workspace_id, project=self.project_id, key=self.api_key_id
        )


@pytest.fixture
async def scenario(
    client: TestClient,
    actor: Actor,
    auth: FakeAuthProvider,
    owner_database: Database,
) -> Scenario:
    """A workspace with one member at each role, plus a project and a key.

    The owner is the `actor` fixture, who creates everything through the API.
    The other three are seeded directly: they never make a request before the
    one under test, so there is nothing for `GET /v1/me` to materialise.
    """
    workspace = client.post(
        "/v1/workspaces", json={"name": "Matrix Workspace"}, headers=actor.headers
    ).json()
    project = client.post(
        f"/v1/workspaces/{workspace['id']}/projects",
        json={"name": "Matrix Project"},
        headers=actor.headers,
    ).json()
    api_key = client.post(
        f"/v1/workspaces/{workspace['id']}/api-keys",
        json={"name": "Matrix Key", "scope": "ingest", "project_id": project["id"]},
        headers=actor.headers,
    ).json()

    actors = {MemberRole.OWNER: actor}
    for role in (MemberRole.VIEWER, MemberRole.MEMBER, MemberRole.ADMIN):
        member = Actor(auth, f"{role.value}@example.com")
        await seed_user(owner_database, member.user_id, member.email)
        await seed_membership(owner_database, uuid.UUID(workspace["id"]), member.user_id, role)
        actors[role] = member

    # Someone who exists but belongs to no workspace, as the target of the
    # "add a member" call.
    await seed_user(owner_database, uuid.uuid4(), "outsider@example.com")

    return Scenario(
        workspace_id=workspace["id"],
        project_id=project["id"],
        api_key_id=api_key["id"],
        actors=actors,
    )


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=str)
@pytest.mark.parametrize("role", list(MemberRole), ids=lambda role: role.value)
def test_role_matrix(
    client: TestClient, scenario: Scenario, endpoint: Endpoint, role: MemberRole
) -> None:
    actor = scenario.actors[role]
    response = client.request(
        endpoint.method,
        scenario.url(endpoint.path),
        json=endpoint.body,
        headers=actor.headers,
    )

    if role.at_least(endpoint.minimum):
        assert response.status_code < 400, (
            f"{role.value} should be allowed to {endpoint}, got "
            f"{response.status_code}: {response.text}"
        )
    else:
        # 403, specifically: the caller is a member, so the workspace's
        # existence is not a secret from them and a 404 would be misleading.
        assert response.status_code == 403, (
            f"{role.value} should be forbidden from {endpoint}, got {response.status_code}"
        )
        assert response.headers["content-type"].startswith("application/problem+json")


def test_the_matrix_exercises_both_outcomes(scenario: Scenario) -> None:
    """Guard on the guard.

    If every endpoint happened to require `viewer`, the parametrised test
    above would pass while asserting nothing about denial.
    """
    minimums = {endpoint.minimum for endpoint in ENDPOINTS}
    assert len(minimums) > 1, "the matrix has to contain endpoints of differing privilege"
    assert MemberRole.OWNER in minimums
    assert MemberRole.VIEWER in minimums


def test_the_matrix_covers_every_mutating_route(app_routes: set[str]) -> None:
    """No control-plane route may be absent from the table above.

    A new endpoint added without a row here would otherwise be untested
    against the role matrix, which is the test most likely to catch a
    privilege mistake.
    """
    covered = {f"{endpoint.method} {endpoint.path}" for endpoint in ENDPOINTS}
    # `/v1/me` has no role: the only resource it reaches is the caller
    # themselves, identified by the token rather than by the request.
    exempt = {
        "GET /v1/me",
        "PATCH /v1/me",
        "GET /v1/workspaces",
        "POST /v1/workspaces",
        "GET /v1/self/key",
        "GET /healthz",
        "GET /readyz",
        "GET /v1/meta",
    }
    missing = app_routes - covered - exempt
    assert missing == set(), f"routes with no role-matrix row: {sorted(missing)}"


@pytest.fixture
def app_routes(client: TestClient) -> set[str]:
    """Every route the app serves, in the `METHOD /path` form used above."""
    routes: set[str] = set()
    for route in client.app.routes:  # type: ignore[attr-defined]
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        # FastAPI's own documentation routes, including the OAuth2 redirect
        # helper it mounts beside /docs.
        if path is None or methods is None:
            continue
        if path.startswith(("/openapi", "/docs")):
            continue
        normalised = (
            path.replace("{workspace_id}", "{workspace}")
            .replace("{project_id}", "{project}")
            .replace("{api_key_id}", "{key}")
        )
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            routes.add(f"{method} {normalised}")
    return routes
