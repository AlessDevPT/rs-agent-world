"""Envelope encryption, and the keyring it draws its keys from.

The tests that matter here are the negative ones. That a round trip works is
one line; what the design is actually for is that a round trip *fails* when
anything about where the row lives has changed, and each of those cases gets
a test of its own because each is a different attack:

- the same row read for the wrong workspace (a bad restore, an injection that
  got as far as `INSERT ... SELECT`);
- the same row read under the wrong credential id (two rows' halves swapped);
- the wrapped key and the ciphertext moved together, which is the attempt
  that would work if only one layer carried the binding;
- a KEK that has been retired;
- a single flipped byte anywhere.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

from agentworld.infra import secrets
from agentworld.infra.secrets import keyring as keyring_module

WORKSPACE = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_WORKSPACE = uuid.UUID("22222222-2222-4222-8222-222222222222")
CREDENTIAL = uuid.UUID("33333333-3333-4333-8333-333333333333")
OTHER_CREDENTIAL = uuid.UUID("44444444-4444-4444-8444-444444444444")

SECRET = b"sk-ant-api03-not-a-real-key-0123456789"


def _encoded(seed: bytes) -> str:
    return base64.b64encode(seed * (secrets.KEK_BYTES // len(seed))).decode()


def _keyring(*ids: str, active: str | None = None) -> secrets.Keyring:
    keys = {key_id: os.urandom(secrets.KEK_BYTES) for key_id in ids}
    return secrets.Keyring(keys=keys, active_id=active or ids[0])


def _store(*ids: str, active: str | None = None) -> secrets.EnvelopeSecretStore:
    return secrets.EnvelopeSecretStore(_keyring(*ids, active=active))


# --------------------------------------------------------------- the keyring ---


def test_an_empty_configuration_is_the_development_keyring() -> None:
    ring = secrets.load("", "")
    assert ring.active_id == secrets.DEVELOPMENT_KEY_ID
    assert ring.holds_the_development_key


def test_the_development_keyring_is_the_same_one_every_time() -> None:
    """A credential written before a restart has to be readable after it."""
    assert secrets.development().active == secrets.development().active


def test_a_real_keyring_is_not_the_development_one() -> None:
    ring = secrets.load(json.dumps({"k2026a": _encoded(b"abcd")}), "k2026a")
    assert not ring.holds_the_development_key
    assert ring.key("k2026a") is not None


def test_the_development_key_is_recognised_under_any_id() -> None:
    """The id is not what makes it the development key.

    A deployment that pasted it under a convincing id would pass an id check
    and be exactly as compromised, so the check is on the material.
    """
    disguised = base64.b64encode(secrets.development().active).decode()
    ring = secrets.load(json.dumps({"k2026a": disguised}), "k2026a")
    assert ring.holds_the_development_key


def test_several_keys_are_valid_at_once() -> None:
    """What makes rotation additive rather than a cutover."""
    ring = secrets.load(json.dumps({"old": _encoded(b"abcd"), "new": _encoded(b"efgh")}), "new")
    assert ring.active_id == "new"
    assert ring.key("old") is not None


def test_a_retired_key_is_absent_rather_than_an_error() -> None:
    ring = secrets.load(json.dumps({"k2026b": _encoded(b"abcd")}), "k2026b")
    assert ring.key("k2026a") is None


@pytest.mark.parametrize(
    ("raw", "active", "because"),
    [
        ("{not json", "k", "not valid JSON"),
        ("[]", "k", "non-empty JSON object"),
        ("{}", "k", "non-empty JSON object"),
        ('{"k": "!!!!"}', "k", "not valid base64"),
        ('{"k": 7}', "k", "not a base64 string"),
        ('{"K2026A": "' + _encoded(b"abcd") + '"}', "K2026A", "1-64 characters"),
        ('{"k with spaces": "' + _encoded(b"abcd") + '"}', "k with spaces", "1-64 characters"),
        ('{"k": "' + base64.b64encode(b"short").decode() + '"}', "k", "AES-256 needs exactly"),
        ('{"k2026a": "' + _encoded(b"abcd") + '"}', "k2026b", "not one of the keys given"),
        ("", "k2026a", "CREDENTIAL_KEKS is empty"),
        ('{"k2026a": "' + _encoded(b"abcd") + '"}', "", "ACTIVE_KEK_ID is empty"),
    ],
)
def test_a_malformed_keyring_is_refused(raw: str, active: str, because: str) -> None:
    with pytest.raises(secrets.KeyringError, match=because):
        secrets.load(raw, active)


def test_a_rejection_does_not_quote_the_key_material() -> None:
    """The message says what is wrong, never what was given.

    `CREDENTIAL_KEKS` is key material; a validator that echoed it would put
    it into whatever reads the boot failure.
    """
    material = _encoded(b"abcd")
    with pytest.raises(secrets.KeyringError) as excinfo:
        secrets.load(json.dumps({"k2026a": material}), "k2026b")
    assert material not in str(excinfo.value)


def test_an_active_id_outside_the_keyring_is_refused_at_construction() -> None:
    with pytest.raises(secrets.KeyringError, match="not in the keyring"):
        secrets.Keyring(keys={"a": os.urandom(secrets.KEK_BYTES)}, active_id="b")


# ------------------------------------------------------------- the round trip ---


async def test_material_survives_a_round_trip() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    assert blob.key_id == "k1"
    assert await store.decrypt(blob, workspace_id=WORKSPACE, credential_id=CREDENTIAL) == SECRET


async def test_the_ciphertext_does_not_contain_the_plaintext() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    assert SECRET not in blob.ciphertext
    assert SECRET not in blob.wrapped_key


async def test_two_encryptions_of_one_secret_differ() -> None:
    """A fresh data key and nonce each time, so identical secrets in two
    workspaces are not identifiable as identical from the table."""
    store = _store("k1")
    first = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    second = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce
    assert first.wrapped_key != second.wrapped_key


async def test_the_nonce_is_the_size_gcm_is_specified_for() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    assert len(blob.nonce) == secrets.NONCE_BYTES
    assert len(blob.wrapped_key) > secrets.NONCE_BYTES


# ------------------------------------------------------- what the AAD refuses ---


async def test_a_row_read_for_another_workspace_cannot_be_decrypted() -> None:
    """The strongest isolation property here, and the reason for the AAD.

    This is a row that has physically moved - every byte is intact and the
    KEK is the right one. It still cannot be read, because the workspace it
    was written in is covered by the tag and is not in the row.
    """
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    with pytest.raises(secrets.SecretUnreadableError) as excinfo:
        await store.decrypt(blob, workspace_id=OTHER_WORKSPACE, credential_id=CREDENTIAL)
    assert excinfo.value.reason == "wrapped_key_tag_mismatch"


async def test_a_row_read_under_another_credential_id_cannot_be_decrypted() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    with pytest.raises(secrets.SecretUnreadableError):
        await store.decrypt(blob, workspace_id=WORKSPACE, credential_id=OTHER_CREDENTIAL)


async def test_moving_both_halves_together_does_not_help() -> None:
    """The attempt that would work if only one layer carried the binding.

    Copying `ciphertext` *and* `wrapped_key` into another workspace's row is
    the obvious next thing to try after the test above. Both layers carry the
    same AAD, so the wrap refuses first and the ciphertext would refuse after
    it.

    The assertion is on `reason`, not just on the exception, and that is the
    whole test. Removing the AAD from the *wrap* still leaves this refusing -
    one layer later, in the ciphertext - so a test that only asserted "it
    raises" would pass with the wrap binding gone and would be testing the
    ciphertext binding twice. `wrapped_key_tag_mismatch` is the statement
    that the outer layer is carrying its share.
    """
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    forged = secrets.EncryptedBlob(
        ciphertext=blob.ciphertext,
        nonce=blob.nonce,
        wrapped_key=blob.wrapped_key,
        key_id=blob.key_id,
        aad_version=blob.aad_version,
    )
    with pytest.raises(secrets.SecretUnreadableError) as excinfo:
        await store.decrypt(forged, workspace_id=OTHER_WORKSPACE, credential_id=OTHER_CREDENTIAL)
    assert excinfo.value.reason == "wrapped_key_tag_mismatch"


async def test_an_unknown_aad_version_is_refused_rather_than_guessed() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    future = secrets.EncryptedBlob(
        ciphertext=blob.ciphertext,
        nonce=blob.nonce,
        wrapped_key=blob.wrapped_key,
        key_id=blob.key_id,
        aad_version=secrets.AAD_VERSION + 1,
    )
    with pytest.raises(secrets.SecretUnreadableError) as excinfo:
        await store.decrypt(future, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    assert excinfo.value.reason == "unknown_aad_version"


# ------------------------------------------------- tampering and missing keys ---


@pytest.mark.parametrize("field", ["ciphertext", "nonce", "wrapped_key"])
async def test_one_flipped_byte_is_detected(field: str) -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    original: bytes = getattr(blob, field)
    corrupted = bytes([original[0] ^ 0x01]) + original[1:]
    tampered = secrets.EncryptedBlob(
        ciphertext=corrupted if field == "ciphertext" else blob.ciphertext,
        nonce=corrupted if field == "nonce" else blob.nonce,
        wrapped_key=corrupted if field == "wrapped_key" else blob.wrapped_key,
        key_id=blob.key_id,
        aad_version=blob.aad_version,
    )
    with pytest.raises(secrets.SecretUnreadableError):
        await store.decrypt(tampered, workspace_id=WORKSPACE, credential_id=CREDENTIAL)


async def test_a_row_naming_a_retired_key_is_unreadable_not_a_crash() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    after_retirement = secrets.EnvelopeSecretStore(_keyring("k2"))
    with pytest.raises(secrets.SecretUnreadableError) as excinfo:
        await after_retirement.decrypt(blob, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    assert excinfo.value.reason == "unknown_key_id"


async def test_a_truncated_wrapped_key_is_unreadable_not_an_index_error() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    stub = secrets.EncryptedBlob(
        ciphertext=blob.ciphertext,
        nonce=blob.nonce,
        wrapped_key=b"short",
        key_id=blob.key_id,
        aad_version=blob.aad_version,
    )
    with pytest.raises(secrets.SecretUnreadableError) as excinfo:
        await store.decrypt(stub, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    assert excinfo.value.reason == "truncated_wrapped_key"


async def test_the_failure_message_says_nothing_about_the_cause() -> None:
    """`reason` is for the log; the exception's text is for whoever sees it."""
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    messages = set()
    for workspace, credential in (
        (OTHER_WORKSPACE, CREDENTIAL),
        (WORKSPACE, OTHER_CREDENTIAL),
    ):
        with pytest.raises(secrets.SecretUnreadableError) as excinfo:
            await store.decrypt(blob, workspace_id=workspace, credential_id=credential)
        messages.add(str(excinfo.value))
    assert len(messages) == 1


