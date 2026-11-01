"""Authenticating with an API key, and the two planes staying apart.

`docs/05-ROADMAP.md` asks for: *"Key verification: wrong secret, revoked,
expired, wrong plane - all 401."* All four are here, plus the key format's
environment label and the allowlist.

Every rejection is checked to be the *same* rejection. A credential endpoint
that distinguishes "revoked" from "never existed" tells an attacker which
prefixes were once real.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain import keys
from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration

KEY_HEADER = "X-AgentWorld-Key"


class Minted:
    def __init__(self, client: TestClient, actor: Actor, **overrides: object) -> None:
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Key Tests"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Key Project"},
            headers=actor.headers,
        ).json()
        body: dict[str, object] = {
            "name": "Worker",
            "scope": "ingest",
            "project_id": self.project["id"],
        }
        body.update(overrides)
        response = client.post(
            f"/v1/workspaces/{self.workspace['id']}/api-keys",
            json=body,
            headers=actor.headers,
        )
        assert response.status_code == 201, response.text
        self.key = response.json()

    @property
    def plaintext(self) -> str:
        return str(self.key["key"])

    @property
    def headers(self) -> dict[str, str]:
        return {KEY_HEADER: self.plaintext}


@pytest.fixture
def minted(client: TestClient, actor: Actor) -> Minted:
    return Minted(client, actor)


# ---------------------------------------------------------------- acceptance ---


def test_a_valid_key_is_accepted(client: TestClient, minted: Minted) -> None:
    response = client.get("/v1/self/key", headers=minted.headers)
    assert response.status_code == 200
    assert response.json()["project_id"] == minted.project["id"]


def test_a_key_pasted_with_whitespace_still_works(client: TestClient, minted: Minted) -> None:
    """Keys get copied out of terminals and YAML files."""
    response = client.get("/v1/self/key", headers={KEY_HEADER: f"  {minted.plaintext}  "})
    assert response.status_code == 200


# ---------------------------------------------------------------- rejections ---


def test_a_missing_key_is_401(client: TestClient) -> None:
    response = client.get("/v1/self/key")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("AgentWorldKey")


def test_a_wrong_secret_is_401(client: TestClient, minted: Minted) -> None:
    """The prefix is real, the secret is not - the most likely real attack."""
    prefix, _, _ = minted.plaintext.partition(".")
    forged = f"{prefix}.{keys.mint('test').secret}"
    assert client.get("/v1/self/key", headers={KEY_HEADER: forged}).status_code == 401


def test_an_unknown_prefix_is_401(client: TestClient, minted: Minted) -> None:
    unknown = keys.mint("test")
    assert client.get("/v1/self/key", headers={KEY_HEADER: unknown.plaintext}).status_code == 401


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "   ",
        "garbage",
        "aw_test_abc",
        "aw_test_abcdef123456",
        "aw_live_abcdef123456.secretsecretsecretsecret",
    ],
)
def test_a_malformed_key_is_401(client: TestClient, malformed: str) -> None:
    assert client.get("/v1/self/key", headers={KEY_HEADER: malformed}).status_code == 401


def test_a_revoked_key_is_401(client: TestClient, actor: Actor, minted: Minted) -> None:
    revoke = client.post(f"/v1/api-keys/{minted.key['id']}/revoke", headers=actor.headers)
    assert revoke.status_code == 200
    assert revoke.json()["revoked_at"] is not None

    assert client.get("/v1/self/key", headers=minted.headers).status_code == 401


def test_a_deleted_key_is_401(client: TestClient, actor: Actor, minted: Minted) -> None:
    assert (
        client.delete(f"/v1/api-keys/{minted.key['id']}", headers=actor.headers).status_code == 204
    )
    assert client.get("/v1/self/key", headers=minted.headers).status_code == 401


async def test_an_expired_key_is_401(
    client: TestClient, minted: Minted, owner_database: Database
) -> None:
    """Expiry is evaluated by the database's clock, not the API process's.

    Set directly rather than through the API, which refuses to mint a key
    that is already expired.
    """
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE api_keys SET expires_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": uuid.UUID(minted.key["id"])},
        )
    assert client.get("/v1/self/key", headers=minted.headers).status_code == 401


def test_a_key_expiring_in_the_future_still_works(client: TestClient, actor: Actor) -> None:
    """So that the test above is about expiry and not about the field."""
    future = Minted(client, actor, expires_at="2099-01-01T00:00:00Z")
    assert client.get("/v1/self/key", headers=future.headers).status_code == 200


def test_a_key_from_another_environment_is_401(client: TestClient, minted: Minted) -> None:
    """A `live` key presented to a `test` server, and the reverse.

    Refused rather than ignored: the two environments should never share
    credentials, and silently accepting one hides a misconfiguration until it
    matters.
    """
    prefix, _, secret = minted.plaintext.partition(".")
    relabelled = f"{prefix.replace('aw_test_', 'aw_live_')}.{secret}"
    assert client.get("/v1/self/key", headers={KEY_HEADER: relabelled}).status_code == 401


def test_every_rejection_looks_identical(client: TestClient, actor: Actor, minted: Minted) -> None:
    """Revoked, forged, unknown and malformed must be one answer.

    Only `instance` may differ, since it is the per-request trace id.
    """
    prefix, _, _ = minted.plaintext.partition(".")
    bodies = []
    for presented in (
        f"{prefix}.{keys.mint('test').secret}",  # wrong secret
        keys.mint("test").plaintext,  # unknown prefix
        "garbage",  # malformed
    ):
        response = client.get("/v1/self/key", headers={KEY_HEADER: presented})
        assert response.status_code == 401
        bodies.append({k: v for k, v in response.json().items() if k != "instance"})

    client.post(f"/v1/api-keys/{minted.key['id']}/revoke", headers=actor.headers)
    revoked = client.get("/v1/self/key", headers=minted.headers)
    bodies.append({k: v for k, v in revoked.json().items() if k != "instance"})

    assert all(body == bodies[0] for body in bodies), f"rejections differ: {bodies}"


# -------------------------------------------------------------- the two planes ---


def test_a_user_token_is_rejected_on_the_ingest_plane(client: TestClient, actor: Actor) -> None:
    response = client.get("/v1/self/key", headers=actor.headers)
    assert response.status_code == 401
    assert "API key" in response.json()["detail"]


def test_an_api_key_is_rejected_on_the_control_plane(client: TestClient, minted: Minted) -> None:
    """Rejected, not downgraded to anonymous.

    `docs/03-API.md` section 1: mixing the planes is the single most likely
    source of a privilege bug, so a credential for the wrong plane is an
    error rather than something to ignore.
    """
    for path in ("/v1/me", "/v1/workspaces", f"/v1/projects/{minted.project['id']}"):
        response = client.get(path, headers=minted.headers)
        assert response.status_code == 401, path
        assert "user token" in response.json()["detail"], path


def test_presenting_both_credentials_is_rejected_on_both_planes(
    client: TestClient, actor: Actor, minted: Minted
) -> None:
    """Ambiguity is refused rather than resolved by precedence.

    A client sending both has made a mistake, and picking one silently would
    make which plane it landed on depend on the route rather than the intent.
    """
    both = {**actor.headers, **minted.headers}
    assert client.get("/v1/me", headers=both).status_code == 401
    assert client.get("/v1/self/key", headers=both).status_code == 401


# --------------------------------------------------------------- key metadata ---


def test_an_ingest_key_must_be_pinned_to_a_project(client: TestClient, actor: Actor) -> None:
    workspace = client.post(
        "/v1/workspaces", json={"name": "Unpinned"}, headers=actor.headers
    ).json()
    response = client.post(
        f"/v1/workspaces/{workspace['id']}/api-keys",
        json={"name": "Unpinned", "scope": "ingest"},
        headers=actor.headers,
    )
    assert response.status_code == 422
    assert "project" in response.json()["detail"]


def test_a_control_key_needs_no_project(client: TestClient, actor: Actor) -> None:
    workspace = client.post(
        "/v1/workspaces", json={"name": "Control"}, headers=actor.headers
    ).json()
    response = client.post(
        f"/v1/workspaces/{workspace['id']}/api-keys",
        json={"name": "CI", "scope": "control"},
        headers=actor.headers,
    )
    assert response.status_code == 201
    assert response.json()["project_id"] is None


def test_a_key_for_another_workspaces_project_is_404(
    client: TestClient, actor: Actor, make_actor: object
) -> None:
    """Minting a key pinned to a project you cannot see must not succeed."""
    first = Minted(client, actor)
    other_workspace = client.post(
        "/v1/workspaces", json={"name": "Second Workspace"}, headers=actor.headers
    ).json()
    response = client.post(
        f"/v1/workspaces/{other_workspace['id']}/api-keys",
        json={"name": "Crossed", "scope": "ingest", "project_id": first.project["id"]},
        headers=actor.headers,
    )
    assert response.status_code == 404


def test_the_allowlist_is_reported_to_the_key_itself(client: TestClient, actor: Actor) -> None:
    pinned = Minted(client, actor, agent_key_allowlist=["etl-worker-eu-*"])
    identity = client.get("/v1/self/key", headers=pinned.headers).json()
    assert identity["agent_key_allowlist"] == ["etl-worker-eu-*"]


@pytest.mark.parametrize("allowlist", [[], ["etl-*-eu"], ["  "], ["a*b"], ["a*b*"]])
def test_an_unusable_allowlist_is_refused(
    client: TestClient, actor: Actor, allowlist: list[str]
) -> None:
    """A pattern that looks like a wildcard and behaves like a literal is the
    worst failure mode for a security control: it appears to permit a family
    of agents and in fact permits none."""
    workspace = client.post(
        "/v1/workspaces", json={"name": "Allowlists"}, headers=actor.headers
    ).json()
    project = client.post(
        f"/v1/workspaces/{workspace['id']}/projects",
        json={"name": "Allowlist Project"},
        headers=actor.headers,
    ).json()
    response = client.post(
        f"/v1/workspaces/{workspace['id']}/api-keys",
        json={
            "name": "Patterned",
            "scope": "ingest",
            "project_id": project["id"],
            "agent_key_allowlist": allowlist,
        },
        headers=actor.headers,
    )
    assert response.status_code == 422, response.text


async def test_using_a_key_records_when_it_was_last_used(
    client: TestClient, minted: Minted, owner_database: Database
) -> None:
    async with owner_database.session() as session:
        before = (
            await session.execute(
                text("SELECT last_used_at FROM api_keys WHERE id = :id"),
                {"id": uuid.UUID(minted.key["id"])},
            )
        ).scalar_one()
    assert before is None

    client.get("/v1/self/key", headers=minted.headers)

    async with owner_database.session() as session:
        after = (
            await session.execute(
                text("SELECT last_used_at FROM api_keys WHERE id = :id"),
                {"id": uuid.UUID(minted.key["id"])},
            )
        ).scalar_one()
    assert after is not None


async def test_minting_a_key_is_audited_with_its_prefix_and_never_its_secret(
    client: TestClient, minted: Minted, owner_database: Database
) -> None:
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT action, metadata::text FROM audit_log "
                    "WHERE workspace_id = :workspace_id ORDER BY id"
                ),
                {"workspace_id": uuid.UUID(minted.workspace["id"])},
            )
        ).all()

    actions = [row[0] for row in rows]
    assert "api_key.create" in actions

    _, _, secret = minted.plaintext.partition(".")
    for _, metadata in rows:
        assert secret not in metadata
    mint_metadata = next(metadata for action, metadata in rows if action == "api_key.create")
    assert minted.key["prefix"] in mint_metadata


def test_a_bare_star_is_accepted_and_means_any_agent(client: TestClient, actor: Actor) -> None:
    """`["*"]` is allowed, unlike the patterns above.

    The validator exists to refuse patterns that *look* like wildcards and
    behave like literals. A bare star behaves exactly as it reads - a prefix
    match on the empty string, so every agent key - which makes it an honest,
    if redundant, way of writing "any".
    """
    starred = Minted(client, actor, agent_key_allowlist=["*"])
    identity = client.get("/v1/self/key", headers=starred.headers).json()
    assert identity["agent_key_allowlist"] == ["*"]
