"""Every field a reducer writes is a real column on its real table.

The reducers are pure and know nothing about the database, which is what makes
them cheap to test - and what makes a typo invisible until an event arrives in
production and the whole ingest transaction fails. This is the one test that
closes that gap, and it needs a database precisely because the two halves are
meant not to know about each other.

It is also the test that notices a *migration* removing a column a reducer
still writes, which is the direction that will actually happen.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from agentworld.domain.protocol import KNOWN_EVENTS, PROTOCOL_VERSION, parse_envelope
from agentworld.domain.reducers import Increment, MutationTarget, fold
from agentworld.infra.db import Database

pytestmark = pytest.mark.integration

#: A plausible payload and correlation for each type that needs one, so every
#: reducer actually produces its mutations rather than folding to nothing.
PAYLOADS: dict[str, dict[str, Any]] = {
    "agent.registered": {
        "capabilities": ["search"],
        "labels": {"tier": "gold"},
        "runtime": {"sdk": "python"},
        "heartbeat_ttl_seconds": 90,
    },
    "agent.capability_changed": {"capabilities": ["search", "write"]},
    "agent.status_changed": {"status": "working", "reason": "picked up a task"},
    "agent.paused": {"reason": "operator"},
    "task.progress": {"percent": 42, "message": "reading"},
    "task.completed": {"result": {"ok": True}},
    "task.failed": {"error": {"code": "timeout"}},
    "run.step_started": {"seq": 1, "name": "turn", "kind": "llm", "input": {}},
    "run.step_finished": {"seq": 1, "output": {"tokens": 10}},
    "run.failed": {"error": {"code": "rate_limited"}},
    "workflow.step_retried": {"attempt": 2},
    "workflow.step_succeeded": {"output": {"summary": "done"}},
    "workflow.step_failed": {"error": {"code": "tool_denied"}},
    "workflow.execution_completed": {"output": {"n": 1}},
    "workflow.execution_failed": {"error": {"code": "step_failed"}},
    "approval.denied": {"reason": "not this quarter"},
}

REFS = {"execution_step": "exec-1:post", "tool_call": "run-1:tool-1"}

CORRELATIONS: dict[str, dict[str, str]] = {
    "execution_step": {"execution_ref": "exec-1"},
    "tool_call": {"run_ref": "run-1"},
    "approval": {"execution_ref": "exec-1", "execution_step_ref": "exec-1:post"},
}

#: Fields the repository fills in, not the reducer. Listed so the test can
#: tell "the reducer must not write this" from "this column does not exist".
REPOSITORY_OWNED = {"from_status", "event_id"}


async def _columns(database: Database) -> dict[str, set[str]]:
    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    " WHERE table_schema = 'public' AND table_name = ANY (:tables)"
                ),
                {"tables": [member.value for member in MutationTarget]},
            )
        ).all()
    columns: dict[str, set[str]] = {}
    for table, column in rows:
        columns.setdefault(table, set()).add(column)
    return columns


def _fold_everything() -> list[tuple[str, Any]]:
    """Fold a plausible event of every known type, keeping its type."""
    folded: list[tuple[str, Any]] = []
    for event_type, spec in KNOWN_EVENTS.items():
        kind = spec.subjects[0].value
        envelope = parse_envelope(
            {
                "dedup_key": f"probe:{event_type}",
                "type": event_type,
                "protocol_version": PROTOCOL_VERSION,
                "occurred_at": dt.datetime(2026, 11, 4, 7, tzinfo=dt.UTC).isoformat(),
                "subject": {"kind": kind, "ref": REFS.get(kind, "r-1")},
                "correlation": CORRELATIONS.get(kind, {}),
                "payload": PAYLOADS.get(event_type, {}),
            }
        )
        folded.extend((event_type, mutation) for mutation in fold(envelope))
    return folded


async def test_every_field_a_reducer_writes_exists_on_its_table(
    owner_database: Database,
) -> None:
    mutations = _fold_everything()
    assert mutations, "the fixtures should produce mutations for most known types"

    columns = await _columns(owner_database)
    offenders: list[str] = []

    for event_type, mutation in mutations:
        table = mutation.target.value
        written = (
            mutation.deltas.keys() if isinstance(mutation, Increment) else mutation.fields.keys()
        )
        for field in written:
            if field not in columns[table]:
                offenders.append(f"{event_type} -> {table}.{field}")

    assert offenders == [], "a reducer writes a column that does not exist: " + ", ".join(offenders)


async def test_increments_only_target_numeric_columns(
    owner_database: Database,
) -> None:
    """Adding one to a text column is a transaction failure, not a bad value."""
    async with owner_database.session() as session:
        numeric = {
            f"{table}.{column}"
            for table, column in (
                await session.execute(
                    text(
                        "SELECT table_name, column_name FROM information_schema.columns "
                        " WHERE table_schema = 'public' AND table_name = ANY (:tables) "
                        "   AND data_type IN "
                        "       ('integer', 'bigint', 'smallint', 'numeric', "
                        "        'double precision', 'real')"
                    ),
                    {"tables": [member.value for member in MutationTarget]},
                )
            ).all()
        }

    for event_type, mutation in _fold_everything():
        if not isinstance(mutation, Increment):
            continue
        for field in mutation.deltas:
            qualified = f"{mutation.target.value}.{field}"
            assert qualified in numeric, f"{event_type} increments {qualified}"


async def test_the_reducers_leave_the_repository_owned_fields_alone(
    owner_database: Database,
) -> None:
    """`from_status` and `event_id` cannot be known by a pure function.

    A reducer that guessed at `from_status` would be inventing history, and
    one that wrote `event_id` would need the id of a row that does not exist
    until the transaction it is folded in.
    """
    written = {
        field
        for _, mutation in _fold_everything()
        for field in (
            mutation.deltas.keys() if isinstance(mutation, Increment) else mutation.fields.keys()
        )
    }
    assert written & REPOSITORY_OWNED == set()


#: What a Python value may be, per PostgreSQL type. `jsonb` takes anything
#: JSON-shaped, which is why it is absent: there is nothing to check.
COMPATIBLE: dict[str, tuple[type, ...]] = {
    "ARRAY": (list, tuple),
    "timestamp with time zone": (dt.datetime,),
    "integer": (int,),
    "bigint": (int,),
    "smallint": (int,),
    "double precision": (int, float),
    "numeric": (int, float, Decimal),
    "text": (str,),
    "boolean": (bool,),
    "USER-DEFINED": (str,),  # a native enum, and every one of ours is a StrEnum
}


async def test_every_value_a_reducer_writes_fits_its_column(
    owner_database: Database,
) -> None:
    """Names are not enough, which a real bug proved.

    `agents.capabilities` is `text[]` while `labels` and `runtime` are
    `jsonb`, and a reducer that treated all three alike would have written an
    object into an array column - failing the whole ingest transaction with a
    type error rather than ignoring one malformed field. The name-only check
    above passed it happily.
    """
    async with owner_database.session() as session:
        types = {
            f"{table}.{column}": data_type
            for table, column, data_type in (
                await session.execute(
                    text(
                        "SELECT table_name, column_name, data_type "
                        "  FROM information_schema.columns "
                        " WHERE table_schema = 'public' AND table_name = ANY (:tables)"
                    ),
                    {"tables": [member.value for member in MutationTarget]},
                )
            ).all()
        }

    offenders: list[str] = []
    for event_type, mutation in _fold_everything():
        written = mutation.deltas if isinstance(mutation, Increment) else mutation.fields
        for field, value in written.items():
            qualified = f"{mutation.target.value}.{field}"
            allowed = COMPATIBLE.get(types[qualified])
            # None (clearing a column) and jsonb (takes anything) both pass.
            if allowed is None or value is None:
                continue
            if not isinstance(value, allowed):
                offenders.append(
                    f"{event_type} -> {qualified} is {types[qualified]}, got {type(value).__name__}"
                )

    assert offenders == [], "a reducer writes a value its column cannot hold: " + ", ".join(
        offenders
    )
