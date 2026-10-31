"""Goal G1 and G8, asserted rather than trusted.

import-linter enforces this in `make lint`, but it is repeated here as a test
so that a developer running only pytest still cannot land a provider SDK or a
framework import inside the domain layer. The two checks are cheap and the
property is the one the whole architecture rests on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import agentworld

SRC = Path(agentworld.__file__).parent
DOMAIN = SRC / "domain"

FORBIDDEN_TOP_LEVEL = {
    # Frameworks and I/O
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "redis",
    "httpx",
    "requests",
    "asyncpg",
    "psycopg",
    # Providers - the core must never know which one is in use
    "openai",
    "anthropic",
    "google",
    "cohere",
    "mistralai",
    "ollama",
    "langchain",
    "langgraph",
    "llama_index",
    "crewai",
}

FORBIDDEN_INTERNAL = {
    "agentworld.api",
    "agentworld.services",
    "agentworld.repositories",
    "agentworld.adapters",
    "agentworld.infra",
    "agentworld.workers",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def domain_files() -> list[Path]:
    return sorted(DOMAIN.rglob("*.py"))


def test_there_are_domain_files_to_check() -> None:
    # Guards against the suite silently passing because the glob went stale.
    assert domain_files(), f"no python files found under {DOMAIN}"


@pytest.mark.parametrize("path", domain_files(), ids=lambda p: p.name)
def test_domain_module_imports_nothing_forbidden(path: Path) -> None:
    for module in _imported_modules(path):
        top = module.split(".")[0]
        assert top not in FORBIDDEN_TOP_LEVEL, (
            f"{path.relative_to(SRC)} imports '{module}'. The domain layer must stay "
            "framework-free and provider-free (goal G1)."
        )
        assert not any(
            module == forbidden or module.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_INTERNAL
        ), (
            f"{path.relative_to(SRC)} imports '{module}'. Dependencies point inward: "
            "the domain layer may not import outer layers."
        )


def test_domain_imports_cleanly_with_nothing_else_loaded() -> None:
    # If the domain package can be imported in a subprocess that has no
    # framework installed state, it is genuinely standalone.
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import agentworld.domain.errors, agentworld.domain.protocol.version",
        ],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC.parent), "PATH": ""},
    )
    assert result.returncode == 0, result.stderr
