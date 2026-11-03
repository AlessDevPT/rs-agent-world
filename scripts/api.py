#!/usr/bin/env python3
"""Cross-platform task runner for apps/api.

The Makefile is the documented entry point, but `make` is not available on a
default Windows install, so every API task is also reachable through this
script (and therefore through `npm run api:*`). It deliberately has no
dependencies of its own: it only locates the project venv and execs into it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "apps" / "api"
VENV = API / ".venv"


def venv_bin(name: str) -> Path:
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = f"{name}.exe" if os.name == "nt" else name
    return VENV / sub / exe


def ensure_venv() -> None:
    if not venv_bin("python").exists():
        print(f"creating venv at {VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)


def run(cmd: list[str], **kw: object) -> int:
    print("+ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=API, **kw).returncode  # type: ignore[arg-type]


def task_install() -> int:
    ensure_venv()
    py = str(venv_bin("python"))
    if (rc := run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])) != 0:
        return rc
    # Prefer uv when the developer has it: same pyproject, much faster solve.
    if shutil.which("uv"):
        return run(["uv", "pip", "install", "--python", py, "-e", ".[dev]"])
    return run([py, "-m", "pip", "install", "-e", ".[dev]"])


def task_dev() -> int:
    return run([str(venv_bin("python")), "-m", "uvicorn", "agentworld.main:app", "--reload", "--port", "8000"])


def task_worker() -> int:
    """The dispatch worker. Nothing performs queued work without it."""
    return run([str(venv_bin("python")), "-m", "agentworld.workers.worker", *sys.argv[2:]])


def task_reaper() -> int:
    """Lease recovery and advancement reconciliation, in one process."""
    return run([str(venv_bin("python")), "-m", "agentworld.workers.reaper", *sys.argv[2:]])


def task_test() -> int:
    return run([str(venv_bin("python")), "-m", "pytest", *sys.argv[2:]])


def task_migrate() -> int:
    """Apply migrations, as the schema owner.

    Alembic reads MIGRATION_DATABASE_URL (falling back to DATABASE_URL), not
    a url in alembic.ini, so there is one answer to "which database" shared
    with the application. Extra arguments are passed through, so
    `npm run api:migrate -- downgrade -1` works.
    """
    args = sys.argv[2:] or ["upgrade", "head"]
    return run([str(venv_bin("python")), "-m", "alembic", *args])


def task_revision() -> int:
    """Autogenerate a migration against the current database.

    Always read what it produces: autogenerate cannot see citext, partial
    indexes, check constraints or RLS policies, and will happily propose
    dropping things it does not understand.
    """
    message = " ".join(sys.argv[2:]) or "change"
    return run(
        [str(venv_bin("python")), "-m", "alembic", "revision", "--autogenerate", "-m", message]
    )


def task_lint() -> int:
    py = str(venv_bin("python"))
    worst = 0
    for cmd in (
        [py, "-m", "ruff", "check", "."],
        [py, "-m", "ruff", "format", "--check", "."],
        [py, "-m", "mypy", "src"],
        # The console script, not `python -m importlinter.cli`: that module
        # path exits 0 without evaluating a single contract, which looks
        # exactly like success.
        [str(venv_bin("lint-imports"))],
    ):
        worst = run(cmd) or worst
    return worst


def task_format() -> int:
    py = str(venv_bin("python"))
    run([py, "-m", "ruff", "format", "."])
    return run([py, "-m", "ruff", "check", "--fix", "."])


TASKS = {
    "install": task_install,
    "dev": task_dev,
    "worker": task_worker,
    "reaper": task_reaper,
    "migrate": task_migrate,
    "revision": task_revision,
    "test": task_test,
    "lint": task_lint,
    "format": task_format,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in TASKS:
        print(f"usage: python scripts/api.py [{'|'.join(TASKS)}]", file=sys.stderr)
        return 2
    if sys.argv[1] != "install" and not venv_bin("python").exists():
        print("venv missing - run: npm run api:install", file=sys.stderr)
        return 1
    return TASKS[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
