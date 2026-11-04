"""Phase 3 acceptance: one real execution, through the real API and worker.

    python scripts/acceptance_phase3.py            # the success scenario
    python scripts/acceptance_phase3.py --failure  # and the credential failure

This performs the Phase 3 acceptance scenario against a **running** API and a
**running** worker, with no frontend and no fake provider: publish a one-step
agent-task workflow, start it through `POST /v1/workflows/{id}/run`, and wait
for the worker to claim it, call Anthropic through the guarded client, and
advance the execution to a terminal state.

It invents nothing. Every endpoint and payload below is the one the
integration suite's `Scenario` helper uses, which is the only ground truth
for these contracts.

## What it needs

| Variable | What for |
| --- | --- |
| `AGENTWORLD_BEARER_TOKEN` | A Supabase access token for a real user. The control plane takes no API keys - `current_user` refuses them explicitly - so there is no way around this. |
| `ANTHROPIC_API_KEY` | A live key. Stored through the credentials endpoint, encrypted at rest, and never printed by this script. |
| `AGENTWORLD_API_URL` | Optional, default `http://localhost:8000`. |
| `ANTHROPIC_MODEL_ID` | Optional, default `claude-haiku-5-5` - the cheapest model that can answer the prompt. |
| `DATABASE_URL` | Optional, and the ordinary application URL - no owner privileges needed. Only for the extra evidence the API does not expose: the dispatch row, the event rows and the audit rows. Without it those checks are reported as not collected, never as passed. |

## What it never does

The key is read from the environment only - never a command-line argument,
which would put it in the shell history and the process table. It is sent to
the credentials endpoint and nowhere else, and this script prints only its
length. Provider output is truncated. Nothing here disables the guarded
client, widens the egress policy or touches authorization.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - told to the operator
    print("httpx is missing. Run: npm run api:install", file=sys.stderr)
    raise SystemExit(2) from None

TERMINAL = {"completed", "failed", "cancelled"}
#: A key that cannot work and cannot be mistaken for a real one.
INVALID_KEY = "sk-ant-invalid-acceptance-probe"


class Failed(Exception):
    """A step did not do what the contract says it does."""


def _redacted(value: str) -> str:
    return f"present ({len(value)} chars, not shown)"


class Acceptance:
    def __init__(self, base: str, token: str) -> None:
        self._client = httpx.Client(
            base_url=base.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        self.evidence: dict[str, Any] = {}

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, *, expect: int, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code != expect:
            raise Failed(
                f"{method} {path} returned {response.status_code}, expected {expect}: "
                f"{response.text[:400]}"
            )
        return response.json() if response.content else None

    # ---------------------------------------------------------- the setup ---

    def preflight(self) -> None:
        health = self._client.get("/healthz")
        if health.status_code != 200:
            raise Failed(
                f"the API is not healthy: {health.status_code} {health.text[:200]}"
            )
        me = self._client.get("/v1/me")
        if me.status_code == 503:
            raise Failed(
                "authentication is not configured on this server "
                "(SUPABASE_JWKS_URL is unset) - every control-plane route is a 503"
            )
        if me.status_code != 200:
            raise Failed(f"GET /v1/me returned {me.status_code}: {me.text[:300]}")
        self.evidence["authenticated_user_id"] = me.json()["user"]["id"]

    def build_project(self, api_key: str, model_id: str, label: str) -> None:
        """Workspace, project, credential, model profile, agent - in order.

        The order is forced: a model profile needs a credential, an agent
        needs a profile, and a workflow needs an agent whose key it names.
        """
        suffix = uuid.uuid4().hex[:8]
        workspace = self._call(
            "POST",
            "/v1/workspaces",
            expect=201,
            json={"name": f"Acceptance {label} {suffix}"},
        )
        project = self._call(
            "POST",
            f"/v1/workspaces/{workspace['id']}/projects",
            expect=201,
            json={"name": f"Phase 3 acceptance {suffix}"},
        )
        credential = self._call(
            "POST",
            f"/v1/workspaces/{workspace['id']}/credentials",
            expect=201,
            json={
                "name": "Anthropic acceptance key",
                "kind": "api_key",
                "provider_slug": "anthropic",
                # The only place the key is sent. Encrypted at rest by
                # `EnvelopeSecretStore`; no endpoint can read it back.
                "secret": api_key,
            },
        )
        profile = self._call(
            "POST",
            f"/v1/workspaces/{workspace['id']}/model-profiles",
            expect=201,
            json={
                "name": "Acceptance model",
                "provider_slug": "anthropic",
                "model_id": model_id,
                "credential_id": credential["id"],
                # No `base_url`, so the adapter uses its default -
                # https://api.anthropic.com - which is the point of the run.
                "parameters": {"max_output_tokens": 64},
            },
        )
        agent = self._call(
            "POST",
            f"/v1/projects/{project['id']}/agents",
            expect=201,
            json={
                "display_name": "Acceptance agent",
                "agent_key": "acceptance",
                "interaction_mode": "managed",
                "instructions": (
                    "Reply with exactly the word ACCEPTED and nothing else. "
                    "Do not explain."
                ),
            },
        )
        self._call(
            "PATCH",
            f"/v1/projects/{project['id']}/agents/{agent['id']}",
            expect=200,
            json={"model_profile_id": profile["id"]},
        )
        self.workspace_id = workspace["id"]
        self.project_id = project["id"]
        self.evidence[f"{label}_workspace_id"] = workspace["id"]
        self.evidence[f"{label}_project_id"] = project["id"]
        self.evidence[f"{label}_credential_id"] = credential["id"]
        self.evidence[f"{label}_model_profile_id"] = profile["id"]
        self.evidence[f"{label}_model_id"] = model_id
        self.evidence[f"{label}_agent_id"] = agent["id"]

    def publish_workflow(self, label: str) -> str:
        created = self._call(
            "POST",
            f"/v1/projects/{self.project_id}/workflows",
            expect=201,
            json={
                "name": "Acceptance",
                "graph": {
                    "steps": [
                        {
                            "step_key": "confirm",
                            "kind": "agent_task",
                            "agent_key": "acceptance",
                            "input_mapping": {"prompt": "$.input.prompt"},
                        }
                    ],
                    "edges": [],
                },
            },
        )
        workflow_id = created["workflow"]["id"]
        published = self._call(
            "POST",
            f"/v1/projects/{self.project_id}/workflows/{workflow_id}/publish",
            expect=201,
        )
        self.evidence[f"{label}_workflow_id"] = workflow_id
        # `PublishedVersion` is {version_id, published_at}. Reading `id` here
        # was wrong and silently produced a null in the evidence.
        self.evidence[f"{label}_published_version_id"] = published["version_id"]
        self.evidence[f"{label}_published_at"] = published["published_at"]
        return workflow_id

    # ------------------------------------------------------- the execution ---

    def run_and_wait(
        self, workflow_id: str, label: str, *, timeout: float = 180.0
    ) -> str:
        started = self._call(
            "POST",
            f"/v1/projects/{self.project_id}/workflows/{workflow_id}/run",
            expect=202,
            json={"input": {"prompt": "Reply with exactly: ACCEPTED"}},
        )
        execution_id = started["id"]
        self.evidence[f"{label}_execution_id"] = execution_id
        print(f"  started execution {execution_id}; waiting for the worker")

        deadline = time.monotonic() + timeout
        seen: set[str] = set()
        while time.monotonic() < deadline:
            view = self._call(
                "GET",
                f"/v1/projects/{self.project_id}/executions/{execution_id}",
                expect=200,
            )
            status = str(view["status"])
            if status not in seen:
                print(f"  status: {status}")
                seen.add(status)
            if status in TERMINAL:
                self.evidence[f"{label}_final_status"] = status
                self.evidence[f"{label}_output"] = _truncate(view.get("output"))
                self.evidence[f"{label}_error"] = view.get("error")
                self.evidence[f"{label}_started_at"] = view.get("started_at")
                self.evidence[f"{label}_finished_at"] = view.get("finished_at")
                return status
            time.sleep(1.0)

        raise Failed(
            f"the execution was still '{status}' after {timeout:.0f}s. If it never left "
            "'running', nothing is performing dispatches - start the worker: "
            "npm run api:worker"
        )


def _truncate(output: Any, limit: int = 240) -> Any:
    """Provider text, bounded. The run is a demonstration, not a transcript."""
    if not isinstance(output, dict):
        return output
    trimmed: dict[str, Any] = {}
    for key, value in output.items():
        if isinstance(value, str) and len(value) > limit:
            trimmed[key] = value[:limit] + f"… ({len(value)} chars total)"
        else:
            trimmed[key] = value
    return trimmed


# ------------------------------------- evidence the API does not expose yet ---


async def _query(url: str, execution_id: str, workspace_id: str) -> dict[str, Any]:
    """Read the rows the API does not expose, as the application role.

    **The row-level security has to be satisfied, not avoided.** Every table
    here - `dispatches`, `events`, `audit_log`, `execution_steps` - has
    `FORCE ROW LEVEL SECURITY` and one `ALL` policy keyed on
    `agentworld.workspace_ids`, so the application role sees nothing until
    that setting names a workspace. Measured against this database: with the
    setting unset the role reads **0 rows from all four tables and raises
    nothing**; with it set to the run's workspace it reads exactly what the
    owner reads. Silence, not an error - which is why the earlier version of
    this script reported empty evidence as though there were none to find.

    So the fix is to set it - the same mechanism `TenantScope` uses on every
    application query - rather than to connect as the owner. No owner
    credentials, no `BYPASSRLS`, no extra grant, and no policy is weakened:
    the evidence is scoped to the one workspace this run created, which is
    all it ever wanted to see.

    `set_config(..., false)` for the session rather than `SET LOCAL`, because
    these reads are not inside a transaction.
    """
    import asyncpg

    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "SELECT set_config('agentworld.workspace_ids', $1, false)", workspace_id
        )
        dispatches = await conn.fetch(
            "SELECT id, kind, status, attempt, claim_count, leased_by, last_error_code, "
            "finished_at IS NOT NULL AS finished, "
            "side_effect_started_at IS NOT NULL AS was_in_flight "
            "FROM dispatches WHERE execution_id = $1::uuid",
            execution_id,
        )
        steps = await conn.fetch(
            "SELECT step_key, status, attempt FROM execution_steps "
            "WHERE execution_id = $1::uuid ORDER BY attempt",
            execution_id,
        )
        # `type`, not `name`. The column has always been `type`, so the old
        # query raised UndefinedColumnError - which the broad except turned
        # into a "skipped" line while the run still called itself passed.
        events = await conn.fetch(
            "SELECT type, count(*) AS n FROM events WHERE execution_id = $1::uuid "
            "GROUP BY type ORDER BY type",
            execution_id,
        )
        # Workspace-scoped by the policy now, where this used to count every
        # matching row in the table.
        audit = await conn.fetch(
            "SELECT action, count(*) AS n FROM audit_log "
            "WHERE action LIKE 'workflow.%' OR action LIKE 'dispatch.%' "
            "GROUP BY action ORDER BY action"
        )
    finally:
        await conn.close()

    return {
        "dispatches": [dict(row) for row in dispatches],
        "steps": [dict(row) for row in steps],
        "events": {row["type"]: row["n"] for row in events},
        "audit": {row["action"]: row["n"] for row in audit},
    }


class Evidence:
    """Database rows, and whether they were actually read.

    The point of the class is that `collected` cannot be left out. The
    previous version returned a plain dict that was either the rows or
    `{"skipped": reason}`, and the caller tested `evidence.get("dispatches")`
    - falsy both when the dispatch did not exist and when nothing was read at
    all. The failure scenario treated the second as the first and returned
    success. An absent measurement is not a passing measurement.
    """

    __slots__ = ("collected", "reason", "rows")

    def __init__(
        self,
        *,
        collected: bool,
        rows: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        self.collected = collected
        self.rows = rows or {}
        self.reason = reason

    @property
    def dispatches(self) -> list[dict[str, Any]]:
        rows = self.rows.get("dispatches")
        return rows if isinstance(rows, list) else []

    def as_json(self) -> dict[str, Any]:
        """What goes in the evidence dump: the verdict first, then the rows."""
        if not self.collected:
            return {"collected": False, "reason": self.reason}
        return {"collected": True, **self.rows}


def database_evidence(execution_id: str, workspace_id: str) -> Evidence:
    """The dispatch, event and audit rows, or an explicit statement of why not.

    Separate from everything above because it is not part of the public
    contract: Phase 3 exposes no dispatch read route except the needs-review
    queue, and step detail is T12's. An acceptance run still wants to see
    which dispatch ran, with what classification, and that the events were
    written - so this reads them directly and says, either way, whether it
    managed to.

    `asyncpg`, because that is the driver this project installs; there is no
    synchronous Postgres driver in the venv.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return Evidence(collected=False, reason="DATABASE_URL is unset")
    try:
        rows = asyncio.run(_query(url, execution_id, workspace_id))
    except Exception as exc:  # noqa: BLE001 - reported, never silently dropped
        # Still broad - a missing driver or a refused connection should not
        # look like a crash in the acceptance run itself - but the reason now
        # travels with `collected=False`, so no caller can read it as a pass.
        # `url` is deliberately not interpolated: a DSN carries a password.
        return Evidence(collected=False, reason=f"{type(exc).__name__}: {exc}")
    return Evidence(collected=True, rows=rows)


