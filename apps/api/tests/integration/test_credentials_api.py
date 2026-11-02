"""Credentials: the five things you can do, and the one thing nobody can.

The positive path is short. Most of this file is about what cannot happen,
and the ordering is deliberate: the HTTP surface first, because that is what
an attacker reaches, then the material itself, because that is what they are
reaching for.

The AAD test is the one worth reading. It does not ask a service to refuse -
it moves a row between workspaces as the *schema owner*, below row-level
security, below the repository's tenant scope and below `authorize`, which is
every control the application has. The row still cannot be read, because the
workspace it was written in is covered by the GCM tag and is not in the row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain.enums import CredentialKind, MemberRole
from agentworld.domain.errors import CredentialUnreadableError, NotFoundError
from agentworld.domain.principals import UserPrincipal
from agentworld.infra import secrets
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.credentials import CredentialService
from agentworld.settings import Settings
from helpers import Actor, FakeAuthProvider, seed_user

pytestmark = pytest.mark.integration

#: Long enough to earn a hint, and distinctive enough to grep for.
SECRET = "sk-ant-api03-ZZtestmaterialZZ-9f3c"
ROTATED = "sk-ant-api03-QQrotatedmaterialQQ-77ab"

#: Property names that must not appear anywhere in the published schema for a
#: credential. Taken from the acceptance criterion in
#: `docs/proposals/.../01-DOMAIN-AND-PROTOCOL.md` section 6.
FORBIDDEN_PROPERTIES = frozenset(
    {"ciphertext", "nonce", "wrapped_key", "secret", "token", "password", "key"}
)


@pytest.fixture
def workspace_id(client: TestClient, actor: Actor) -> str:
    response = client.post(
        "/v1/workspaces", json={"name": "Credential Workspace"}, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _create(
    client: TestClient, actor: Actor, workspace_id: str, **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Anthropic production",
        "kind": "api_key",
        "provider_slug": "anthropic",
        "secret": SECRET,
    }
    body.update(overrides)
    response = client.post(
        f"/v1/workspaces/{workspace_id}/credentials", json=body, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _principal(actor: Actor, workspace_id: str) -> UserPrincipal:
    """The actor as the services see them, for the calls with no route.

    `rewrap_workspace` and `use` are deliberately unreachable over HTTP, so
    the tests that cover them build the principal a request would have built.
    """
    return UserPrincipal(
        user_id=actor.user_id,
        email=actor.email,
        memberships={uuid.UUID(workspace_id): MemberRole.OWNER},
    )


def _service(factory: UnitOfWorkFactory, keyring: secrets.Keyring) -> CredentialService:
    """A service over a keyring the caller names.

    Never a default. The app under test configures its keyring from
    `Settings`, so a test that assumed the development one would encrypt
    under a different key from the server on any machine with real
    `CREDENTIAL_KEKS` in its `.env` - and fail for a reason unrelated to
    what it asserts. Tests that want the server's keyring ask the `settings`
    fixture for it; the rotation tests build their own.
    """
    return CredentialService(factory, store=secrets.EnvelopeSecretStore(keyring))


async def _material_row(owner_database: Database, credential_id: str) -> Any:
    """The raw columns, read as the schema owner so RLS cannot hide them."""
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT ciphertext, nonce, wrapped_key, key_id, aad_version, deleted_at "
                    "FROM credentials WHERE id = :id"
                ),
                {"id": credential_id},
            )
        ).one()


# ------------------------------------------------------------- the happy path ---


def test_a_credential_is_stored_and_listed_without_its_material(
    client: TestClient, actor: Actor, workspace_id: str, settings: Settings
) -> None:
    created = _create(client, actor, workspace_id)

    assert created["name"] == "Anthropic production"
    assert created["provider_slug"] == "anthropic"
    assert created["key_id"] == settings.credential_keyring.active_id
    assert created["is_usable"] is True
    # Derived, and only the last four characters of it.
    assert created["display_hint"] == "…9f3c"
    assert SECRET not in json.dumps(created)

    listed = client.get(f"/v1/workspaces/{workspace_id}/credentials", headers=actor.headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [created["id"]]
    assert SECRET not in listed.text


def test_the_hint_is_derived_and_cannot_be_supplied(
    client: TestClient, actor: Actor, workspace_id: str
) -> None:
    """A caller-supplied hint is a field through which the whole secret fits.

    Refused rather than ignored: a client that believes it set a hint and
    did not would show its users something that is not there.
    """
    response = client.post(
        f"/v1/workspaces/{workspace_id}/credentials",
        json={
            "name": "With a hint",
            "kind": "api_key",
            "provider_slug": "anthropic",
            "secret": SECRET,
            "display_hint": SECRET,
        },
        headers=actor.headers,
    )
    assert response.status_code == 422
    assert SECRET not in response.text


@pytest.mark.parametrize("kind", ["basic", "oauth2", "custom"])
def test_only_api_key_material_earns_a_hint(
    client: TestClient, actor: Actor, workspace_id: str, kind: str
) -> None:
    """Four trailing characters of a password are worth more than of a token.

    `basic` material is or contains something a human chose, `oauth2`
    material's last characters belong to whichever field was serialised
    last, and `custom` is unknown. None of those is worth four characters.
    """
    created = _create(client, actor, workspace_id, kind=kind, name=f"A {kind} credential")
    assert created["display_hint"] is None


def test_material_too_short_for_a_hint_gets_none(
    client: TestClient, actor: Actor, workspace_id: str
) -> None:
    created = _create(client, actor, workspace_id, secret="short-one", name="Short")
    assert created["display_hint"] is None


def test_a_duplicate_name_for_one_provider_is_a_conflict(
    client: TestClient, actor: Actor, workspace_id: str
) -> None:
    _create(client, actor, workspace_id)
    response = client.post(
        f"/v1/workspaces/{workspace_id}/credentials",
        json={
            "name": "Anthropic production",
            "kind": "api_key",
            "provider_slug": "anthropic",
            "secret": SECRET,
        },
        headers=actor.headers,
    )
    assert response.status_code == 409
    assert SECRET not in response.text


def test_a_credential_for_a_project_in_another_workspace_is_refused(
    client: TestClient, actor: Actor, workspace_id: str
) -> None:
    other = client.post("/v1/workspaces", json={"name": "Elsewhere"}, headers=actor.headers).json()
    project = client.post(
        f"/v1/workspaces/{other['id']}/projects",
        json={"name": "Not here"},
        headers=actor.headers,
    ).json()
    response = client.post(
        f"/v1/workspaces/{workspace_id}/credentials",
        json={
            "name": "Cross wired",
            "kind": "api_key",
            "provider_slug": "anthropic",
            "secret": SECRET,
            "project_id": project["id"],
        },
        headers=actor.headers,
    )
    assert response.status_code == 404


# ------------------------------------------------------ no endpoint returns it ---


def test_no_credential_schema_has_a_property_for_material(client: TestClient) -> None:
    """The acceptance criterion from the design document, enforced.

    Walks the published OpenAPI document rather than the code: the schema is
    the contract, so a field added to a response model shows up here even if
    nobody remembered this test existed.
    """
    schema = client.app.openapi()  # type: ignore[attr-defined]
    components: dict[str, Any] = schema.get("components", {}).get("schemas", {})

    views = {
        name: definition
        for name, definition in components.items()
        if name.startswith("Credential") and not name.endswith(("Create", "Rotate"))
    }
    assert views, "the walk found no credential response schema, so this test is blind"

    for name, definition in views.items():
        offending = FORBIDDEN_PROPERTIES & set(definition.get("properties", {}))
        assert offending == set(), f"{name} exposes {sorted(offending)}"


def test_there_is_no_route_that_reads_a_credential(client: TestClient) -> None:
    """No `GET /credentials/{id}/secret`, under any spelling.

    The absence is structural - there is no `credential.read_secret` action
    for a route to authorize against - and this is the assertion that it
    stays absent.
    """
    schema = client.app.openapi()  # type: ignore[attr-defined]
    credential_paths = [path for path in schema["paths"] if "credentials" in path]
    assert credential_paths, "the walk found no credential routes, so this test is blind"
    for path in credential_paths:
        assert not any(word in path for word in ("secret", "reveal", "material", "decrypt"))


# ----------------------------------------------------------- tenant isolation ---


async def test_a_non_member_cannot_see_or_create_credentials(
    client: TestClient,
    actor: Actor,
    auth: FakeAuthProvider,
    owner_database: Database,
    workspace_id: str,
) -> None:
    """404, not 403: the workspace's existence is not theirs to learn."""
    _create(client, actor, workspace_id)
    outsider = Actor(auth, "outsider@example.com")
    await seed_user(owner_database, outsider.user_id, outsider.email)

    listed = client.get(f"/v1/workspaces/{workspace_id}/credentials", headers=outsider.headers)
    assert listed.status_code == 404
    created = client.post(
        f"/v1/workspaces/{workspace_id}/credentials",
        json={
            "name": "Theirs now",
            "kind": "api_key",
            "provider_slug": "anthropic",
            "secret": SECRET,
        },
        headers=outsider.headers,
    )
    assert created.status_code == 404


