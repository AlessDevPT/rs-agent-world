"""Slug generation and validation.

Slugs are the human-memorable half of a workspace or project identity. They
are `citext`, so `Acme` and `acme` cannot both exist, and they appear in URLs
the web app builds, which is where the reserved list comes from.

Pure: no database, so "is this slug free" is not answered here. This module
decides whether a slug is *well-formed*; the repository decides whether it is
*available*. Conflating the two is how you end up with a validator that needs
a connection.
"""

from __future__ import annotations

import re
import unicodedata

from agentworld.domain.errors import ValidationFailedError

MIN_LENGTH = 2
MAX_LENGTH = 48

_VALID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: Segments the web app uses in its own routes. A workspace called `settings`
#: would make `/w/settings/projects` ambiguous with the settings page, so the
#: ambiguity is refused at creation rather than resolved by precedence rules
#: nobody can remember.
RESERVED = frozenset(
    {
        "admin",
        "api",
        "auth",
        "callback",
        "dashboard",
        "docs",
        "health",
        "healthz",
        "login",
        "logout",
        "new",
        "readyz",
        "settings",
        "signup",
        "static",
        "v1",
        "ws",
    }
)


def slugify(value: str) -> str:
    """Derive a candidate slug from a display name.

    Unicode is folded to ASCII rather than rejected: a workspace named
    "Café Zürich" should get `cafe-zurich` and not an error message. NFKD
    decomposition splits accented characters into a base letter plus a
    combining mark, and the mark is then dropped by the ASCII encode.

    The result may still be invalid - a name of only emoji folds to the empty
    string - so callers validate rather than trust.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM.sub("-", folded.lower()).strip("-")[:MAX_LENGTH].strip("-")


def is_valid(slug: str) -> bool:
    return (
        MIN_LENGTH <= len(slug) <= MAX_LENGTH
        and _VALID.match(slug) is not None
        and slug not in RESERVED
    )


def reason_invalid(slug: str) -> str | None:
    """Why a slug is unacceptable, phrased for the person who typed it.

    Returns None when the slug is fine. Unlike a credential check, being
    specific here costs nothing and saves the caller a guessing game.
    """
    if len(slug) < MIN_LENGTH:
        return f"must be at least {MIN_LENGTH} characters"
    if len(slug) > MAX_LENGTH:
        return f"must be at most {MAX_LENGTH} characters"
    if _VALID.match(slug) is None:
        return "may contain only lowercase letters, digits and single hyphens between them"
    if slug in RESERVED:
        return "is reserved"
    return None


def validated(slug: str | None, *, fallback: str | None = None) -> str:
    """Normalise a slug and raise if it is unusable.

    `fallback` is the display name to derive from when no slug was given, so
    `POST /v1/workspaces {"name": "Support Automation"}` yields
    `support-automation` without the caller having to think about slugs at
    all.
    """
    candidate = slugify(slug if slug is not None else (fallback or ""))
    if (problem := reason_invalid(candidate)) is not None:
        raise ValidationFailedError(f"The slug '{candidate}' {problem}", slug=candidate)
    return candidate