# ------------------------------------------------------------------ driving ---


class ScenarioResult:
    """Two verdicts, kept apart on purpose.

    `execution_ok` is what the API said the execution did. `claims_ok` is
    whether the things only the database can show were checked and held:
    `None` when the scenario makes no such claim, `False` when it makes one
    that could not be checked. Collapsing the two is the bug this replaces -
    a run whose evidence never arrived used to print PASSED.
    """

    __slots__ = ("claims_ok", "evidence", "execution_ok", "name")

    def __init__(
        self,
        name: str,
        *,
        execution_ok: bool,
        evidence: Evidence,
        claims_ok: bool | None = None,
    ) -> None:
        self.name = name
        self.execution_ok = execution_ok
        self.evidence = evidence
        self.claims_ok = claims_ok

    @property
    def passed(self) -> bool:
        return self.execution_ok and self.claims_ok is not False


def _report_evidence(evidence: Evidence) -> None:
    """Say what happened to the database read, every time, either way."""
    if not evidence.collected:
        print(f"  database evidence: NOT COLLECTED - {evidence.reason}")
        return
    counts = ", ".join(
        f"{len(value) if isinstance(value, (list, dict)) else value} {key}"
        for key, value in evidence.rows.items()
    )
    print(f"  database evidence: collected ({counts})")


