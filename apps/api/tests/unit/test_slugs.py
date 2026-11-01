"""Slug derivation and validation."""

from __future__ import annotations

import pytest

from agentworld.domain import slugs
from agentworld.domain.errors import ValidationFailedError


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Support Automation", "support-automation"),
        ("  Trimmed  ", "trimmed"),
        ("ALL CAPS", "all-caps"),
        ("many   spaces", "many-spaces"),
        ("punctuation!!! here", "punctuation-here"),
        ("already-a-slug", "already-a-slug"),
        ("mixed_underscores", "mixed-underscores"),
        ("v2.0 release", "v2-0-release"),
        ("--leading and trailing--", "leading-and-trailing"),
        ("digits 123", "digits-123"),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugs.slugify(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Café Zürich", "cafe-zurich"),
        ("Müller & Söhne", "muller-sohne"),
        ("naïve", "naive"),
        ("ação", "acao"),
    ],
)
def test_unicode_is_folded_rather_than_rejected(name: str, expected: str) -> None:
    """A workspace named "Café Zürich" should get a usable slug, not an error."""
    assert slugs.slugify(name) == expected


def test_a_name_with_nothing_to_fold_yields_an_empty_slug() -> None:
    """Which is then invalid, rather than silently becoming something odd."""
    assert slugs.slugify("🎉🎉🎉") == ""
    assert not slugs.is_valid("")


def test_a_long_name_is_truncated_without_a_trailing_hyphen() -> None:
    derived = slugs.slugify("a very long workspace name " * 10)
    assert len(derived) <= slugs.MAX_LENGTH
    assert not derived.endswith("-")
    assert slugs.is_valid(derived)


@pytest.mark.parametrize("slug", ["ok", "a-b", "project-1", "x" * slugs.MAX_LENGTH])
def test_valid_slugs(slug: str) -> None:
    assert slugs.is_valid(slug)
    assert slugs.reason_invalid(slug) is None


@pytest.mark.parametrize(
    ("slug", "fragment"),
    [
        ("a", "at least"),
        ("x" * (slugs.MAX_LENGTH + 1), "at most"),
        ("-leading", "lowercase"),
        ("trailing-", "lowercase"),
        ("double--hyphen", "lowercase"),
        ("UPPER", "lowercase"),
        ("has space", "lowercase"),
        ("under_score", "lowercase"),
        ("settings", "reserved"),
        ("api", "reserved"),
    ],
)
def test_invalid_slugs_explain_themselves(slug: str, fragment: str) -> None:
    """Unlike a credential check, being specific here costs nothing."""
    reason = slugs.reason_invalid(slug)
    assert reason is not None
    assert fragment in reason


def test_reserved_slugs_would_collide_with_the_web_app_routes() -> None:
    for reserved in ("settings", "new", "login", "v1"):
        assert reserved in slugs.RESERVED
        assert not slugs.is_valid(reserved)


def test_validated_derives_from_the_fallback_name() -> None:
    assert slugs.validated(None, fallback="Support Automation") == "support-automation"


def test_validated_prefers_an_explicit_slug() -> None:
    assert slugs.validated("explicit", fallback="Ignored Name") == "explicit"


def test_validated_normalises_an_explicit_slug() -> None:
    """A client sending "My Project" as a slug gets it folded, not rejected."""
    assert slugs.validated("My Project") == "my-project"


def test_validated_raises_on_something_unusable() -> None:
    with pytest.raises(ValidationFailedError):
        slugs.validated(None, fallback="🎉")
    with pytest.raises(ValidationFailedError):
        slugs.validated("settings")