async def test_a_row_copied_into_another_workspace_cannot_be_decrypted(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    workspace_id: str,
    settings: Settings,
) -> None:
    """The last layer, exercised with every layer above it bypassed.

    The copy is made as the schema owner: below row-level security, below the
    repository's tenant scope, below `authorize`. Then the credential is read
    through the service in its new home, by a caller who is legitimately an
    admin there. It fails, and it fails in the crypto rather than in a check
    - which is the difference between an isolation property and a code path
    somebody could forget to call.
    """
    created = _create(client, actor, workspace_id)
    other = client.post("/v1/workspaces", json={"name": "Attacker"}, headers=actor.headers).json()
    stolen_id = uuid.uuid4()

    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO credentials (id, workspace_id, name, kind, provider_slug, "
                "ciphertext, nonce, wrapped_key, key_id, aad_version) "
                "SELECT :new_id, :new_workspace, name, kind, provider_slug, "
                "ciphertext, nonce, wrapped_key, key_id, aad_version "
                "FROM credentials WHERE id = :source"
            ),
            {
                "new_id": stolen_id,
                "new_workspace": uuid.UUID(other["id"]),
                "source": uuid.UUID(created["id"]),
            },
        )

    service = _service(factory, settings.credential_keyring)
    # In its own workspace it reads fine, which is what makes the failure
    # below a statement about the AAD rather than about a broken fixture.
    assert (
        await service.use(
            workspace_id=uuid.UUID(workspace_id), credential_id=uuid.UUID(created["id"])
        )
        == SECRET
    )
    with pytest.raises(CredentialUnreadableError):
        await service.use(workspace_id=uuid.UUID(other["id"]), credential_id=stolen_id)


