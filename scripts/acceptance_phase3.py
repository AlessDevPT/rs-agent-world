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
| `DATABASE_URL` | Optional. Only for the extra evidence the API does not expose: the dispatch row, the event rows and the audit rows. |

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


async def _query(url: str, execution_id: str) -> dict[str, Any]:
    import asyncpg

    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        dispatches = await conn.fetch(
            "SELECT id, kind, status, attempt, claim_count, leased_by, last_error_code, "
            "side_effect_started_at IS NOT NULL AS was_in_flight "
            "FROM dispatches WHERE execution_id = $1::uuid",
            execution_id,
        )
        steps = await conn.fetch(
            "SELECT step_key, status, attempt FROM execution_steps "
            "WHERE execution_id = $1::uuid ORDER BY attempt",
            execution_id,
        )
        events = await conn.fetch(
            "SELECT name, count(*) AS n FROM events WHERE execution_id = $1::uuid "
            "GROUP BY name ORDER BY name",
            execution_id,
        )
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
        "events": {row["name"]: row["n"] for row in events},
        "audit": {row["action"]: row["n"] for row in audit},
    }


def database_evidence(execution_id: str) -> dict[str, Any]:
    """The dispatch, event and audit rows, read as the owner.

    Separate from everything above because it is not part of the public
    contract: Phase 3 exposes no dispatch read route except the needs-review
    queue, and step detail is T12's. An acceptance run still wants to see
    which dispatch ran, with what classification, and that the events were
    written - so this reads them directly and says so.

    `asyncpg`, because that is the driver this project installs; there is no
    synchronous Postgres driver in the venv.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return {"skipped": "DATABASE_URL is unset"}
    try:
        return asyncio.run(_query(url, execution_id))
    except Exception as exc:  # noqa: BLE001 - optional evidence, never fatal
        # Deliberately broad. This is diagnostics beside the acceptance run,
        # and a missing driver, a refused connection or a renamed column must
        # be reported in the output rather than failing a run that otherwise
        # succeeded against the real API and the real provider.
        return {"skipped": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------ driving ---


def success_scenario(api: Acceptance, api_key: str, model_id: str) -> bool:
    print("\n== the success scenario: a real Anthropic call ==")
    api.build_project(api_key, model_id, "success")
    workflow_id = api.publish_workflow("success")
    status = api.run_and_wait(workflow_id, "success")
    api.evidence["success_database"] = database_evidence(
        api.evidence["success_execution_id"]
    )
    if status != "completed":
        print(f"  !! the execution finished '{status}', not 'completed'")
        return False
    print("  the execution completed")
    return True


def failure_scenario(api: Acceptance, model_id: str) -> bool:
    """An unusable credential, which must be terminal rather than retried.

    This is the safe failure: a 401 from the provider classifies as
    `UNAUTHORIZED`, which `ProviderFailure.is_retryable` calls terminal - so
    the dispatch must fail on its **first** attempt, with `claim_count` 1,
    rather than burning the attempt budget on something waiting cannot fix.
    Nothing external is touched and no secret is involved.
    """
    print("\n== the failure scenario: an unusable credential ==")
    api.build_project(INVALID_KEY, model_id, "failure")
    workflow_id = api.publish_workflow("failure")
    status = api.run_and_wait(workflow_id, "failure", timeout=120.0)
    evidence = database_evidence(api.evidence["failure_execution_id"])
    api.evidence["failure_database"] = evidence

    if status != "failed":
        print(f"  !! expected 'failed', got '{status}'")
        return False
    rows = evidence.get("dispatches") or []
    if not isinstance(rows, list) or not rows:
        print("  (could not read the dispatch row; DATABASE_URL unset?)")
        return True
    row = rows[0]
    ok = True
    if row.get("last_error_code") != "unauthorized":
        print(
            f"  !! classified as {row.get('last_error_code')!r}, expected 'unauthorized'"
        )
        ok = False
    if row.get("claim_count") != 1:
        print(
            f"  !! claim_count is {row.get('claim_count')}, so a terminal failure was retried"
        )
        ok = False
    if ok:
        print("  terminal on the first attempt, classified 'unauthorized', not retried")
    return ok


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
    passed = True
    try:
        api.preflight()
        print(f"authenticated as user {api.evidence['authenticated_user_id']}")
        if not arguments.failure_only:
            passed = success_scenario(api, api_key, model_id) and passed
        if arguments.failure or arguments.failure_only:
            passed = failure_scenario(api, model_id) and passed
    except Failed as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        passed = False
    finally:
        api.close()

    print("\n== evidence ==")
    print(json.dumps(api.evidence, indent=2, default=str))
    print("\nPASSED" if passed else "\nNOT PASSED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
