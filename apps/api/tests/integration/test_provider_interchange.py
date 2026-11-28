"""One published workflow, run through both providers.

This is T13's acceptance criterion, and the thing it actually proves is that
provider choice is a *configuration* decision rather than a property of a
workflow: the published version is written once, and the only edit between
the two runs is which model profile the agent points at.

That works because of a design decision worth naming, since this test is
what would notice it changing. A published `workflow_steps` row pins
`agent_id` and carries no model profile; the dispatch instruction carries
`agent_key`; and `StepRunner._plan` resolves agent -> `model_profile_id` ->
provider slug, model id, credential and base URL at dispatch time. So
re-pointing the agent changes the provider without touching the published
graph, and both executions report the same `workflow_version_id` - which is
asserted here rather than assumed.

Both legs go through the real worker, the real credential decryption and the
real guarded client to a real socket. The two providers are told apart by
what arrived on the wire: the first leg must carry the Anthropic key and its
token-cap field, the second must carry a bearer token for a *different*
credential and the chat-completions field. Nothing here contacts a real
provider, and neither "key" is real.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_dispatch_worker import API_KEY, FakeProvider, Scenario, _states, _step_output, _worker

from agentworld.domain.enums import ExecutionStatus, StepStatus
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from helpers import Actor

pytestmark = pytest.mark.integration

#: The second credential. Distinct from `API_KEY` on purpose: the only way to
#: prove the second leg used the second credential is for the bearer token to
#: be a string the first leg could not have produced.
OPENAI_KEY = "sk-test-ZZinterchangeZZ-0002"

#: Two answers, so a step output identifies which provider produced it. If
#: both legs returned the same text, a test that re-ran the first provider
#: twice would pass.
FIRST_ANSWER = "Four competitors, and one of them is new."
SECOND_ANSWER = "Three competitors, and the new one withdrew."

#: The agent's instructions, set by `Scenario`. Asserted on both legs because
#: the two providers carry a system prompt differently - a top-level field
#: against a first message - and the point is that the caller never knows.
INSTRUCTIONS = "Be brief."

#: `Scenario` stores this on the Anthropic profile; the second profile gets
#: the same value so that provider choice is the only variable between runs.
MAX_OUTPUT_TOKENS = 64

#: A test value. This test says nothing about real model naming - the model
#: id is a string the adapter forwards and this layer never interprets.
SECOND_MODEL_ID = "test-model-compatible"


@pytest.fixture
async def provider() -> Any:
    """The fake provider server, re-exported from the dispatch suite.

    A fixture cannot be imported by name, so the established pattern in this
    directory is to re-enter the upstream generator. See
    `test_agent_test_execution.py`, which does the same.
    """
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


def _anthropic_answer(text_: str) -> dict[str, Any]:
    """A Messages-API response, in the shape that suite's fake already uses."""
    return {
        "id": "msg_01",
        "model": "test-model",
        "content": [{"type": "text", "text": text_}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 4},
    }


def _chat_completions_answer(text_: str) -> dict[str, Any]:
    """A chat-completions response.

    Deliberately a different shape rather than a renamed one: the answer is
    inside `choices[0].message`, the stop reason is `finish_reason`, and the
    token counts are `prompt_tokens` and `completion_tokens`. If the adapter
    read these like the other provider's, the step would fail as malformed
    rather than quietly returning nothing.
    """
    return {
        "id": "chatcmpl-01",
        "object": "chat.completion",
        "created": 1764300000,
        "model": SECOND_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text_},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 17, "completion_tokens": 6, "total_tokens": 23},
    }


