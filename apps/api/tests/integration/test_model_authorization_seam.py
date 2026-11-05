"""C19: a managed model step stays plannable without a stored credential.

The decision (`04-ROADMAP-AND-MVP.md` §2, Phase 4) is that a model turn must
be able to run **either** against a provider API credential the workspace
holds **or** on an AI runtime a user has personally authorized - and the
second cannot be assumed to be reachable as a conventional API. A personal
subscription may have no server-callable endpoint at all, so there is nothing
to store and nothing to replay.

Phase 4 owes that decision no features, only restraint, which is exactly the
kind of obligation that quietly dies during implementation: a `NOT NULL`, a
validation finding, an early `if not credential: refuse`, and the axis is
closed with every test still green. **This file is the practical test that
C19 survived implementation rather than merely being documented.**

What it pins is the *location* of the credential requirement, not its
existence. Today a credential-free profile does fail - that is correct and
expected, because no runtime exists to select yet (Phase 7, O4). What must
stay true is **where** it fails:

    API accepts the profile  ->  validate still calls the draft publishable
                             ->  advance() plans and queues the step
                             ->  the worker claims it
                             ->  the runner refuses when it would build a provider

Every arrow before the last one must keep working. The moment the refusal
moves earlier - to the schema, to `validate`, or to `advance()` - a personal
runtime becomes unrepresentable, and the cost of discovering that in Phase 7
is a migration on a populated table plus a change to the one code path every
execution runs through.

The runner's own classification is the evidence. `StepRunner.perform` catches
`NotFoundError` and `ConflictError` out of `_plan` and settles the dispatch
`dead` with `error_code='misconfigured'`, keeping `exc.detail` - "a
misconfigured agent is terminal: it will be just as misconfigured in thirty
seconds". So the persisted message names the credential, which places the
refusal at the exact line that may still require one, and `dead` proves it is
not a retry loop burning the attempt budget on a configuration problem.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_dispatch_worker import FakeProvider, Scenario, _dispatch_row, _worker

from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from helpers import Actor


@pytest.fixture
async def provider() -> Any:
    """`test_dispatch_worker`'s fake provider, which is module-local there."""
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