# ----------------------------------------------------------------- re-wrapping ---


async def test_rewrapping_moves_the_key_and_leaves_the_ciphertext_alone() -> None:
    """The property that makes KEK rotation safe to run online.

    Asserted on the bytes, not on the call: `ciphertext` and `nonce` must be
    the same objects they were, because the rotation never has the plaintext
    to rebuild them from.
    """
    ring = _keyring("old", "new", active="old")
    store = secrets.EnvelopeSecretStore(ring)
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)

    rotated = secrets.EnvelopeSecretStore(secrets.Keyring(keys=ring.keys, active_id="new"))
    moved = await rotated.rewrap(blob, workspace_id=WORKSPACE, credential_id=CREDENTIAL)

    assert moved.key_id == "new"
    assert moved.wrapped_key != blob.wrapped_key
    assert moved.ciphertext == blob.ciphertext
    assert moved.nonce == blob.nonce
    assert await rotated.decrypt(moved, workspace_id=WORKSPACE, credential_id=CREDENTIAL) == SECRET


async def test_rewrapping_an_already_active_row_changes_nothing() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    assert await store.rewrap(blob, workspace_id=WORKSPACE, credential_id=CREDENTIAL) is blob


async def test_a_rewrapped_row_is_still_bound_to_its_workspace() -> None:
    """Rotation must not launder the AAD binding."""
    ring = _keyring("old", "new", active="old")
    store = secrets.EnvelopeSecretStore(ring)
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)

    rotated = secrets.EnvelopeSecretStore(secrets.Keyring(keys=ring.keys, active_id="new"))
    moved = await rotated.rewrap(blob, workspace_id=WORKSPACE, credential_id=CREDENTIAL)

    with pytest.raises(secrets.SecretUnreadableError):
        await rotated.decrypt(moved, workspace_id=OTHER_WORKSPACE, credential_id=CREDENTIAL)


async def test_rewrapping_a_row_whose_key_is_gone_fails_rather_than_corrupting_it() -> None:
    store = _store("k1")
    blob = await store.encrypt(SECRET, workspace_id=WORKSPACE, credential_id=CREDENTIAL)
    elsewhere = _store("k2")
    with pytest.raises(secrets.SecretUnreadableError):
        await elsewhere.rewrap(blob, workspace_id=WORKSPACE, credential_id=CREDENTIAL)


def test_the_development_seed_is_not_a_configurable() -> None:
    """Guard on the fixture helpers above.

    `_keyring` builds random keys, so if `development()` ever started
    returning one of those the tests that distinguish the two would pass for
    the wrong reason.
    """
    assert keyring_module.DEVELOPMENT_KEY_ID == "local-dev-insecure"
    assert len(secrets.development().active) == secrets.KEK_BYTES