async def test_one_published_workflow_runs_through_both_providers(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The acceptance criterion, end to end and offline.

    The fake answers from a queue, one document per call, so it needs to know
    nothing about either protocol - which is also why this test can assert
    the protocols itself, from what arrived, rather than trusting a fake to
    have branched correctly.
    """
    provider.responses = [
        _anthropic_answer(FIRST_ANSWER),
        _chat_completions_answer(SECOND_ANSWER),
    ]
    worker = _worker(factory, provider)
    client, actor = scenario.client, scenario.actor
    workspace_id = scenario.workspace["id"]
    project_id = scenario.project_id

    # ----------------------------------------------------------- leg one ---

    first = scenario.run()
    claimed = await worker.run_once()
    assert claimed is not None
    assert claimed.step_key == "research"

    execution, step = await _states(owner_database, first)
    assert execution == ExecutionStatus.COMPLETED
    assert step == StepStatus.SUCCEEDED
    assert (await _step_output(owner_database, first))["text"] == FIRST_ANSWER

    assert provider.requests[0]["x-api-key"] == API_KEY
    sent = provider.captured[0]
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["system"] == INSTRUCTIONS, "this provider carries it out of band"
    assert "max_completion_tokens" not in sent

    # ------------------------------------------- re-point, nothing else ---

    credential = client.post(
        f"/v1/workspaces/{workspace_id}/credentials",
        json={
            "name": "Compatible key",
            "kind": "api_key",
            "provider_slug": "openai",
            "secret": OPENAI_KEY,
        },
        headers=actor.headers,
    )
    assert credential.status_code == 201, credential.text

    profile = client.post(
        f"/v1/workspaces/{workspace_id}/model-profiles",
        json={
            "name": "Compatible model",
            "provider_slug": "openai",
            "model_id": SECOND_MODEL_ID,
            "credential_id": credential.json()["id"],
            # The `/v1` belongs to the stored base URL for this provider,
            # whose adapter appends only `/chat/completions`. It is here so
            # the profile is shaped the way an operator's would be - and
            # **not** as coverage of that convention. This fake records
            # headers and bodies and discards the request line, so nothing
            # below would notice a wrong path: a base URL producing
            # `/v1/v1/chat/completions` would be answered and this test
            # would pass. What asserts the prefix survives is the unit wire
            # test, whose fake records the target.
            "parameters": {
                "base_url": f"{provider.base_url}/v1",
                "max_output_tokens": MAX_OUTPUT_TOKENS,
            },
        },
        headers=actor.headers,
    )
    assert profile.status_code == 201, profile.text

    # The whole switch: one field on the agent. No republish, no new
    # workflow, no second agent - the published step pins `agent_id`, so a
    # second agent would have meant a second version and a different test.
    repointed = client.patch(
        f"/v1/projects/{project_id}/agents/{scenario.agent['id']}",
        json={"model_profile_id": profile.json()["id"]},
        headers=actor.headers,
    )
    assert repointed.status_code == 200, repointed.text

    # ----------------------------------------------------------- leg two ---

    second = scenario.run()
    claimed = await worker.run_once()
    assert claimed is not None
    assert claimed.step_key == "research"

    execution, step = await _states(owner_database, second)
    assert execution == ExecutionStatus.COMPLETED
    assert step == StepStatus.SUCCEEDED
    assert (await _step_output(owner_database, second))["text"] == SECOND_ANSWER

    # A bearer token for the *other* credential, which is how this asserts
    # that the second credential was resolved and decrypted rather than the
    # first one being reused under a new header name.
    assert provider.requests[1]["authorization"] == f"Bearer {OPENAI_KEY}"
    assert provider.requests[1].get("x-api-key") is None
    sent = provider.captured[1]
    assert sent["max_completion_tokens"] == MAX_OUTPUT_TOKENS
    assert "max_tokens" not in sent, "the deprecated field is never sent"
    assert sent["messages"][0] == {"role": "system", "content": INSTRUCTIONS}

    # ------------------------------------------------- the same workflow ---

    views = [
        client.get(f"/v1/projects/{project_id}/executions/{run}", headers=actor.headers).json()
        for run in (first, second)
    ]
    assert views[0]["workflow_version_id"] == views[1]["workflow_version_id"], (
        "the two runs executed different published versions, "
        "so this proves nothing about one workflow"
    )

    versions = client.get(
        f"/v1/projects/{project_id}/workflows/{scenario.workflow_id}/versions",
        headers=actor.headers,
    )
    assert versions.status_code == 200, versions.text
    published = versions.json()
    assert len(published) == 1, "something republished between the runs"
    assert published[0]["id"] == views[0]["workflow_version_id"]
    assert published[0]["is_current"] is True