async def _last_error(owner_database: Database, execution_id: Any) -> str | None:
    """The persisted failure message, which `_dispatch_row` does not select."""
    async with owner_database.session() as session:
        return (
            await session.execute(
                text("SELECT last_error FROM dispatches WHERE execution_id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()


def _credential_free_profile(scenario: Scenario) -> str:
    """A model profile with no credential, created through the public API.

    Through the API rather than by inserting a row, because "the column is
    nullable" is not the claim. The claim is that an operator can configure
    one, which means the request schema has to accept the omission too.
    """
    created = scenario.client.post(
        f"/v1/workspaces/{scenario.workspace['id']}/model-profiles",
        json={
            "name": "Authorized runtime, no stored key",
            "provider_slug": "anthropic",
            "model_id": "test-model",
            "parameters": {
                "base_url": scenario.provider.base_url,
                "max_output_tokens": 64,
            },
        },
        headers=scenario.actor.headers,
    )
    assert created.status_code == 201, (
        "the public surface must accept a model profile with no credential - "
        "without it a user-authorized runtime could never be configured at all: "
        f"{created.text}"
    )
    assert created.json()["credential_id"] is None, (
        "a profile created without a credential must not acquire one"
    )
    return str(created.json()["id"])


async def test_a_managed_step_is_plannable_without_a_stored_credential(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    provider: FakeProvider,
) -> None:
    """The whole seam, in the order a real execution walks it."""
    profile_id = _credential_free_profile(scenario)
    repointed = scenario.client.patch(
        f"/v1/projects/{scenario.project['id']}/agents/{scenario.agent['id']}",
        json={"model_profile_id": profile_id},
        headers=scenario.actor.headers,
    )
    assert repointed.status_code == 200, (
        f"an agent must be assignable to a credential-free profile: {repointed.text}"
    )

    # 1. The draft is still publishable. `validate` is the other place a
    #    credential check would plausibly land, and it is the worst place for
    #    one: it runs before any execution exists, so a finding here makes a
    #    runtime-backed agent look broken in the editor. Asserted explicitly
    #    because `Scenario` publishes during construction, before the repoint
    #    above - without this call the credential-free profile would never
    #    meet the validator at all.
    report = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/workflows/{scenario.workflow_id}/validate",
        headers=scenario.actor.headers,
    )
    assert report.status_code == 200, report.text
    findings = report.json()["findings"]
    assert report.json()["publishable"] is True, (
        f"validate refuses a draft whose agent has a credential-free profile: {findings}"
    )
    assert not [f for f in findings if "credential" in str(f).lower()], (
        f"validate raised a credential finding: {findings}"
    )

    execution_id = scenario.run()

    # 2. The orchestrator planned and queued the turn with no credential in
    #    sight. `advance()` names no credential anywhere, and this is what
    #    holds it to that.
    queued = await _dispatch_row(owner_database, execution_id)
    assert queued is not None, (
        "advance() produced no dispatch for a managed step whose profile has no "
        "credential. C19 requires the orchestrator to be credential-free: the "
        "refusal belongs at provider construction, not here"
    )
    assert queued.status == "pending", (
        f"the dispatch was queued as {queued.status!r} rather than 'pending'"
    )
    assert queued.last_error_code is None, "nothing has failed yet"

    # 3. It is claimable. Dispatchable work, not work refused up front.
    claimed = await _worker(factory, provider).run_once()
    assert claimed is not None, (
        "the credential-free dispatch was never claimed, so the step is queued but "
        "unreachable - the seam is closed one layer later than it looks"
    )

    # 4. The refusal happened inside the runner, where a runtime would have
    #    been selected - and before any outbound call.
    settled = await _dispatch_row(owner_database, execution_id)
    assert settled.claim_count == 1, (
        f"claim_count is {settled.claim_count}: the runner ran other than once"
    )
    assert settled.last_error_code == "misconfigured", (
        f"the failure is classified {settled.last_error_code!r}. It must be the runner "
        "recognising a configuration problem while gathering what the call needs - not "
        "a crash ('runner_error'), and not a provider failure"
    )
    assert settled.status == "dead", (
        f"the dispatch is {settled.status!r}. A missing credential is terminal, not "
        "retryable: waiting does not conjure one, and retrying spends the attempt "
        "budget to learn the same thing"
    )
    detail = await _last_error(owner_database, execution_id)
    assert detail is not None and "credential" in detail.lower(), (
        f"the recorded failure reads {detail!r}. It must name the missing credential, "
        "which is what locates the refusal at the one line permitted to require one. "
        "A different message here means something else refused first"
    )
    assert provider.requests == [], (
        "a provider call was made for a profile with no credential. The refusal must "
        "precede the outbound call, or an unauthorized turn could reach a vendor"
    )
    assert settled.side_effect_started_at is None, (
        "nothing went out, so nothing may be recorded as possibly in flight"
    )


def test_the_orchestrator_names_no_credential() -> None:
    """`advance()` must not learn about credentials.

    The structural half of C19, and the cheap one. Asserted the same way the
    vendor-containment contract is asserted - by reading the source - because
    the property is "this module does not know about that concept", which no
    behavioural test states as plainly. If a credential check is ever needed
    to decide the *next step*, that is a real architectural change and should
    fail here first and be argued for, rather than appearing as one `if`.
    """
    # The package re-exports `advance` as a function, so importing the name
    # from the package gives the callable rather than the module it lives in.
    module = importlib.import_module("agentworld.domain.orchestration.advance")
    source = inspect.getsource(module)

    assert "credential" not in source.lower(), (
        "domain/orchestration/advance.py now mentions a credential. The orchestrator "
        "decides what happens next; whether the workspace holds a key for it is the "
        "runner's question, and keeping that out of here is what leaves room for a "
        "user-authorized runtime (C19)"
    )