def success_scenario(api: Acceptance, api_key: str, model_id: str) -> ScenarioResult:
    print("\n== the success scenario: a real Anthropic call ==")
    api.build_project(api_key, model_id, "success")
    workflow_id = api.publish_workflow("success")
    status = api.run_and_wait(workflow_id, "success")
    evidence = database_evidence(
        api.evidence["success_execution_id"], api.evidence["success_workspace_id"]
    )
    api.evidence["success_database"] = evidence.as_json()
    _report_evidence(evidence)

    if status != "completed":
        print(f"  !! the execution finished '{status}', not 'completed'")
        return ScenarioResult("success", execution_ok=False, evidence=evidence)
    print("  the execution completed")

    # The API's verdict is this scenario's acceptance criterion, so a missing
    # database read does not retract it - but it is not reported as a check
    # that passed either. `claims_ok` stays None when nothing was checked.
    if not evidence.collected:
        return ScenarioResult("success", execution_ok=True, evidence=evidence)

    claims_ok = True
    rows = evidence.dispatches
    if not rows:
        print("  !! the execution completed with no dispatch row")
        claims_ok = False
    elif rows[0].get("status") != "succeeded":
        print(f"  !! the dispatch is '{rows[0].get('status')}', not 'succeeded'")
        claims_ok = False
    elif not rows[0].get("finished"):
        print("  !! the dispatch is terminal but finished_at is NULL")
        claims_ok = False
    if not evidence.rows.get("events"):
        print("  !! no events were written for this execution")
        claims_ok = False
    if claims_ok:
        print("  the dispatch succeeded, is stamped finished, and events were written")
    return ScenarioResult(
        "success", execution_ok=True, evidence=evidence, claims_ok=claims_ok
    )


