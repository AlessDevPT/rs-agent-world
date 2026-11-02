"""The key-encryption keys, and where they are allowed to come from.

A keyring is a map of key id to 32 raw bytes, plus the id of the one that
wraps new data keys. Several are valid at once on purpose: that is what makes
rotation additive. A new KEK is added, `active_id` moves to it, rows are
re-wrapped in the background, and the old KEK is retired only once no row
names it. At no point is there a cutover during which half the table cannot
be read.

Two things here are deliberately strict.

**A malformed `CREDENTIAL_KEKS` is a boot failure, not a runtime one.**
`load` raises `KeyringError`, which is a `ValueError`, so the settings
validator that calls it turns it into a pydantic `ValidationError` and the
process never finishes starting. The alternative - discovering it at the
first credential write - means a deployment that looks healthy and cannot
store a secret.

**The development keyring is a constant, and says so in its id.** Tests and a
local run need *a* key, and making one up per process would mean a credential
written before a restart could not be read after it. So `development()`
returns a fixed key derived from a fixed string. It is not a secret and is not
meant to be one: `Settings` refuses to construct in staging or production
without a real `CREDENTIAL_KEKS`, which is what keeps this out of a
deployment. The id it carries, `local-dev-insecure`, is stored in
`credentials.key_id`, so a row written under it is identifiable in one query.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: AES-256. Not negotiable per key: a 16-byte value here would silently give
#: a weaker cipher than the one the design documents, so it is refused.
KEK_BYTES: Final = 32

#: `key_id` goes into a text column, into log lines and into operator
#: queries. Keeping it to this shape means it needs no quoting anywhere.
_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: The id every locally-written credential carries. Chosen to be obvious in a
#: table: `SELECT count(*) FROM credentials WHERE key_id = 'local-dev-insecure'`
#: answers "is any of this protected by the development key?".
DEVELOPMENT_KEY_ID: Final = "local-dev-insecure"

_DEVELOPMENT_SEED: Final = b"agentworld local development key-encryption key"


class KeyringError(ValueError):
    """`CREDENTIAL_KEKS` or `CREDENTIAL_ACTIVE_KEK_ID` cannot be used.

    A `ValueError` so that a pydantic validator calling `load` reports it as
    a configuration error on the field rather than as an internal failure.
    """


@dataclass(frozen=True, slots=True)
class Keyring:
    """Every KEK this process can unwrap with, and the one it wraps with."""

    keys: Mapping[str, bytes]
    active_id: str

    def __post_init__(self) -> None:
        if self.active_id not in self.keys:
            raise KeyringError(f"The active key id '{self.active_id}' is not in the keyring")

    @property
    def active(self) -> bytes:
        return self.keys[self.active_id]

    def key(self, key_id: str) -> bytes | None:
        """The KEK a row names, or None if it has been retired.

        None rather than an exception: a retired KEK is an operational state
        with an operational answer (put it back, or accept the rows are
        gone), and the caller turns it into the same unreadable-secret
        failure as a tampered row so that neither is distinguishable from
        outside.
        """
        return self.keys.get(key_id)

    @property
    def holds_the_development_key(self) -> bool:
        """Whether any key here is the fixed development one.

        Compared by material, not by id. A deployment that pasted the
        development key under a convincing id like `k2026a` would pass an
        id check and be exactly as compromised, so the id is not what is
        worth looking at.
        """
        development_key = hashlib.sha256(_DEVELOPMENT_SEED).digest()
        return any(key == development_key for key in self.keys.values())


def development() -> Keyring:
    """The fixed local keyring. See the module docstring for why it is fixed."""
    return Keyring(
        keys={DEVELOPMENT_KEY_ID: hashlib.sha256(_DEVELOPMENT_SEED).digest()},
        active_id=DEVELOPMENT_KEY_ID,
    )


def load(raw_keys: str, active_id: str) -> Keyring:
    """Parse `CREDENTIAL_KEKS` and `CREDENTIAL_ACTIVE_KEK_ID`.

    Both empty means the development keyring - a convenience `Settings`
    permits only outside staging and production. Anything else must be
    complete: a map with no active id, or an active id with no map, is a
    misconfiguration rather than a default to fill in.
    """
    if not raw_keys and not active_id:
        return development()
    if not raw_keys:
        raise KeyringError("CREDENTIAL_ACTIVE_KEK_ID is set but CREDENTIAL_KEKS is empty")
    if not active_id:
        raise KeyringError("CREDENTIAL_KEKS is set but CREDENTIAL_ACTIVE_KEK_ID is empty")

    try:
        parsed = json.loads(raw_keys)
    except json.JSONDecodeError as exc:
        # The message, not the document: the document is key material.
        raise KeyringError(f"CREDENTIAL_KEKS is not valid JSON ({exc.msg})") from None
    if not isinstance(parsed, dict) or not parsed:
        raise KeyringError("CREDENTIAL_KEKS must be a non-empty JSON object of id -> base64 key")

    keys: dict[str, bytes] = {}
    for key_id, encoded in parsed.items():
        if not isinstance(key_id, str) or not _ID_PATTERN.match(key_id):
            raise KeyringError(
                "A key id must be 1-64 characters of [a-z0-9_-] and start with [a-z0-9]"
            )
        if not isinstance(encoded, str):
            raise KeyringError(f"The value for key id '{key_id}' is not a base64 string")
        keys[key_id] = _decoded(key_id, encoded)

    if active_id not in keys:
        raise KeyringError(f"CREDENTIAL_ACTIVE_KEK_ID '{active_id}' is not one of the keys given")
    return Keyring(keys=keys, active_id=active_id)


def _decoded(key_id: str, encoded: str) -> bytes:
    """Decode one base64 key, saying what is wrong without saying what it is."""
    try:
        material = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise KeyringError(f"The key for id '{key_id}' is not valid base64") from None
    if len(material) != KEK_BYTES:
        raise KeyringError(
            f"The key for id '{key_id}' decodes to {len(material)} bytes; "
            f"AES-256 needs exactly {KEK_BYTES}"
        )
    return material
