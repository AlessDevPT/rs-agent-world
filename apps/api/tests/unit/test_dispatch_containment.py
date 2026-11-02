"""Nothing can create a dispatch row, and nothing may until T5.

A dispatch row *is* the authority to perform a side effect. A worker claims
one and does what it says, so every question about execution safety reduces
to one question about this table: who can write to it?

Today the answer is nobody, and this file is what keeps that answer true
until a reviewed change makes it one named module. The allow-list below is
empty on purpose. T5 adds `services/executions.py` to it, in the same commit
that adds the writer, and that one-line diff is the moment a reviewer gets
to look at the only code in the system that can cause an irreversible
action.

Three layers already stand behind that, and this is the fourth:

1. `MutationTarget` has no `dispatches` member, so no folded event can
   reach the table - the seventh protocol promise, enforced by a closed
   vocabulary rather than by a trigger.
2. `DerivedStateRepository._TABLES` is keyed by that vocabulary, so the
   repository could not write one if a caller asked.
3. `advance()` never returns a decision carrying
   `side_effect_class = consequential`, so even once a writer exists there
   is no consequential dispatch for it to write.
4. This: no statement anywhere in `src/` targets the table at all.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentworld.domain.reducers.mutations import MutationTarget

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agentworld"

#: Modules permitted to write to `dispatches`, as paths relative to
#: `src/agentworld`. Empty until T5. Adding an entry is the reviewable act.
MAY_WRITE_DISPATCHES: frozenset[str] = frozenset()

#: A SQLAlchemy write against the model, or raw SQL against the table. Both
#: forms, because one import contract away from SQL is not the same as no
#: SQL - `repositories/` is allowed `text()` and uses it in places.
_WRITERS = re.compile(
    r"""(
        (insert|update|delete)\s*\(\s*m?\.?Dispatch\b   # sa.insert(m.Dispatch)
      | \b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+dispatches\b
      | m\.Dispatch\s*\.\s*__table__                     # .insert() off the table
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _sources() -> list[Path]:
    return [path for path in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in path.parts]


def test_the_scan_has_something_to_scan() -> None:
    """Guard on the guard, as everywhere else: a path typo would make the
    test below pass over an empty list."""
    files = _sources()
    assert len(files) > 50, f"only {len(files)} files under {SOURCE_ROOT}"


def test_nothing_in_src_writes_a_dispatch_row() -> None:
    findings: list[str] = []
    for path in _sources():
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative in MAY_WRITE_DISPATCHES:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _WRITERS.search(line):
                findings.append(f"{relative}:{number}: {line.strip()}")
    assert findings == [], (
        "a dispatch writer appeared outside the allow-list. A dispatch row is "
        "the authority to perform a side effect; adding one is a reviewed "
        "change, not an incidental one:\n" + "\n".join(findings)
    )


def test_the_pattern_would_actually_catch_a_writer() -> None:
    """Non-vacuity, inline. Every spelling a writer could plausibly use."""
    assert _WRITERS.search("await session.execute(sa.insert(m.Dispatch).values(...))")
    assert _WRITERS.search("sa.update(m.Dispatch).where(...)")
    assert _WRITERS.search("sa.delete(Dispatch)")
    assert _WRITERS.search('text("INSERT INTO dispatches (kind) VALUES (:kind)")')
    assert _WRITERS.search('text("UPDATE dispatches SET status = :status")')
    assert _WRITERS.search("m.Dispatch.__table__.insert()")
    # And would not fire on a comment that merely mentions the table.
    assert not _WRITERS.search("# the only decision that writes a dispatches row")
    assert not _WRITERS.search("rows = await session.execute(sa.select(m.Dispatch))")


def test_no_folded_event_can_reach_the_table() -> None:
    """The first of the four layers, read off the vocabulary itself.

    `MutationTarget` is what a reducer may name. There is no member for
    `dispatches`, so replaying the entire event log cannot produce a single
    row of work - which is the seventh protocol promise, and the reason
    replay is safe to run in production.
    """
    assert "dispatch" not in {member.value for member in MutationTarget}
    assert not any("dispatch" in member.value for member in MutationTarget)


def test_the_allow_list_is_still_empty() -> None:
    """Stated as its own test so that filling it is a visible diff.

    When T5 adds `services/executions.py`, this test changes with it and the
    reviewer sees both halves in one commit.
    """
    assert not MAY_WRITE_DISPATCHES
