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
from sqlalchemy import text

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
    #: What a *permitted* caller gets here, when it is not a success.
    #:
    #: One shape of endpoint needs this: a verb whose precondition the
    #: scenario deliberately does not meet. `POST .../restore` is the
    #: example - the scenario's agent and workflow are live, so a permitted
    #: caller gets the 404 that restoring something never archived earns,
    #: while a member and a viewer still get 403. Leaving it unset asserts
    #: the usual `< 400`; setting it keeps the privilege half of the row
    #: honest without archiving a fixture every other row depends on.
    permitted: int | None = None

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
    # Phase 1 routes that were never covered, because the guard below could
    # not see any route at all until Phase 2 fixed it.
    Endpoint(
        "PATCH",
        "/v1/workspaces/{workspace}/members/{target}",
        MemberRole.ADMIN,
        {"role": "member"},
    ),
    Endpoint("DELETE", "/v1/workspaces/{workspace}/members/{target}", MemberRole.ADMIN),
    # ---- Phase 2 -----------------------------------------------------------
    Endpoint("GET", "/v1/projects/{project}/agents", MemberRole.VIEWER),
    Endpoint(
        "POST",
        "/v1/projects/{project}/agents",
        MemberRole.MEMBER,
        {"display_name": "Matrix Agent", "interaction_mode": "managed"},
    ),
    Endpoint("GET", "/v1/projects/{project}/agents/{agent}", MemberRole.VIEWER),
    # Editing is `member`; the configuration half is `admin` and is covered
    # by its own test in `test_agents_api.py`, because the role it needs
    # depends on which fields the body carries rather than on the route.
    Endpoint(
        "PATCH",
        "/v1/projects/{project}/agents/{agent}",
        MemberRole.MEMBER,
        {"display_name": "Renamed"},
    ),
    Endpoint("DELETE", "/v1/projects/{project}/agents/{agent}", MemberRole.ADMIN),
    # Restoring is `admin` like archiving. The scenario's agent is live, so
    # a permitted caller meets the 404 that restoring something never
    # archived earns - which is what `permitted` is for: this row proves a
    # member and a viewer are refused with 403 instead, and
    # `test_restore.py` covers the successful restore.
    Endpoint(
        "POST",
        "/v1/projects/{project}/agents/{agent}/restore",
        MemberRole.ADMIN,
        permitted=404,
    ),
    # `member`, and sensitive: a test run spends money. It also creates and
    # publishes the agent's one-step harness on first use, so the roles for
    # `workflow.create`, `workflow.publish` and `workflow.run` have to admit
    # a member too - `tests/unit/test_policy.py` holds them to that.
    Endpoint(
        "POST",
        "/v1/projects/{project}/agents/{agent}/test",
        MemberRole.MEMBER,
        {"input": "Say hello."},
    ),
    Endpoint("GET", "/v1/workspaces/{workspace}/model-profiles", MemberRole.VIEWER),
    Endpoint(
        "POST",
        "/v1/workspaces/{workspace}/model-profiles",
        MemberRole.ADMIN,
        {"name": "Matrix Profile", "provider_slug": "anthropic", "model_id": "claude-opus-5"},
    ),
    Endpoint(
        "PATCH",
        "/v1/workspaces/{workspace}/model-profiles/{profile}",
        MemberRole.ADMIN,
        {"name": "Renamed by the matrix"},
    ),
    Endpoint("DELETE", "/v1/workspaces/{workspace}/model-profiles/{profile}", MemberRole.ADMIN),
    Endpoint("GET", "/v1/projects/{project}/connections", MemberRole.VIEWER),
    # A read-only grant, so this row tests the baseline role. The elevation
    # to `admin` for a consequential tool is a property of the body, and has
    # its own test in `test_workflows_api.py`.
    Endpoint(
        "POST",
        "/v1/projects/{project}/connections",
        MemberRole.MEMBER,
        {
            "kind": "grant_tool",
            "source_kind": "agent",
            # A different agent from the one the scenario already granted
            # to: `connections` has a unique index over
            # (project, source_kind, source_id, kind) where enabled, so a
            # second grant from the same agent is a 409 rather than a
            # permission answer.
            "source_id": "{other_agent}",
            "target_kind": "tool",
            "target_id": "{other_agent}",
            "config": {"tool_name": "web_search", "side_effect": "read"},
        },
    ),
    Endpoint("DELETE", "/v1/projects/{project}/connections/{connection}", MemberRole.MEMBER),
    Endpoint("GET", "/v1/projects/{project}/workflows", MemberRole.VIEWER),
    Endpoint(
        "POST",
        "/v1/projects/{project}/workflows",
        MemberRole.MEMBER,
        {"name": "Matrix Workflow"},
    ),
    Endpoint("GET", "/v1/projects/{project}/workflows/{workflow}", MemberRole.VIEWER),
    Endpoint(
        "PATCH",
        "/v1/projects/{project}/workflows/{workflow}",
        MemberRole.MEMBER,
        {"name": "Renamed by the matrix"},
    ),
    # `connection.update` is `member` here because the scenario's
    # connection grants a read-only tool. The admin half of the rule -
    # either side of the edit being consequential - is
    # `test_workflows_api.py`'s, where the config can be chosen.
    Endpoint(
        "PATCH",
        "/v1/projects/{project}/connections/{connection}",
        MemberRole.MEMBER,
        {"enabled": True},
    ),
    Endpoint("GET", "/v1/projects/{project}/workflows/{workflow}/versions", MemberRole.VIEWER),
    # `workflow.read` *and* `execution.read`, both viewer. The row below
    # proves the route asks for no more than a viewer has; which two it
    # asks for is `test_workflow_reads.py`'s.
    Endpoint("GET", "/v1/projects/{project}/workflows/{workflow}/executions", MemberRole.VIEWER),
    Endpoint("GET", "/v1/projects/{project}/workflows/{workflow}/draft", MemberRole.VIEWER),
    Endpoint(
        "PUT",
        "/v1/projects/{project}/workflows/{workflow}/draft",
        MemberRole.MEMBER,
        {"graph": {"steps": [], "edges": []}},
    ),
    Endpoint("POST", "/v1/projects/{project}/workflows/{workflow}/validate", MemberRole.MEMBER),
    Endpoint("DELETE", "/v1/projects/{project}/workflows/{workflow}", MemberRole.ADMIN),
    Endpoint(
        "POST",
        "/v1/projects/{project}/workflows/{workflow}/restore",
        MemberRole.ADMIN,
        permitted=404,
    ),
    # ---- Phase 3 / T1 ------------------------------------------------------
    # Every credential route is `admin`, and the table is uniform on purpose:
    # there is no viewer row, no member row, and no row at any role that
    # reads material, because no such route exists.
    Endpoint("GET", "/v1/workspaces/{workspace}/credentials", MemberRole.ADMIN),
    Endpoint(
        "POST",
        "/v1/workspaces/{workspace}/credentials",
        MemberRole.ADMIN,
        # A different provider from the one the scenario already stored for,
        # so the call is answering a permission question rather than hitting
        # `credentials_name_key`.
        {
            "name": "Matrix Credential",
            "kind": "api_key",
            "provider_slug": "openai",
            "secret": "sk-matrix-0123456789abcdef",
        },
    ),
    Endpoint(
        "POST",
        "/v1/workspaces/{workspace}/credentials/{credential}/rotate",
        MemberRole.ADMIN,
        {"secret": "sk-matrix-rotated-0123456789"},
    ),
    Endpoint(
        "POST", "/v1/workspaces/{workspace}/credentials/{credential}/revoke", MemberRole.ADMIN
    ),
    Endpoint("DELETE", "/v1/workspaces/{workspace}/credentials/{credential}", MemberRole.ADMIN),
    # ---- Phase 3 / T5 ------------------------------------------------------
    # Publishing freezes a graph and running one spends money and reaches the
    # outside world. Both are `member`, which is the row `docs` specifies -
    # and the reason they are separate routes rather than one "save and run".
    Endpoint("POST", "/v1/projects/{project}/workflows/{workflow}/publish", MemberRole.MEMBER),
    Endpoint("POST", "/v1/projects/{project}/workflows/{workflow}/run", MemberRole.MEMBER, {}),
    Endpoint("GET", "/v1/projects/{project}/executions", MemberRole.VIEWER),
    Endpoint("GET", "/v1/projects/{project}/executions/{execution}", MemberRole.VIEWER),
    # member, per 05-PERMISSIONS.md: stopping work that is running is an
    # operator action, not an administrative one.
    Endpoint("POST", "/v1/projects/{project}/executions/{execution}/cancel", MemberRole.MEMBER),
    # ---- Phase 3 / T6b ----------------------------------------------------
    # The split in privilege is the design: seeing that an execution is
    # stuck is operational awareness, while asserting what happened to an
    # effect nobody observed is not. The resolve row is `admin` and is the
    # only reason `dispatch.resolve_review` exists as its own action.
    Endpoint("GET", "/v1/projects/{project}/dispatches", MemberRole.VIEWER),
    Endpoint(
        "POST",
        "/v1/projects/{project}/dispatches/{dispatch}/resolve",
        MemberRole.ADMIN,
        {"outcome": "failed", "note": "Resolved while checking the role matrix."},
    ),
]