async def test_the_same_row_id_in_the_wrong_workspace_is_also_refused(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    workspace_id: str,
    settings: Settings,
) -> None:
    """Keeping the id does not help either: the workspace is in the AAD too."""
    created = _create(client, actor, workspace_id)
    other = client.post("/v1/workspaces", json={"name": "Attacker"}, headers=actor.headers).json()

    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE credentials SET workspace_id = :new WHERE id = :id"),
            {"new": uuid.UUID(other["id"]), "id": uuid.UUID(created["id"])},
        )

    with pytest.raises(CredentialUnreadableError):
        await _service(factory, settings.credential_keyring).use(
            workspace_id=uuid.UUID(other["id"]), credential_id=uuid.UUID(created["id"])
        )


# ------------------------------------------------------------- rotation of all ---


async def test_rotating_the_kek_rewraps_every_key_and_changes_no_ciphertext(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    workspace_id: str,
) -> None:
    """The acceptance criterion: re-wrapped, no plaintext, no ciphertext change.

    Written through a service holding only the old KEK, so the rotation is
    doing real work; read back afterwards through a service holding only the
    *new* KEK, so a row that was not re-wrapped would be unreadable rather
    than quietly fine.
    """
    old = secrets.Keyring(keys={"k2026a": bytes(range(32))}, active_id="k2026a")
    both = secrets.Keyring(
        keys={"k2026a": bytes(range(32)), "k2026b": bytes(range(32, 64))},
        active_id="k2026b",
    )
    new_only = secrets.Keyring(keys={"k2026b": bytes(range(32, 64))}, active_id="k2026b")

    principal = _principal(actor, workspace_id)
    before = _service(factory, old)
    first = await before.create(
        principal,
        CallContext.background(),
        uuid.UUID(workspace_id),
        name="First",
        kind=CredentialKind.API_KEY,
        provider_slug="anthropic",
        secret=SECRET,
    )
    second = await before.create(
        principal,
        CallContext.background(),
        uuid.UUID(workspace_id),
        name="Second",
        kind=CredentialKind.API_KEY,
        provider_slug="openai",
        secret=ROTATED,
    )

    original = {
        str(credential.id): await _material_row(owner_database, str(credential.id))
        for credential in (first, second)
    }

    summary = await _service(factory, both).rewrap_workspace(principal, uuid.UUID(workspace_id))
    assert (summary.examined, summary.rewrapped, summary.unreadable) == (2, 2, 0)

    after = _service(factory, new_only)
    for credential, expected in ((first, SECRET), (second, ROTATED)):
        row = await _material_row(owner_database, str(credential.id))
        was = original[str(credential.id)]
        assert row.key_id == "k2026b"
        assert row.wrapped_key != was.wrapped_key
        # The two columns a rotation must never touch, because it never has
        # the plaintext to rebuild them from.
        assert row.ciphertext == was.ciphertext
        assert row.nonce == was.nonce
        assert (
            await after.use(workspace_id=uuid.UUID(workspace_id), credential_id=credential.id)
            == expected
        )


