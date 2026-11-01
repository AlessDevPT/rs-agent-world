"""API key format: minting, parsing, and nothing else.

The wire format is

    aw_<env>_<random>.<secret>
    ^^^^^^^^^^^^^^^^^ the prefix: public, stored in clear, shown in the UI
                      ^^^^^^^^ the secret: Argon2id-hashed, shown exactly once

Splitting the key means lookup is `prefix -> row -> verify(secret, hash)`, a
single indexed read followed by one constant-time comparison. The alternative
- hashing the whole key and searching for the hash - either needs the hash to
be deterministic (no per-key salt, so one rainbow table breaks every key) or
needs a full table scan per request.

Hashing lives in `agentworld.infra.hashing`, not here: Argon2id is a library
dependency and this module stays pure so the format can be tested, documented
and reimplemented in an SDK without pulling in cryptography.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Literal

#: Keys minted against a deployed environment say `live`; everything else says
#: `test`. The verifier refuses a key whose label does not match the server it
#: is presented to, so a development key pasted into production fails loudly
#: instead of quietly working.
KeyEnvironment = Literal["live", "test"]

SEPARATOR = "."

#: 48 bits of prefix. The documented example in `docs/03-API.md` shows six hex
#: characters; the parser accepts 6-16 so illustrative and historical keys
#: still parse, but minting uses 12. At 24 bits a workspace with a few
#: thousand keys has a noticeable chance of a prefix collision, and the unique
#: index would surface that as a failed mint for no reason worth paying.
_PREFIX_RANDOM_HEX = 12

#: 32 bytes, URL-safe base64. `token_urlsafe` emits only [A-Za-z0-9_-], never
#: a ".", which is what makes the separator unambiguous: the first "." in a
#: well-formed key is always the real one.
_SECRET_BYTES = 32

_PREFIX_RE = re.compile(r"^aw_(?P<env>live|test)_(?P<random>[0-9a-f]{6,16})$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")


@dataclass(frozen=True, slots=True)
class MintedKey:
    """The result of minting. `plaintext` exists only in this object."""

    prefix: str
    secret: str

    @property
    def plaintext(self) -> str:
        return f"{self.prefix}{SEPARATOR}{self.secret}"


@dataclass(frozen=True, slots=True)
class ParsedKey:
    prefix: str
    secret: str
    environment: KeyEnvironment


def mint(environment: KeyEnvironment) -> MintedKey:
    return MintedKey(
        prefix=f"aw_{environment}_{secrets.token_hex(_PREFIX_RANDOM_HEX // 2)}",
        secret=secrets.token_urlsafe(_SECRET_BYTES),
    )


def parse(raw: str) -> ParsedKey | None:
    """Parse a presented key, or return None if it is not even well-formed.

    None rather than an exception: a malformed credential is a 401 like any
    other bad credential, and the caller should not have to tell the two cases
    apart. Nothing about *why* it failed is ever reported back - that would
    turn this function into an oracle for the format.
    """
    prefix, separator, secret = raw.strip().partition(SEPARATOR)
    if not separator:
        return None
    match = _PREFIX_RE.match(prefix)
    if match is None or not _SECRET_RE.match(secret):
        return None
    environment: KeyEnvironment = "live" if match.group("env") == "live" else "test"
    return ParsedKey(prefix=prefix, secret=secret, environment=environment)