def failure_scenario(api: Acceptance, model_id: str) -> ScenarioResult:
    """An unusable credential, which must be terminal rather than retried.

    This is the safe failure: a 401 from the provider classifies as
    `UNAUTHORIZED`, which `ProviderFailure.is_retryable` calls terminal - so
    the dispatch must fail on its **first** attempt, with `claim_count` 1,
    rather than burning the attempt budget on something waiting cannot fix.
    Nothing external is touched and no secret is involved.

    Both of those are properties of the dispatch row, so this scenario cannot
    reach a verdict without the database. It used to return success when it
    could not read one, which made the only check it exists for vacuous.
    """
    print("\n== the failure scenario: an unusable credential ==")
    api.build_project(INVALID_KEY, model_id, "failure")
    workflow_id = api.publish_workflow("failure")
    status = api.run_and_wait(workflow_id, "failure", timeout=120.0)
    evidence = database_evidence(
        api.evidence["failure_execution_id"], api.evidence["failure_workspace_id"]
    )
    api.evidence["failure_database"] = evidence.as_json()
    _report_evidence(evidence)

    execution_ok = status == "failed"
    if not execution_ok:
        print(f"  !! expected 'failed', got '{status}'")

    if not evidence.collected:
        print(
            "  !! NOT VERIFIED: this scenario's whole claim - terminal on the first"
            "\n     attempt, classified 'unauthorized' - lives in the dispatch row,"
            f"\n     and the row could not be read ({evidence.reason})."
            "\n     Not treated as a pass."
        )
        return ScenarioResult(
            "failure", execution_ok=execution_ok, evidence=evidence, claims_ok=False
        )

    rows = evidence.dispatches
    if not rows:
        print("  !! no dispatch row exists for this execution, so nothing ran")
        return ScenarioResult(
            "failure", execution_ok=execution_ok, evidence=evidence, claims_ok=False
        )

    row = rows[0]
    claims_ok = True
    if row.get("last_error_code") != "unauthorized":
        print(
            f"  !! classified as {row.get('last_error_code')!r}, expected 'unauthorized'"
        )
        claims_ok = False
    if row.get("claim_count") != 1:
        print(
            f"  !! claim_count is {row.get('claim_count')}, so a terminal failure was retried"
        )
        claims_ok = False
    if not row.get("finished"):
        print("  !! the dispatch is terminal but finished_at is NULL")
        claims_ok = False
    if claims_ok:
        print("  terminal on the first attempt, classified 'unauthorized', not retried")
    return ScenarioResult(
        "failure", execution_ok=execution_ok, evidence=evidence, claims_ok=claims_ok
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="acceptance_phase3",
        description="Run the Phase 3 acceptance scenario against a live API and worker.",
    )
    parser.add_argument(
        "--failure",
        action="store_true",
        help="Also run the unusable-credential failure scenario.",
    )
    parser.add_argument(
        "--failure-only", action="store_true", help="Run only the failure scenario."
    )
    arguments = parser.parse_args()

    base = os.environ.get("AGENTWORLD_API_URL", "http://localhost:8000")
    token = os.environ.get("AGENTWORLD_BEARER_TOKEN", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    model_id = os.environ.get("ANTHROPIC_MODEL_ID", "claude-haiku-5-5")

    missing = [
        name
        for name, value in (
            ("AGENTWORLD_BEARER_TOKEN", token),
            ("ANTHROPIC_API_KEY", api_key),
        )
        if not value and not (name == "ANTHROPIC_API_KEY" and arguments.failure_only)
    ]
    if missing:
        print(
            f"missing required environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            "\nAGENTWORLD_BEARER_TOKEN is a Supabase access token for a real user; the\n"
            "control plane refuses API keys. ANTHROPIC_API_KEY must be a live key - this\n"
            "script reads it from the environment only and never prints it.",
            file=sys.stderr,
        )
        return 2

    print(f"API            : {base}")
    print(f"bearer token   : {_redacted(token)}")
    print(
        f"anthropic key  : {_redacted(api_key) if api_key else 'not needed (--failure-only)'}"
    )
    print(f"model          : {model_id}")

    api = Acceptance(base, token)
    results: list[ScenarioResult] = []
    crashed = False
    try:
        api.preflight()
        print(f"authenticated as user {api.evidence['authenticated_user_id']}")
        if not arguments.failure_only:
            results.append(success_scenario(api, api_key, model_id))
        if arguments.failure or arguments.failure_only:
            results.append(failure_scenario(api, model_id))
    except Failed as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        crashed = True
    finally:
        api.close()

    print("\n== evidence ==")
    print(json.dumps(api.evidence, indent=2, default=str))

    # Reported separately, because they fail for different reasons and a
    # reader needs to know which one did. An execution that ran correctly
    # while the evidence read failed is a configuration problem; an execution
    # that misbehaved is a defect. The old single PASSED said neither, and
    # printed itself even when no evidence had been read at all.
    print("\n== result ==")
    if crashed:
        print("  scenarios        : NOT PASSED (the run did not finish)")
    for result in results:
        execution = "passed" if result.execution_ok else "NOT PASSED"
        print(f"  {result.name + ' execution':<17}: {execution}")
        if result.claims_ok is None:
            checks = "not checked - no database evidence"
        else:
            checks = "passed" if result.claims_ok else "NOT PASSED"
        print(f"  {result.name + ' row checks':<17}: {checks}")

    unread = [r.name for r in results if not r.evidence.collected]
    if unread:
        print(f"  database evidence: NOT COLLECTED for {', '.join(unread)}")
    elif results:
        print("  database evidence: collected")

    passed = not crashed and bool(results) and all(r.passed for r in results)
    print("\nPASSED" if passed else "\nNOT PASSED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