async def test_a_second_rotation_pass_writes_nothing(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    workspace_id: str,
    settings: Settings,
) -> None:
    """Idempotent, so an interrupted rotation can simply be run again."""
    principal = _principal(actor, workspace_id)
    _create(client, actor, workspace_id)
    service = _service(factory, settings.credential_keyring)

    assert (await service.rewrap_workspace(principal, uuid.UUID(workspace_id))).rewrapped == 0
    second = await service.rewrap_workspace(principal, uuid.UUID(workspace_id))
    assert (second.examined, second.rewrapped, second.unreadable) == (1, 0, 0)


async def test_a_row_under_a_retired_key_is_reported_not_fatal(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    workspace_id: str,
) -> None:
    """One unrecoverable row must not pin every other row to the old key."""
    principal = _principal(actor, workspace_id)
    _create(client, actor, workspace_id)

    unrelated = secrets.Keyring(
        keys={"k2026b": bytes(range(32, 64))},
        active_id="k2026b",
    )
    summary = await _service(factory, unrelated).rewrap_workspace(
        principal, uuid.UUID(workspace_id)
    )
    assert (summary.examined, summary.rewrapped, summary.unreadable) == (1, 0, 1)


# --------------------------------------------------- rotate, revoke and delete ---


async def test_rotating_material_keeps_the_id_and_replaces_the_secret(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    workspace_id: str,
    settings: Settings,
) -> None:
    created = _create(client, actor, workspace_id)
    response = client.post(
        f"/v1/workspaces/{workspace_id}/credentials/{created['id']}/rotate",
        json={"secret": ROTATED},
        headers=actor.headers,
    )
    assert response.status_code == 200, response.text
    rotated = response.json()
    assert rotated["id"] == created["id"]
    assert rotated["display_hint"] == "…77ab"
    assert ROTATED not in response.text

    assert (
        await _service(factory, settings.credential_keyring).use(
            workspace_id=uuid.UUID(workspace_id), credential_id=uuid.UUID(created["id"])
        )
        == ROTATED
    )


async def test_revoking_stops_use_but_keeps_the_material(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    workspace_id: str,
    settings: Settings,
) -> None:
    """Revocation is the reversible one, and this is what reversible means."""
    created = _create(client, actor, workspace_id)
    response = client.post(
        f"/v1/workspaces/{workspace_id}/credentials/{created['id']}/revoke",
        headers=actor.headers,
    )
    assert response.status_code == 200
    assert response.json()["is_usable"] is False

    with pytest.raises(NotFoundError):
        await _service(factory, settings.credential_keyring).use(
            workspace_id=uuid.UUID(workspace_id), credential_id=uuid.UUID(created["id"])
        )

    row = await _material_row(owner_database, created["id"])
    assert row.ciphertext != b""

    # Gone from the default listing, visible when asked for.
    assert (
        client.get(f"/v1/workspaces/{workspace_id}/credentials", headers=actor.headers).json() == []
    )
    with_revoked = client.get(
        f"/v1/workspaces/{workspace_id}/credentials?include_revoked=true",
        headers=actor.headers,
    )
    assert [row["id"] for row in with_revoked.json()] == [created["id"]]


async def test_rotating_a_revoked_credential_leaves_it_revoked(
    client: TestClient, actor: Actor, workspace_id: str
) -> None:
    """Rotation replaces material. It is not a way to undo a revocation."""
    created = _create(client, actor, workspace_id)
    client.post(
        f"/v1/workspaces/{workspace_id}/credentials/{created['id']}/revoke",
        headers=actor.headers,
    )
    response = client.post(
        f"/v1/workspaces/{workspace_id}/credentials/{created['id']}/rotate",
        json={"secret": ROTATED},
        headers=actor.headers,
    )
    assert response.status_code == 200
    assert response.json()["is_usable"] is False


