"""Argon2id hashing for API key secrets.

Peppered: the secret is hashed as `pepper + secret`, so a database dump alone
is not enough to mount an offline attack - the attacker also needs
`API_KEY_PEPPER`, which lives in the process environment and never in a table.
The pepper is prepended rather than concatenated at the end to keep the
construction unambiguous if either value ever contains the other's suffix.

Parameters are deliberately far below a password hasher's. A key secret is 32
bytes of `secrets.token_urlsafe` output - 256 bits of real entropy - so
brute-forcing it is already impossible, and the hash's job is only to make a
leaked table useless rather than to stretch a weak human-chosen input. The
cost matters here because this runs on every single ingest request: at
password-grade parameters a busy agent fleet would spend most of its latency
budget in the hasher. See `verify_needs_rehash` for how parameters change
later without invalidating existing keys.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: ~8 MiB and one pass over it. Tuned for a high-entropy secret checked on
#: every request, not for a human password checked at login.
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=8192,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def hash_secret(secret: str, *, pepper: str) -> str:
    """Hash a key secret. The returned string carries its own parameters."""
    return _HASHER.hash(_peppered(secret, pepper))


def verify_secret(secret: str, *, hashed: str, pepper: str) -> bool:
    """Constant-time-ish verification. False for every kind of failure.

    A malformed stored hash returns False rather than raising: a corrupt row
    should fail the one request that touches it, not 500 the endpoint, and it
    must not be distinguishable from a wrong secret by the caller.
    """
    try:
        return _HASHER.verify(hashed, _peppered(secret, pepper))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """Whether `hashed` was produced with weaker parameters than the current ones.

    Called after a successful verification, which is the only moment the
    plaintext is available to re-hash with. Keys verified against an old cost
    keep working; they are simply upgraded in place on next use.
    """
    try:
        return _HASHER.check_needs_rehash(hashed)
    except InvalidHashError:
        return False


def _peppered(secret: str, pepper: str) -> str:
    return f"{pepper}{secret}"