@dataclass
class Scenario:
    workspace_id: str
    project_id: str
    api_key_id: str
    agent_id: str
    other_agent_id: str
    profile_id: str
    connection_id: str
    workflow_id: str
    #: An execution of that workflow, so the read rows have something to
    #: read. Created by the fixture, not by the row under test.
    execution_id: str
    #: Its dispatch, parked in `needs_review` so the resolve row answers a
    #: permission question rather than "this is not awaiting review".
    dispatch_id: str
    credential_id: str
    #: A member who is never one of the callers, so that removing them is
    #: never self-service - which a viewer may do regardless of role.
    target_user_id: str
    actors: dict[MemberRole, Actor]

    def substitutions(self) -> dict[str, str]:
        return {
            "workspace": self.workspace_id,
            "project": self.project_id,
            "key": self.api_key_id,
            "agent": self.agent_id,
            "other_agent": self.other_agent_id,
            "profile": self.profile_id,
            "connection": self.connection_id,
            "workflow": self.workflow_id,
            "execution": self.execution_id,
            "dispatch": self.dispatch_id,
            "credential": self.credential_id,
            "target": self.target_user_id,
        }

    def url(self, template: str) -> str:
        return template.format(**self.substitutions())

    def body(self, template: dict[str, Any] | None) -> dict[str, Any] | None:
        """Substitute ids inside a request body as well as inside a path.

        One endpoint needs it: creating a connection names the agent it
        grants to, and that id is not known until the scenario is built.
        """
        if template is None:
            return None
        values = self.substitutions()
        return {
            key: value.format(**values) if isinstance(value, str) else value
            for key, value in template.items()
        }


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

    # A fifth member, only ever a target. If the member rows below aimed at
    # one of the four callers, the call would be self-service when that
    # caller made it - which a viewer may do - and the row would be testing
    # the wrong rule.
    target_user_id = uuid.uuid4()
    await seed_user(owner_database, target_user_id, "target@example.com")
    await seed_membership(
        owner_database, uuid.UUID(workspace["id"]), target_user_id, MemberRole.VIEWER
    )

    agent = client.post(
        f"/v1/projects/{project['id']}/agents",
        json={"display_name": "Matrix Researcher", "interaction_mode": "managed"},
        headers=actor.headers,
    ).json()
    other_agent = client.post(
        f"/v1/projects/{project['id']}/agents",
        json={"display_name": "Matrix Writer", "interaction_mode": "managed"},
        headers=actor.headers,
    ).json()
    profile = client.post(
        f"/v1/workspaces/{workspace['id']}/model-profiles",
        json={"name": "Matrix Model", "provider_slug": "anthropic", "model_id": "opus"},
        headers=actor.headers,
    ).json()
    connection = client.post(
        f"/v1/projects/{project['id']}/connections",
        json={
            "kind": "grant_tool",
            "source_kind": "agent",
            "source_id": agent["id"],
            "target_kind": "tool",
            "target_id": agent["id"],
            "config": {"tool_name": "web_search", "side_effect": "read"},
        },
        headers=actor.headers,
    ).json()
    # Publishable on purpose: the publish and run rows must answer a
    # permission question, and an empty graph would answer "unpublishable"
    # for every role instead.
    workflow = client.post(
        f"/v1/projects/{project['id']}/workflows",
        json={
            "name": "Matrix Flow",
            "graph": {
                "steps": [
                    {"step_key": "work", "kind": "agent_task", "agent_key": agent["agent_key"]}
                ],
                "edges": [],
            },
        },
        headers=actor.headers,
    ).json()
    workflow_id = workflow["workflow"]["id"]
    published = client.post(
        f"/v1/projects/{project['id']}/workflows/{workflow_id}/publish",
        headers=actor.headers,
    )
    assert published.status_code == 201, published.text
    execution = client.post(
        f"/v1/projects/{project['id']}/workflows/{workflow_id}/run",
        json={},
        headers=actor.headers,
    )
    assert execution.status_code == 202, execution.text

    # The run produced one dispatch. Parked in `needs_review` directly,
    # because the role matrix is about permissions and arranging a crashed
    # worker here would be testing the reaper instead.
    async with owner_database.engine.begin() as conn:
        dispatch_id = (
            await conn.execute(
                text(
                    "UPDATE dispatches SET status = 'needs_review', "
                    "last_error = 'parked for the role matrix' "
                    "WHERE execution_id = :id RETURNING id"
                ),
                {"id": execution.json()["id"]},
            )
        ).scalar_one()
    credential = client.post(
        f"/v1/workspaces/{workspace['id']}/credentials",
        json={
            "name": "Matrix Key",
            "kind": "api_key",
            "provider_slug": "anthropic",
            "secret": "sk-matrix-seeded-0123456789",
        },
        headers=actor.headers,
    ).json()

    return Scenario(
        workspace_id=workspace["id"],
        project_id=project["id"],
        api_key_id=api_key["id"],
        agent_id=agent["id"],
        other_agent_id=other_agent["id"],
        profile_id=profile["id"],
        connection_id=connection["id"],
        workflow_id=workflow_id,
        execution_id=execution.json()["id"],
        dispatch_id=str(dispatch_id),
        credential_id=credential["id"],
        target_user_id=str(target_user_id),
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
        json=scenario.body(endpoint.body),
        headers=actor.headers,
    )

    if role.at_least(endpoint.minimum):
        if endpoint.permitted is not None:
            assert response.status_code == endpoint.permitted, (
                f"{role.value} should reach {endpoint} and meet "
                f"{endpoint.permitted}, got {response.status_code}: {response.text}"
            )
        else:
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
        # The ingest plane authenticates with a key, not a role, so the role
        # matrix has nothing to say about it. `test_ingest.py` covers what
        # does apply: the scope, the project pin and the agent allowlist.
        "POST /v1/ingest/events",
        "POST /v1/ingest/heartbeat",
        "POST /v1/ingest/logs",
        "POST /v1/ingest/metrics",
        "GET /healthz",
        "GET /readyz",
        "GET /v1/meta",
    }
    missing = app_routes - covered - exempt
    assert missing == set(), f"routes with no role-matrix row: {sorted(missing)}"