async def test_deleting_destroys_the_material_and_keeps_the_row(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    workspace_id: str,
    settings: Settings,
) -> None:
    """Irreversible, and the row survives for the audit trail's sake."""
    created = _create(client, actor, workspace_id)
    response = client.request(
        "DELETE",
        f"/v1/workspaces/{workspace_id}/credentials/{created['id']}",
        headers=actor.headers,
    )
    assert response.status_code == 204

    row = await _material_row(owner_database, created["id"])
    assert row.deleted_at is not None
    assert (row.ciphertext, row.nonce, row.wrapped_key) == (b"", b"", b"")

    with pytest.raises(NotFoundError):
        await _service(factory, settings.credential_keyring).use(
            workspace_id=uuid.UUID(workspace_id), credential_id=uuid.UUID(created["id"])
        )
    assert (
        client.get(
            f"/v1/workspaces/{workspace_id}/credentials?include_revoked=true",
            headers=actor.headers,
        ).json()
        == []
    )


async def test_a_tombstone_is_not_rotated_back_to_life(
    client: TestClient, actor: Actor, workspace_id: str
) -> None:
    created = _create(client, actor, workspace_id)
    client.request(
        "DELETE",
        f"/v1/workspaces/{workspace_id}/credentials/{created['id']}",
        headers=actor.headers,
    )
    response = client.post(
        f"/v1/workspaces/{workspace_id}/credentials/{created['id']}/rotate",
        json={"secret": ROTATED},
        headers=actor.headers,
    )
    assert response.status_code == 404


async def test_using_a_credential_records_when(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    workspace_id: str,
    settings: Settings,
) -> None:
    """ "When was this last used" is the first question when retiring a key."""
    created = _create(client, actor, workspace_id)
    assert created["last_used_at"] is None

    await _service(factory, settings.credential_keyring).use(
        workspace_id=uuid.UUID(workspace_id), credential_id=uuid.UUID(created["id"])
    )
    listed = client.get(f"/v1/workspaces/{workspace_id}/credentials", headers=actor.headers).json()
    assert listed[0]["last_used_at"] is not None


# ------------------------------------------------------------------- no leaks ---


async def test_the_material_reaches_no_response_log_or_audit_row(
    client: TestClient,
    actor: Actor,
    owner_database: Database,
    workspace_id: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The acceptance criterion: not in a response, a log line or a problem.

    Four kinds of failure are provoked, because the dangerous moment for a
    secret is the error path - the code nobody reads, on the request nobody
    expected. Then every place the material could have landed is checked:
    the bodies the server sent, the rows it wrote, and the lines it printed.
    """
    created = _create(client, actor, workspace_id)
    bodies = [json.dumps(created)]

    # A conflict, a validation failure, a missing row, and a bad reference.
    bodies.append(
        client.post(
            f"/v1/workspaces/{workspace_id}/credentials",
            json={
                "name": "Anthropic production",
                "kind": "api_key",
                "provider_slug": "anthropic",
                "secret": SECRET,
            },
            headers=actor.headers,
        ).text
    )
    bodies.append(
        client.post(
            f"/v1/workspaces/{workspace_id}/credentials",
            json={
                "name": "Too big",
                "kind": "api_key",
                "provider_slug": "anthropic",
                "secret": SECRET * 1000,
            },
            headers=actor.headers,
        ).text
    )
    bodies.append(
        client.post(
            f"/v1/workspaces/{workspace_id}/credentials/{uuid.uuid4()}/rotate",
            json={"secret": SECRET},
            headers=actor.headers,
        ).text
    )
    bodies.append(
        client.post(
            f"/v1/workspaces/{workspace_id}/credentials",
            json={
                "name": "Bad project",
                "kind": "api_key",
                "provider_slug": "anthropic",
                "secret": SECRET,
                "project_id": str(uuid.uuid4()),
            },
            headers=actor.headers,
        ).text
    )

    for body in bodies:
        assert SECRET not in body
        # The hint is four characters by design; the rest must not be there.
        assert SECRET[:-4] not in body

    async with owner_database.session() as session:
        written = await session.execute(
            text("SELECT action, metadata::text FROM audit_log ORDER BY created_at")
        )
        rows: Sequence[Any] = written.all()
        appended = (
            await session.execute(
                text("SELECT coalesce(string_agg(payload::text, ' '), '') FROM events")
            )
        ).scalar_one()

    actions = [row.action for row in rows]
    # Guard on the guard: the workspace's own creation is in here too, and
    # if the credential write had left no audit row the grep below would be
    # checking nothing.
    assert "credential.create" in actions, "the audit check is blind"
    for row in rows:
        assert SECRET not in row.metadata
    assert SECRET not in appended

    printed = capsys.readouterr().out
    # Guard on the guard: if nothing was captured, the grep below proves
    # nothing. The request logger emits one line per request.
    assert "credential" in printed, "no log output was captured, so this check is blind"
    assert SECRET not in printed
    assert SECRET[:-4] not in printed
