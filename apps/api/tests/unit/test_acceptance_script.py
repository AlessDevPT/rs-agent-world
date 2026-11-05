"""The acceptance script must not report evidence it never read.

`scripts/acceptance_phase3.py` had three defects that reinforced each other,
and the third made the first two invisible:

1. It selected `events.name`, a column that does not exist - the column is
   `type` - so the query raised `UndefinedColumnError`.
2. It connected with `DATABASE_URL` and set no workspace. Every table it
   reads has `FORCE ROW LEVEL SECURITY` with a policy keyed on
   `agentworld.workspace_ids`, so the application role saw **0 rows from all
   four tables and no error at all**.
3. The failure scenario returned success when it could not read the dispatch
   row. Its only claim - that a terminal failure is not retried - lives in
   that row, so the check it exists for passed vacuously, every time.

The script is not on `pythonpath`, so it is loaded by path here. These are
unit tests: no database, no API, no network. The real evidence query is
exercised against a stub connection, which is enough to pin the column name
and the workspace setting - the two things that were wrong.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from typing import Any

import pytest

SCRIPT = pathlib.Path(__file__).parents[4] / "scripts" / "acceptance_phase3.py"


def _load() -> Any:
    """Load the script as a module without running it."""
    assert SCRIPT.is_file(), f"the acceptance script moved: {SCRIPT}"
    spec = importlib.util.spec_from_file_location("acceptance_phase3", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> Any:
    return _load()


# ------------------------------------------------- the query reads real rows ---


class _Conn:
    """Records what the evidence query does, and answers with canned rows."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.queries: list[str] = []
        self.closed = False

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append((sql, args))

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(sql)
        if "FROM dispatches" in sql:
            return [{"id": "d1", "status": "succeeded", "claim_count": 1}]
        if "FROM execution_steps" in sql:
            return [{"step_key": "s1", "status": "succeeded", "attempt": 1}]
        if "FROM events" in sql:
            # Keyed by `type`, so a query selecting `name` would KeyError here
            # exactly as it raised UndefinedColumnError against Postgres.
            return [{"type": "execution.started", "n": 2}]
        if "FROM audit_log" in sql:
            return [{"action": "workflow.published", "n": 1}]
        raise AssertionError(f"unexpected query: {sql}")

    async def close(self) -> None:
        self.closed = True


def _stub_asyncpg(monkeypatch: pytest.MonkeyPatch, conn: _Conn) -> None:
    module = types.ModuleType("asyncpg")

    async def connect(dsn: str) -> _Conn:
        conn.dsn = dsn  # type: ignore[attr-defined]
        return conn

    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", module)