@pytest.fixture
def app_routes(client: TestClient) -> set[str]:
    """Every route the app serves, in the `METHOD /path` form used above.

    Read from the OpenAPI schema rather than by walking `app.routes`, and
    that is not a detail. FastAPI wraps each `include_router` in an object
    that carries no `path` of its own, so a one-level walk finds only
    `/openapi.json` and `/docs` - both filtered - and returns nothing. This
    fixture did exactly that from Phase 1 until Phase 2 noticed, which made
    the guard below pass unconditionally: `missing` is always empty when
    `app_routes` is. A blind check that reports success is worse than no
    check, because it occupies the place where a real one would go.

    The schema is also the better source on its own merits: it is the
    contract the API publishes, so it cannot drift from what is served.
    """
    schema = client.app.openapi()  # type: ignore[attr-defined]
    routes: set[str] = set()
    for path, operations in schema.get("paths", {}).items():
        if path.startswith(("/openapi", "/docs")):
            continue
        normalised = (
            path.replace("{workspace_id}", "{workspace}")
            .replace("{project_id}", "{project}")
            .replace("{api_key_id}", "{key}")
            .replace("{agent_id}", "{agent}")
            .replace("{connection_id}", "{connection}")
            .replace("{workflow_id}", "{workflow}")
            .replace("{profile_id}", "{profile}")
            .replace("{credential_id}", "{credential}")
            .replace("{execution_id}", "{execution}")
            .replace("{dispatch_id}", "{dispatch}")
            .replace("{user_id}", "{target}")
        )
        for method in operations:
            if method.upper() in ("HEAD", "OPTIONS"):
                continue
            routes.add(f"{method.upper()} {normalised}")

    assert routes, "the schema walk found nothing, so the guard below is blind"
    return routes
