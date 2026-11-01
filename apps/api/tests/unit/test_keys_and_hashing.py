"""The API key format and the hashing of its secret."""

from __future__ import annotations

import pytest

from agentworld.domain import keys
from agentworld.domain.enums import ApiKeyScope
from agentworld.domain.principals import KeyPrincipal
from agentworld.infra import hashing

PEPPER = "a-test-pepper"


# -------------------------------------------------------------- the format ---


@pytest.mark.parametrize("environment", ["live", "test"])
def test_mint_then_parse_round_trips(environment: keys.KeyEnvironment) -> None:
    minted = keys.mint(environment)
    parsed = keys.parse(minted.plaintext)

    assert parsed is not None
    assert parsed.prefix == minted.prefix
    assert parsed.secret == minted.secret
    assert parsed.environment == environment


def test_the_prefix_carries_its_environment() -> None:
    assert keys.mint("live").prefix.startswith("aw_live_")
    assert keys.mint("test").prefix.startswith("aw_test_")


def test_minted_keys_are_unique() -> None:
    """Both halves are random, so a collision would be a generator bug.

    The prefix is UNIQUE in the database; a generator that repeated itself
    would surface as a failed mint for a user who did nothing wrong.
    """
    minted = [keys.mint("test") for _ in range(200)]
    assert len({key.prefix for key in minted}) == 200
    assert len({key.secret for key in minted}) == 200


def test_the_secret_never_contains_the_separator() -> None:
    """What makes `partition(".")` unambiguous.

    `token_urlsafe` emits only [A-Za-z0-9_-], so the first "." in a key is
    always the real separator. If that ever changed, parsing would silently
    truncate secrets instead of failing.
    """
    for _ in range(200):
        assert keys.SEPARATOR not in keys.mint("test").secret


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "   ",
        "not-a-key",
        "aw_live_8f2c1a",  # prefix only, no separator
        "aw_live_8f2c1a.",  # empty secret
        ".secret",  # empty prefix
        "aw_prod_8f2c1a.abcdefghijklmnopqrstuvwxyz",  # unknown environment
        "aw_live_8f2c1.abcdefghijklmnopqrstuvwxyz",  # prefix too short
        "aw_live_NOTHEX8f.abcdefghijklmnopqrstuvwxyz",  # prefix not hex
        "aw_live_8f2c1a3d5e70.short",  # secret too short
        "aw_live_8f2c1a3d5e70.has spaces in it here",  # illegal characters
        "sk_live_8f2c1a3d5e70.abcdefghijklmnopqrstuvwxyz",  # someone else's format
    ],
)
def test_malformed_keys_do_not_parse(malformed: str) -> None:
    assert keys.parse(malformed) is None


def test_surrounding_whitespace_is_tolerated() -> None:
    """A key pasted out of a terminal or a YAML file often carries a newline."""
    minted = keys.mint("test")
    assert keys.parse(f"  {minted.plaintext}\n") is not None


def test_a_secret_containing_a_separator_parses_at_the_first_one() -> None:
    """Defensive: a hand-made key with extra dots must not parse as valid.

    `partition` splits at the first ".", so the remainder keeps the second
    one, which then fails the secret pattern. The alternative - splitting at
    the last "." - would accept a key whose prefix contained a dot.
    """
    assert keys.parse("aw_live_8f2c1a3d5e70.abcdefghijklmnopqrst.uvwxyz") is None


# -------------------------------------------------------------- the allowlist ---


def principal(allowlist: list[str] | None) -> KeyPrincipal:
    import uuid

    return KeyPrincipal(
        api_key_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        scope=ApiKeyScope.INGEST,
        agent_key_allowlist=allowlist,
    )


def test_no_allowlist_permits_any_agent() -> None:
    assert principal(None).may_act_as("anything-at-all")


def test_an_exact_allowlist_entry_matches_only_itself() -> None:
    key = principal(["etl-worker-eu-1"])
    assert key.may_act_as("etl-worker-eu-1")
    assert not key.may_act_as("etl-worker-eu-2")
    assert not key.may_act_as("etl-worker-eu-1-extra")


def test_a_trailing_star_matches_a_prefix() -> None:
    key = principal(["etl-worker-eu-*"])
    assert key.may_act_as("etl-worker-eu-1")
    assert key.may_act_as("etl-worker-eu-anything")
    assert not key.may_act_as("etl-worker-us-1")


def test_allowlist_matching_is_case_insensitive() -> None:
    """`agent_key` is `citext` in the database, so matching must agree.

    If the allowlist were case-sensitive and the unique index were not, an
    agent could register as `Worker` and bypass an allowlist for `worker-*`.
    """
    assert principal(["worker-*"]).may_act_as("WORKER-7")
    assert principal(["Worker-7"]).may_act_as("worker-7")


def test_several_patterns_are_ored() -> None:
    key = principal(["planner", "etl-*"])
    assert key.may_act_as("planner")
    assert key.may_act_as("etl-1")
    assert not key.may_act_as("support-bot")


# ---------------------------------------------------------------- the hashing ---


def test_hash_then_verify() -> None:
    secret = keys.mint("test").secret
    hashed = hashing.hash_secret(secret, pepper=PEPPER)
    assert hashing.verify_secret(secret, hashed=hashed, pepper=PEPPER)


def test_the_hash_does_not_contain_the_secret() -> None:
    secret = keys.mint("test").secret
    assert secret not in hashing.hash_secret(secret, pepper=PEPPER)


def test_the_same_secret_hashes_differently_each_time() -> None:
    """Per-hash salt. Without it, identical secrets would be visibly identical
    in a database dump, and one precomputed table would break every key."""
    secret = keys.mint("test").secret
    assert hashing.hash_secret(secret, pepper=PEPPER) != hashing.hash_secret(secret, pepper=PEPPER)


def test_a_wrong_secret_does_not_verify() -> None:
    hashed = hashing.hash_secret("the-right-secret", pepper=PEPPER)
    assert not hashing.verify_secret("the-wrong-secret", hashed=hashed, pepper=PEPPER)


def test_a_wrong_pepper_does_not_verify() -> None:
    """The point of the pepper: a stolen database is not enough.

    An attacker with every row still needs `API_KEY_PEPPER`, which lives in
    the process environment and in no table.
    """
    secret = keys.mint("test").secret
    hashed = hashing.hash_secret(secret, pepper=PEPPER)
    assert not hashing.verify_secret(secret, hashed=hashed, pepper="a-different-pepper")


@pytest.mark.parametrize("corrupt", ["", "not-a-hash", "$argon2id$v=19$garbage", "$2b$10$abc"])
def test_a_corrupt_stored_hash_returns_false_rather_than_raising(corrupt: str) -> None:
    """One bad row should fail its own request, not 500 the endpoint - and
    must not be distinguishable from a wrong secret."""
    assert not hashing.verify_secret("whatever", hashed=corrupt, pepper=PEPPER)


def test_current_parameters_do_not_ask_for_a_rehash() -> None:
    hashed = hashing.hash_secret("secret", pepper=PEPPER)
    assert hashing.needs_rehash(hashed) is False


def test_weaker_parameters_do_ask_for_a_rehash() -> None:
    """The upgrade path: a hash from older, cheaper settings is replaced on
    next use, while continuing to verify until then."""
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("x")
    assert hashing.needs_rehash(weak) is True


def test_needs_rehash_tolerates_a_corrupt_hash() -> None:
    assert hashing.needs_rehash("not-a-hash") is False