def test_the_evidence_query_sets_the_workspace_before_reading(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the application role reads nothing, silently.

    Asserted as "before reading" rather than merely "somewhere", because a
    setting applied after the SELECTs would leave them just as empty.
    """
    conn = _Conn()
    _stub_asyncpg(monkeypatch, conn)

    asyncio.run(script._query("postgresql+asyncpg://u:p@h/db", "exec-1", "ws-1"))

    assert conn.executed, "no workspace was set, so RLS would hide every row"
    sql, args = conn.executed[0]
    assert "agentworld.workspace_ids" in sql
    assert args == ("ws-1",)
    assert conn.queries, "nothing was read"
    assert conn.closed


def test_the_events_query_names_the_column_that_exists(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`type`, not `name`. The old query could not run at all."""
    conn = _Conn()
    _stub_asyncpg(monkeypatch, conn)

    rows = asyncio.run(script._query("postgresql://u:p@h/db", "exec-1", "ws-1"))

    events = [q for q in conn.queries if "FROM events" in q]
    assert len(events) == 1
    assert "type" in events[0]
    assert "name" not in events[0]
    assert rows["events"] == {"execution.started": 2}


def test_the_driver_url_is_translated_for_asyncpg(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _Conn()
    _stub_asyncpg(monkeypatch, conn)

    asyncio.run(script._query("postgresql+asyncpg://u:p@h/db", "exec-1", "ws-1"))

    assert conn.dsn == "postgresql://u:p@h/db"


# ------------------------------------------- collection is reported honestly ---


def test_evidence_always_states_whether_it_was_collected(script: Any) -> None:
    """The flag cannot be left out of the dump, either way."""
    assert script.Evidence(collected=False, reason="why").as_json() == {
        "collected": False,
        "reason": "why",
    }
    assert script.Evidence(collected=True, rows={"events": {}}).as_json() == {
        "collected": True,
        "events": {},
    }


def test_an_unset_database_url_is_not_collected(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    evidence = script.database_evidence("exec-1", "ws-1")

    assert evidence.collected is False
    assert evidence.reason is not None
    assert evidence.dispatches == []


def test_a_failed_query_is_reported_without_the_dsn(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken read must say so, and must not quote the connection string.

    The reason carries the exception text, so an exception that named the
    password would still leak it - what is asserted here is that this
    function does not add the URL itself, which is the part it controls.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:sup3r-s3cret@h/db")

    async def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(script, "_query", boom)

    evidence = script.database_evidence("exec-1", "ws-1")

    assert evidence.collected is False
    assert "connection refused" in (evidence.reason or "")
    assert "sup3r-s3cret" not in (evidence.reason or "")
    assert "sup3r-s3cret" not in str(evidence.as_json())


# --------------------------------------------- a missing read is not a pass ---


class _Api:
    """Just enough of `Acceptance` for the scenarios to run offline."""

    def __init__(self, status: str) -> None:
        self._status = status
        self.evidence: dict[str, Any] = {}

    def build_project(self, *args: Any, **kwargs: Any) -> None:
        self.evidence["failure_workspace_id"] = "ws-1"
        self.evidence["success_workspace_id"] = "ws-1"
        self.second_agent = bool(kwargs.get("second_agent"))

    def publish_workflow(self, *args: Any, **kwargs: Any) -> str:
        self.two_step = bool(kwargs.get("two_step"))
        return "wf-1"

    def run_and_wait(self, *args: Any, **kwargs: Any) -> str:
        self.evidence["failure_execution_id"] = "exec-1"
        self.evidence["success_execution_id"] = "exec-1"
        return self._status


def _dispatch(**overrides: Any) -> dict[str, Any]:
    row = {
        "last_error_code": "unauthorized",
        "claim_count": 1,
        "finished": True,
        "status": "failed",
    }
    return {**row, **overrides}


def test_the_failure_scenario_does_not_pass_without_the_dispatch_row(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that mattered: unverified used to mean passed.

    The scenario claims a terminal failure was not retried. That claim is
    only visible in the dispatch row, so with no row there is no verdict -
    and `passed` must be False rather than True.
    """
    monkeypatch.setattr(
        script,
        "database_evidence",
        lambda *_: script.Evidence(collected=False, reason="DATABASE_URL is unset"),
    )

    result = script.failure_scenario(_Api("failed"), "model-x")

    assert result.execution_ok is True, "the execution itself did fail, as intended"
    assert result.claims_ok is False, "an unread row is not a passing check"
    assert result.passed is False


def test_the_failure_scenario_passes_on_a_terminal_unretried_row(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        script,
        "database_evidence",
        lambda *_: script.Evidence(collected=True, rows={"dispatches": [_dispatch()]}),
    )

    result = script.failure_scenario(_Api("failed"), "model-x")

    assert result.claims_ok is True
    assert result.passed is True


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"claim_count": 2}, "a terminal failure that was retried"),
        ({"last_error_code": "provider_error"}, "the wrong classification"),
        ({"finished": False}, "terminal but never stamped finished"),
    ],
)
def test_the_failure_scenario_rejects_a_row_that_breaks_its_claim(
    script: Any, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], why: str
) -> None:
    monkeypatch.setattr(
        script,
        "database_evidence",
        lambda *_: script.Evidence(collected=True, rows={"dispatches": [_dispatch(**overrides)]}),
    )

    result = script.failure_scenario(_Api("failed"), "model-x")

    assert result.claims_ok is False, why
    assert result.passed is False


def test_the_success_scenario_does_not_claim_checks_it_could_not_make(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed execution still stands; the row checks are reported absent.

    `None` rather than True is the whole point: the API's verdict is this
    scenario's acceptance criterion and is not retracted by an unreadable
    database, but nothing may print as a check that passed when it never ran.
    """
    monkeypatch.setattr(
        script,
        "database_evidence",
        lambda *_: script.Evidence(collected=False, reason="DATABASE_URL is unset"),
    )

    result = script.success_scenario(_Api("completed"), "key", "model-x")

    assert result.execution_ok is True
    assert result.claims_ok is None
    assert result.evidence.collected is False


# ------------------------------------------- the two-step success scenario ---

FIRST = "ACCEPTED"
SECOND = "RELAYED:ACCEPTED"

#: `None` is a value these tests need to pass deliberately - "the step
#: recorded no output" is one of the cases - so it cannot double as "use the
#: default".
_UNSET: Any = object()


def _two_step(
    *,
    first_output: Any = _UNSET,
    second_output: Any = _UNSET,
    relay_input: Any = _UNSET,
    dispatches: Any = None,
    step_statuses: tuple[str, str] = ("succeeded", "succeeded"),
    finished: tuple[bool, bool] = (True, True),
) -> dict[str, Any]:
    """Evidence for a healthy two-step run, with one thing varied at a time."""
    if first_output is _UNSET:
        first_output = {"text": FIRST}
    if second_output is _UNSET:
        second_output = {"text": SECOND}
    if relay_input is _UNSET:
        relay_input = {"received": FIRST}
    rows = dispatches
    if rows is None:
        rows = [
            {
                "step_key": "confirm",
                "status": "succeeded",
                "finished": finished[0],
                "instruction": {"input": {"prompt": "Say ACCEPTED"}},
            },
            {
                "step_key": "relay",
                "status": "succeeded",
                "finished": finished[1],
                "instruction": {"input": relay_input},
            },
        ]
    return {
        "dispatches": rows,
        "steps": [
            {"step_key": "confirm", "status": step_statuses[0], "output": first_output},
            {"step_key": "relay", "status": step_statuses[1], "output": second_output},
        ],
        "events": {"execution.started": 1},
    }


def _ran(script: Any, monkeypatch: pytest.MonkeyPatch, rows: dict[str, Any]) -> Any:
    monkeypatch.setattr(
        script, "database_evidence", lambda *_: script.Evidence(collected=True, rows=rows)
    )
    return script.success_scenario(_Api("completed"), "key", "model-x")


def test_the_success_scenario_asks_for_two_steps(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario must actually build the sequence, not just check for one."""
    api = _Api("completed")
    monkeypatch.setattr(
        script,
        "database_evidence",
        lambda *_: script.Evidence(collected=True, rows=_two_step()),
    )

    script.success_scenario(api, "key", "model-x")

    assert api.second_agent is True, "the relay agent was never created"
    assert api.two_step is True, "the published graph was not the two-step one"


def test_a_healthy_two_step_run_passes(script: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _ran(script, monkeypatch, _two_step())

    assert result.claims_ok is True
    assert result.passed is True


def test_the_handoff_must_carry_the_first_step_output(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim of the whole two-step scenario.

    The second step was dispatched with something, but not what the first
    step actually said - which is what a broken mapping looks like once the
    expression still resolves to *a* value.
    """
    result = _ran(script, monkeypatch, _two_step(relay_input={"received": "something else"}))

    assert result.claims_ok is False
    assert result.passed is False


def test_an_empty_mapped_input_is_not_a_handoff(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_mapped` omits a field whose expression did not resolve."""
    result = _ran(script, monkeypatch, _two_step(relay_input={}))

    assert result.claims_ok is False


def test_jsonb_arriving_as_text_is_still_read(script: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncpg hands JSONB back as `str` unless a codec is registered.

    So the readers have to parse it. Without this the whole two-step check
    would silently fail against the real database while passing here.
    """
    rows = _two_step()
    rows["steps"][0]["output"] = json.dumps({"text": FIRST})
    rows["steps"][1]["output"] = json.dumps({"text": SECOND})
    rows["dispatches"][1]["instruction"] = json.dumps({"input": {"received": FIRST}})

    result = _ran(script, monkeypatch, rows)

    assert result.claims_ok is True, "JSONB returned as text was not parsed"


def test_one_dispatch_is_not_two_steps(script: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _two_step()
    rows["dispatches"] = rows["dispatches"][:1]

    result = _ran(script, monkeypatch, rows)

    assert result.claims_ok is False


def test_a_second_step_with_no_output_means_the_second_call_never_completed(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _ran(script, monkeypatch, _two_step(second_output=None))

    assert result.claims_ok is False


def test_a_first_step_with_no_output_has_nothing_to_hand_on(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _ran(script, monkeypatch, _two_step(first_output=None))

    assert result.claims_ok is False


def test_a_failed_step_fails_the_claims(script: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _ran(script, monkeypatch, _two_step(step_statuses=("succeeded", "failed")))

    assert result.claims_ok is False


def test_a_terminal_dispatch_with_no_finish_time_fails_the_claims(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finished_at invariant, checked by the acceptance run too."""
    result = _ran(script, monkeypatch, _two_step(finished=(True, False)))

    assert result.claims_ok is False
