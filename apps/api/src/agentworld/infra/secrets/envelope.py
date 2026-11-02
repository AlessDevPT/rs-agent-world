"""Envelope encryption for credential material.

```
secret material
   │  AES-256-GCM under a per-credential data key (DEK),
   │  AAD = "agentworld.credential.v1" ‖ workspace_id ‖ credential_id
   ▼
ciphertext + nonce                    ──► credentials.ciphertext, .nonce
   ▲
   │ wrapped by
DEK, 256-bit, one per credential
   │  AES-256-GCM under the KEK named by key_id, same AAD
   ▼
wrapped_key = nonce ‖ ciphertext ‖ tag ──► credentials.wrapped_key
```

Three properties, each the reason a layer exists.

**The AAD binds both layers to one row in one workspace.** Additional
authenticated data is covered by the GCM tag and is not stored, so decryption
fails unless the caller supplies the same `workspace_id` and `credential_id`
the write used. A row copied into another workspace - by a bad restore, a
mistaken migration, or an injection that got as far as `INSERT ... SELECT` -
cannot be decrypted in its new home. Note that the *wrap* carries the same
AAD, so moving `wrapped_key` and `ciphertext` together does not help either:
both halves refuse. This is the one tenant-isolation property that still
holds when every check above it has been bypassed, and it costs nothing -
`decrypt` takes both ids and has no overload that omits them.

**A per-credential DEK makes KEK rotation cheap and plaintext-free.**
`rewrap` unwraps a DEK and wraps it again under the active KEK. It never
touches `ciphertext`, never sees the secret, and is a few microseconds per
row, so rotating a KEK over a few thousand credentials is an online
operation.

**`key_id` makes rotation additive.** Every row names the KEK that wrapped
it, so adding a key and moving `active_id` is not a cutover: old rows keep
working until something re-wraps them.

The nonce is 12 random bytes per encryption, which is the size GCM is
specified for. Random rather than a counter because there is no shared
counter to keep: a DEK is used for exactly one encryption in this design -
rotating a credential's material mints a new DEK rather than reusing the old
one with a second nonce.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Final, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agentworld.infra.secrets.keyring import Keyring

#: GCM's specified nonce length. Also the length `wrapped_key` reserves for
#: the wrap's own nonce in its first bytes.
NONCE_BYTES: Final = 12

#: AES-256.
DEK_BYTES: Final = 32

#: The only AAD construction. A second one would mean a second value here
#: and a branch in `_aad`; rows record which they were written with, so both
#: could be read during a migration between them.
AAD_VERSION: Final = 1

_AAD_PREFIX: Final = b"agentworld.credential.v1|"


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """What the four binary-ish columns of `credentials` hold, together.

    The only type in the codebase that carries credential ciphertext, and it
    carries no plaintext. `repositories.credentials` builds one on read and
    consumes one on write; no entity, schema or log line has a field for it.
    """

    ciphertext: bytes
    nonce: bytes
    wrapped_key: bytes
    key_id: str
    aad_version: int = AAD_VERSION


class SecretUnreadableError(Exception):
    """The material cannot be recovered, and the caller is not told why.

    One exception for every cause - a retired KEK, a row moved between
    workspaces, a corrupted byte, a tampered tag - because the differences
    are useful to an operator reading logs and useful to an attacker probing
    an endpoint. `reason` is for the first audience; nothing puts it in a
    response.
    """

    def __init__(self, reason: str) -> None:
        super().__init__("The credential material could not be decrypted")
        self.reason = reason


class SecretStore(Protocol):
    """What a credential store must do, whoever holds the keys.

    Implemented here over a keyring in the process environment. A KMS or
    Vault backend implements the same three methods with the same meanings
    and no caller changes - which is why these are `async` despite this
    implementation needing no await: the choice of backend is a deployment
    decision and must not be a code change.
    """

    async def encrypt(
        self, plaintext: bytes, *, workspace_id: UUID, credential_id: UUID
    ) -> EncryptedBlob: ...

    async def decrypt(
        self, blob: EncryptedBlob, *, workspace_id: UUID, credential_id: UUID
    ) -> bytes: ...

    async def rewrap(
        self, blob: EncryptedBlob, *, workspace_id: UUID, credential_id: UUID
    ) -> EncryptedBlob: ...


class EnvelopeSecretStore:
    """`SecretStore` over a keyring held in this process.

    Holds no state beyond the keyring, so one instance per process is fine
    and a per-request one costs nothing.
    """

    def __init__(self, keyring: Keyring) -> None:
        self._keyring = keyring

    @property
    def active_key_id(self) -> str:
        return self._keyring.active_id

    async def encrypt(
        self, plaintext: bytes, *, workspace_id: UUID, credential_id: UUID
    ) -> EncryptedBlob:
        aad = _aad(workspace_id=workspace_id, credential_id=credential_id, version=AAD_VERSION)
        dek = os.urandom(DEK_BYTES)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        return EncryptedBlob(
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_key=self._wrap(dek, aad),
            key_id=self._keyring.active_id,
            aad_version=AAD_VERSION,
        )

    async def decrypt(
        self, blob: EncryptedBlob, *, workspace_id: UUID, credential_id: UUID
    ) -> bytes:
        aad = _aad(workspace_id=workspace_id, credential_id=credential_id, version=blob.aad_version)
        dek = self._unwrap(blob, aad)
        try:
            return AESGCM(dek).decrypt(blob.nonce, blob.ciphertext, aad)
        except InvalidTag:
            # The DEK unwrapped, so the KEK and the ids are right and the
            # ciphertext itself does not match its tag.
            raise SecretUnreadableError("ciphertext_tag_mismatch") from None

    async def rewrap(
        self, blob: EncryptedBlob, *, workspace_id: UUID, credential_id: UUID
    ) -> EncryptedBlob:
        """Wrap the same DEK under the active KEK, leaving the ciphertext alone.

        Returns the blob unchanged when it already names the active key, so
        a rotation pass over a table that is already rotated writes nothing.
        """
        if blob.key_id == self._keyring.active_id:
            return blob
        aad = _aad(workspace_id=workspace_id, credential_id=credential_id, version=blob.aad_version)
        dek = self._unwrap(blob, aad)
        # `replace` rather than a fresh constructor: it is a compile-time
        # guarantee that `ciphertext` and `nonce` are carried over rather
        # than recomputed, which is the property the acceptance test asserts.
        return replace(blob, wrapped_key=self._wrap(dek, aad), key_id=self._keyring.active_id)

    def _wrap(self, dek: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + AESGCM(self._keyring.active).encrypt(nonce, dek, aad)

    def _unwrap(self, blob: EncryptedBlob, aad: bytes) -> bytes:
        kek = self._keyring.key(blob.key_id)
        if kek is None:
            raise SecretUnreadableError("unknown_key_id")
        if len(blob.wrapped_key) <= NONCE_BYTES:
            raise SecretUnreadableError("truncated_wrapped_key")
        nonce, wrapped = blob.wrapped_key[:NONCE_BYTES], blob.wrapped_key[NONCE_BYTES:]
        try:
            return AESGCM(kek).decrypt(nonce, wrapped, aad)
        except InvalidTag:
            # Either the row is not where it was written, or the KEK under
            # this id is not the one that wrapped it.
            raise SecretUnreadableError("wrapped_key_tag_mismatch") from None


def _aad(*, workspace_id: UUID, credential_id: UUID, version: int) -> bytes:
    """The additional authenticated data for one row.

    Raw uuid bytes rather than their text form: fixed-width fields cannot be
    confused with each other, whereas two concatenated strings could in
    principle be re-split in another place.
    """
    if version != AAD_VERSION:
        raise SecretUnreadableError("unknown_aad_version")
    return _AAD_PREFIX + workspace_id.bytes + credential_id.bytes
