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

import ast
import re
from pathlib import Path

from agentworld.domain.reducers.mutations import MutationTarget

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agentworld"

#: Modules permitted to write to `dispatches`, as paths relative to
#: `src/agentworld`. It was empty until T5, and T5 is the commit that added
#: the one entry - which is the point of keeping the list: this line is where
#: a reviewer looks to see every path by which AgentWorld can cause work.
#:
#: `repositories/executions.py` holds `DispatchRepository.create`, one INSERT,
#: reached only from `ExecutionService._apply` and only for a `DispatchStep`
#: decision. Claiming is T6's and will be a different repository, because one
#: that could both create and claim is the natural place for someone to add
#: "create and immediately run".
MAY_WRITE_DISPATCHES: frozenset[str] = frozenset({"repositories/executions.py"})

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


def _code_only(source: str) -> str:
    """The module's code with its documentation removed.

    Necessary, and the reason is a false positive this test produced on its
    first run against T5: `services/executions.py` documents the outbox with
    an SQL sketch - `INSERT INTO dispatches (the intent, as work to do)` -
    inside a docstring, and the pattern matched the prose. Rewording the
    docstring to please the test would have been the wrong way round.

    Comments and docstrings go; **string literals stay**, because raw SQL is
    a string literal and `text("INSERT INTO dispatches ...")` is exactly the
    spelling this test must still catch. `ast` drops comments for free, so
    only docstrings need removing by hand.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


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
        code = _code_only(path.read_text(encoding="utf-8"))
        for match in _WRITERS.finditer(code):
            findings.append(f"{relative}: {match.group().strip()}")
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
    # And not on a read, nor on documentation - the latter checked through
    # the same stripping the scan uses, since the pattern itself cannot tell
    # prose from code.
    assert not _WRITERS.search("rows = await session.execute(sa.select(m.Dispatch))")
    documented = _code_only(
        '''"""One transaction does this:

  INSERT INTO dispatches (kind)
"""
x = 1
'''
    )
    assert not _WRITERS.search(documented)
    # But a real statement survives the stripping.
    real = _code_only('''"""Doc."""
await session.execute(sa.insert(m.Dispatch))
''')
    assert _WRITERS.search(real)


def test_no_folded_event_can_reach_the_table() -> None:
    """The first of the four layers, read off the vocabulary itself.

    `MutationTarget` is what a reducer may name. There is no member for
    `dispatches`, so replaying the entire event log cannot produce a single
    row of work - which is the seventh protocol promise, and the reason
    replay is safe to run in production.
    """
    assert "dispatch" not in {member.value for member in MutationTarget}
    assert not any("dispatch" in member.value for member in MutationTarget)


def test_the_allow_list_holds_exactly_one_writer() -> None:
    """Stated as its own test so that changing it is a visible diff.

    One module, one method, one INSERT. A second entry here is a second way
    for work to come into being, and it should be as hard to add quietly as
    this test makes it.
    """
    assert sorted(MAY_WRITE_DISPATCHES) == ["repositories/executions.py"]
    allowed = SOURCE_ROOT / "repositories" / "executions.py"
    assert allowed.is_file(), "the allow-list names a file that does not exist"
