"""Credential material: the only module that encrypts or decrypts it.

`infra/hashing.py` is the other half of the pair and is deliberately a
different mechanism. An API key must be verifiable and unreadable even by
this server, so it is an Argon2id hash of a peppered secret and nothing can
recover it. An integration credential must be *usable* by this server, so it
is AES-256-GCM ciphertext under a wrapped data key. Conflating the two gives
either unusable integrations or recoverable AgentWorld keys.
"""

from __future__ import annotations

from agentworld.infra.secrets.envelope import (
    AAD_VERSION,
    DEK_BYTES,
    NONCE_BYTES,
    EncryptedBlob,
    EnvelopeSecretStore,
    SecretStore,
    SecretUnreadableError,
)
from agentworld.infra.secrets.keyring import (
    DEVELOPMENT_KEY_ID,
    KEK_BYTES,
    Keyring,
    KeyringError,
    development,
    load,
)

__all__ = [
    "AAD_VERSION",
    "DEK_BYTES",
    "DEVELOPMENT_KEY_ID",
    "KEK_BYTES",
    "NONCE_BYTES",
    "EncryptedBlob",
    "EnvelopeSecretStore",
    "Keyring",
    "KeyringError",
    "SecretStore",
    "SecretUnreadableError",
    "development",
    "load",
]
