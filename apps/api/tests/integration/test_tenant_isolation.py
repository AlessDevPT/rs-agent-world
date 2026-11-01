"""Tenant isolation at the HTTP boundary.

`docs/02-DATABASE.md` section 7: *"A conformance test calls every
control-plane endpoint with a foreign resource id and asserts 404 (not 403:
existence is itself information)."* This is that test.

Two tenants are set up with no overlap, and then every endpoint that takes an
id is called by the wrong tenant. 403 would be a failure here, not a pass: it
would confirm to a stranger that the resource exists.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import Actor

pytestmark = pytest.mark.integration


class Tenant:
    """One tenant's actor and the ids of everything they own."""

    def __init__(self, client: TestClient, actor: Actor, label: str) -> None:
        self.actor = actor
        self.workspace = client.post(
            "/v1/workspaces", json={"name": f"Tenant {label}"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": f"Project {label}"},
            headers=actor.headers,
        ).json()
        self.api_key = client.post(
            f"/v1/workspaces/{self.workspace['id']}/api-keys",
            json={
                "name": f"Key {label}",
                "scope": "ingest",
                "project_id": self.project["id"],
            },
            headers=actor.headers,
        ).json()

        for created in (self.workspace, self.project, self.api_key):
            assert "id" in created, f"setup failed: {created}"


@pytest.fixture
def tenants(client: TestClient, make_actor: Any) -> tuple[Tenant, Tenant]:
    return (
        Tenant(client, make_actor("alice@example.com"), "A"),
        Tenant(client, make_actor("bob@example.com"), "B"),
    )


#: `{workspace}`, `{project}` and `{key}` are substituted with tenant A's ids,
#: and every request is then made by tenant B.
FOREIGN_REQUESTS = [
    ("GET", "/v1/workspaces/{workspace}", None),
    ("PATCH", "/v1/workspaces/{workspace}", {"name": "Hijacked"}),
    ("DELETE", "/v1/workspaces/{workspace}", None),
    ("GET", "/v1/workspaces/{workspace}/members", None),
    ("POST", "/v1/workspaces/{workspace}/members", {"email": "bob@example.com"}),
    ("GET", "/v1/workspaces/{workspace}/projects", None),
    ("POST", "/v1/workspaces/{workspace}/projects", {"name": "Intruding Project"}),
    ("GET", "/v1/projects/{project}", None),
    ("PATCH", "/v1/projects/{project}", {"name": "Hijacked"}),
    ("DELETE", "/v1/projects/{project}", None),
    ("GET", "/v1/workspaces/{workspace}/api-keys", None),
    ("POST", "/v1/workspaces/{workspace}/api-keys", {"name": "Intruding Key", "scope": "control"}),
    ("POST", "/v1/api-keys/{key}/revoke", None),
    ("DELETE", "/v1/api-keys/{key}", None),
]


@pytest.mark.parametrize(
    ("method", "template", "body"), FOREIGN_REQUESTS, ids=lambda value: str(value)
)
def test_a_foreign_id_is_404_on_every_endpoint(
    client: TestClient,
    tenants: tuple[Tenant, Tenant],
    method: str,
    template: str,
    body: dict[str, Any] | None,
) -> None:
    owner, intruder = tenants
    path = template.format(
        workspace=owner.workspace["id"],
        project=owner.project["id"],
        key=owner.api_key["id"],
    )
    response = client.request(method, path, json=body, headers=intruder.actor.headers)

    assert response.status_code == 404, (
        f"{method} {template} leaked existence with {response.status_code}: {response.text}"
    )
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    ("method", "template", "body"), FOREIGN_REQUESTS, ids=lambda value: str(value)
)
def test_the_owner_can_do_all_of_the_same_things(
    client: TestClient,
    tenants: tuple[Tenant, Tenant],
    method: str,
    template: str,
    body: dict[str, Any] | None,
) -> None:
    """The mirror image, and the reason the test above means something.

    Without this, every 404 could be explained by a broken route rather than
    by isolation working.
    """
    owner, _ = tenants
    path = template.format(
        workspace=owner.workspace["id"],
        project=owner.project["id"],
        key=owner.api_key["id"],
    )
    if "members" in template and method == "POST":
        # The owner cannot add themselves; aim at the other tenant's user,
        # who exists and is not a member here.
        body = {"email": "bob@example.com"}

    response = client.request(method, path, json=body, headers=owner.actor.headers)
    assert response.status_code < 400, (
        f"{method} {template}: {response.status_code} {response.text}"
    )


def test_a_random_id_is_also_404(client: TestClient, tenants: tuple[Tenant, Tenant]) -> None:
    """An id that belongs to nobody must be indistinguishable from one that
    belongs to someone else. If the two differed, the difference itself would
    be an existence oracle."""
    _, intruder = tenants
    for path in (
        f"/v1/workspaces/{uuid.uuid4()}",
        f"/v1/projects/{uuid.uuid4()}",
        f"/v1/workspaces/{uuid.uuid4()}/projects",
    ):
        assert client.get(path, headers=intruder.actor.headers).status_code == 404


def test_a_foreign_and_a_nonexistent_id_give_identical_responses(
    client: TestClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """Same status, same problem type, same detail - differing only in the
    request id, which is per-request by design."""
    owner, intruder = tenants
    foreign = client.get(
        f"/v1/workspaces/{owner.workspace['id']}", headers=intruder.actor.headers
    ).json()
    absent = client.get(f"/v1/workspaces/{uuid.uuid4()}", headers=intruder.actor.headers).json()

    assert {k: v for k, v in foreign.items() if k != "instance"} == {
        k: v for k, v in absent.items() if k != "instance"
    }


def test_listings_never_include_another_tenant(
    client: TestClient, tenants: tuple[Tenant, Tenant]
) -> None:
    owner, intruder = tenants

    workspaces = client.get("/v1/workspaces", headers=intruder.actor.headers).json()
    assert [entry["workspace"]["id"] for entry in workspaces] == [intruder.workspace["id"]]

    projects = client.get(
        f"/v1/workspaces/{intruder.workspace['id']}/projects", headers=intruder.actor.headers
    ).json()
    assert [project["id"] for project in projects] == [intruder.project["id"]]

    keys = client.get(
        f"/v1/workspaces/{intruder.workspace['id']}/api-keys", headers=intruder.actor.headers
    ).json()
    assert [key["id"] for key in keys] == [intruder.api_key["id"]]

    me = client.get("/v1/me", headers=intruder.actor.headers).json()
    assert [entry["workspace"]["id"] for entry in me["workspaces"]] == [intruder.workspace["id"]]
    assert owner.workspace["id"] not in str(me)
