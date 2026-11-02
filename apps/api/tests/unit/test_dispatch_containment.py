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
4. This: no statement outside the apply path *creates* one, and only the
   claiming repository may update one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from agentworld.domain.reducers.mutations import MutationTarget

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agentworld"

#: Modules permitted to **create** a dispatch, as paths relative to
#: `src/agentworld`. This is the list that matters: an INSERT is how work
#: comes into being, and this line is where a reviewer looks to see every
#: path by which AgentWorld can cause an irreversible action. It was empty
#: until T5.
#:
#: `repositories/executions.py` holds `DispatchRepository.create`, one
#: INSERT, reached only from `ExecutionService._apply` and only for a
#: `DispatchStep` decision.
MAY_CREATE_DISPATCHES: frozenset[str] = frozenset({"repositories/executions.py"})

#: Modules permitted to **update** an existing dispatch. Looser on purpose,
#: and the distinction is the point: claiming, leasing and settling are
#: bookkeeping about work that was already authorized, while creating is
#: authorization itself. T6 added `repositories/dispatches.py`, which can
#: claim and settle and cannot create - and that split is why it is a
#: separate module rather than two more methods on the one above.
MAY_UPDATE_DISPATCHES: frozenset[str] = MAY_CREATE_DISPATCHES | {"repositories/dispatches.py"}

#: Creating one. Both the SQLAlchemy and the raw-SQL spelling, because one
#: import contract away from SQL is not the same as no SQL - `repositories/`
#: is allowed `text()` and uses it in places.
_CREATORS = re.compile(
    r"""(
        insert\s*\(\s*m?\.?Dispatch\b                 # sa.insert(m.Dispatch)
      | \bINSERT\s+INTO\s+dispatches\b
      | m\.Dispatch\s*\.\s*__table__\s*\.\s*insert    # .insert() off the table
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: Changing or removing one.
_MUTATORS = re.compile(
    r"""(
        (update|delete)\s*\(\s*m?\.?Dispatch\b
      | \b(UPDATE|DELETE\s+FROM)\s+dispatches\b
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


def _findings(pattern: re.Pattern[str], allowed: frozenset[str]) -> list[str]:
    findings: list[str] = []
    for path in _sources():
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative in allowed:
            continue
        code = _code_only(path.read_text(encoding="utf-8"))
        findings.extend(f"{relative}: {m.group().strip()}" for m in pattern.finditer(code))
    return findings


def test_nothing_outside_the_apply_path_creates_a_dispatch() -> None:
    """The one that matters. An INSERT is authorization to act."""
    findings = _findings(_CREATORS, MAY_CREATE_DISPATCHES)
    assert findings == [], (
        "a dispatch creator appeared outside the allow-list. A dispatch row is "
        "the authority to perform a side effect; adding one is a reviewed "
        "change, not an incidental one:\n" + "\n".join(findings)
    )


def test_only_the_claiming_repository_updates_a_dispatch() -> None:
    """Looser, and still worth enforcing: an UPDATE can revive settled work.

    A caller that could set `status` back to `pending` could re-run a
    consequential side effect without going near the decision path.
    """
    findings = _findings(_MUTATORS, MAY_UPDATE_DISPATCHES)
    assert findings == [], "a dispatch mutator appeared outside the allow-list:\n" + "\n".join(
        findings
    )


def test_the_patterns_would_actually_catch_one() -> None:
    """Non-vacuity, inline. Every spelling either verb could plausibly use."""
    assert _CREATORS.search("await session.execute(sa.insert(m.Dispatch).values(...))")
    assert _CREATORS.search('text("INSERT INTO dispatches (kind) VALUES (:kind)")')
    assert _CREATORS.search("m.Dispatch.__table__.insert()")
    assert _MUTATORS.search("sa.update(m.Dispatch).where(...)")
    assert _MUTATORS.search("sa.delete(Dispatch)")
    assert _MUTATORS.search('text("UPDATE dispatches SET status = :status")')
    # The verbs do not bleed into each other: a claim is not a creation.
    assert not _CREATORS.search("sa.update(m.Dispatch).values(status=LEASED)")
    # Nor on a read, nor on documentation - the latter checked through the
    # same stripping the scan uses, since a pattern cannot tell prose from
    # code.
    assert not _CREATORS.search("rows = await session.execute(sa.select(m.Dispatch))")
    prose = '''"""One transaction does this:

      INSERT INTO dispatches (kind)
    """
x = 1
'''
    assert not _CREATORS.search(_code_only(prose))
    real = '''"""Doc."""
await session.execute(sa.insert(m.Dispatch))
'''
    assert _CREATORS.search(_code_only(real))


def test_no_folded_event_can_reach_the_table() -> None:
    """The first of the four layers, read off the vocabulary itself.

    `MutationTarget` is what a reducer may name. There is no member for
    `dispatches`, so replaying the entire event log cannot produce a single
    row of work - which is the seventh protocol promise, and the reason
    replay is safe to run in production.
    """
    assert "dispatch" not in {member.value for member in MutationTarget}
    assert not any("dispatch" in member.value for member in MutationTarget)


def test_the_allow_lists_hold_what_they_should() -> None:
    """Stated as its own test so that changing either is a visible diff.

    One module may create: one method, one INSERT. A second entry there is a
    second way for work to come into being, and it should be as hard to add
    quietly as this makes it. Two may update, and the claiming one cannot
    create - which the creation test above is what actually proves.
    """
    assert sorted(MAY_CREATE_DISPATCHES) == ["repositories/executions.py"]
    assert sorted(MAY_UPDATE_DISPATCHES) == [
        "repositories/dispatches.py",
        "repositories/executions.py",
    ]
    for relative in MAY_UPDATE_DISPATCHES:
        assert (SOURCE_ROOT / relative).is_file(), f"{relative} does not exist"
