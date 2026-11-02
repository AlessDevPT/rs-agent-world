"""No vendor's name appears in source outside `infra/providers/`.

The import contract in `pyproject.toml` covers SDK *imports*, and that is
most of it but not all: a vendor can leak in as a base URL in a settings
field, a slug in a match statement, a model id in a constant or a comment
that says "Anthropic needs this header". None of those is an import, and each
is the beginning of a caller that knows which provider it is talking to.

So this test reads the source rather than the import graph. It is the one
check of the three that catches the leak before it becomes a dependency - by
the time there is an `import`, the coupling has already happened.

It is also the only check that can cover the Gemini SDK, which import-linter
cannot name: a forbidden contract may not list a subpackage of an external
package, and forbidding `google` wholesale would be absurd.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agentworld"

#: The one package allowed to know a vendor exists.
EXEMPT = SOURCE_ROOT / "infra" / "providers"

#: Matched case-insensitively, on a word boundary so that an unrelated
#: substring in an ordinary word is not a finding. `claude` and `gpt` are
#: here as well as the company names: a model id in a constant is the same
#: leak as a company name, and arguably the more tempting one.
VENDORS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "claude",
    "gemini",
    "bedrock",
    "mistral",
    "cohere",
    "llama",
    "langchain",
    "langgraph",
    "litellm",
    "gpt-3",
    "gpt-4",
    "gpt-5",
)

_PATTERN = re.compile("|".join(rf"\b{re.escape(name)}\b" for name in VENDORS), re.IGNORECASE)


def _searchable() -> list[Path]:
    return [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if EXEMPT not in path.parents and "__pycache__" not in path.parts
    ]


def test_the_search_has_something_to_search() -> None:
    """Guard on the guard.

    A path typo would make the test below pass over an empty list, which is
    the failure mode that matters: a containment check that finds nothing
    because it looked nowhere.
    """
    files = _searchable()
    assert len(files) > 50, f"only {len(files)} files found under {SOURCE_ROOT}"
    assert EXEMPT.is_dir(), "the exempt package must exist or the exemption is a typo"
    assert not any(EXEMPT in path.parents for path in files)


def test_no_vendor_is_named_outside_the_provider_package() -> None:
    findings: list[str] = []
    for path in _searchable():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if (match := _PATTERN.search(line)) is not None:
                findings.append(f"{path.relative_to(SOURCE_ROOT)}:{number} names '{match.group()}'")
    assert findings == [], "a vendor leaked outside infra/providers:\n" + "\n".join(findings)


def test_the_pattern_would_actually_catch_one() -> None:
    """Non-vacuity, inline.

    The test above passes if the regex is broken, if `VENDORS` is emptied,
    or if the word-boundary anchors stop matching. This is cheaper than
    planting a file and deleting it.
    """
    assert _PATTERN.search('BASE_URL = "https://api.anthropic.com"') is not None
    assert _PATTERN.search("# Anthropic needs this header") is not None
    assert _PATTERN.search('model_id = "claude-opus-5"') is not None
    assert _PATTERN.search("from openai import AsyncOpenAI") is not None
    # And would not fire on ordinary prose that merely contains a substring.
    assert _PATTERN.search("the llamas were unimpressed") is None


@pytest.mark.parametrize("name", VENDORS)
def test_every_listed_vendor_is_matchable(name: str) -> None:
    """A typo in the list is a vendor nobody is checking for."""
    assert _PATTERN.search(f"x = {name} ") is not None
