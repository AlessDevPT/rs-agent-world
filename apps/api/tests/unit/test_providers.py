"""The Anthropic adapter's wire shape, and the failure vocabulary.

**What is left here is what should name a vendor.** The behaviour every
provider owes a caller moved to `test_provider_conformance.py`, which is
parametrised over `provider_cases.CASES` and names none; this file keeps the
assertions that are only meaningful about the Messages API - its endpoint,
its header names, its request body and its content blocks - which is what
T13's criterion means by *"with the Anthropic wire-shape test left beside
the adapter it describes"*.

`test_every_failure_member_has_a_retryability_answer` stays too. It is about
`ProviderFailure` rather than about any provider, so parametrising it over
the cases would run the same assertions once per implementation and claim
something about each.

The requests still go through the real `GuardedClient` to a real socket; see
`provider_cases` for why that is not a mocked transport.
"""

from __future__ import annotations

from provider_cases import ANTHROPIC, FakeProvider, anthropic_answer, build, request

from agentworld.infra import providers
from agentworld.infra.providers import anthropic


async def test_the_request_is_the_documented_shape(fake: FakeProvider) -> None:
    fake.payload = anthropic_answer()
    await build(ANTHROPIC, fake).complete(
        request(
            ANTHROPIC,
            system="Be brief.",
            temperature=0.2,
            stop_sequences=["STOP"],
            max_output_tokens=99,
        )
    )

    sent = fake.requests[0]
    assert sent.method == "POST"
    assert sent.target == "/v1/messages"
    assert sent.headers["x-api-key"] == ANTHROPIC.api_key
    assert sent.headers["anthropic-version"] == anthropic.API_VERSION
    assert sent.json == {
        "model": ANTHROPIC.model_id,
        "max_tokens": 99,
        "messages": [{"role": "user", "content": "2+2?"}],
        "system": "Be brief.",
        "temperature": 0.2,
        "stop_sequences": ["STOP"],
    }


async def test_optional_fields_are_omitted_rather_than_sent_as_null(
    fake: FakeProvider,
) -> None:
    """The field names are this provider's; that none is null is every
    provider's, and is asserted in the conformance suite as well."""
    fake.payload = anthropic_answer()
    await build(ANTHROPIC, fake).complete(request(ANTHROPIC))
    assert set(fake.requests[0].json) == {"model", "max_tokens", "messages"}


async def test_an_idempotency_token_is_forwarded_when_there_is_one(
    fake: FakeProvider,
) -> None:
    """A retry after an uncertain outcome must not be billed or served twice.

    The header name is this provider's, which is why this is here rather
    than in the conformance suite: `CompletionRequest.idempotency_token` is
    documented as forwarded "to providers that accept one", and what a
    second provider does with it is that provider's decision to record.
    """
    fake.payload = anthropic_answer()
    await build(ANTHROPIC, fake).complete(request(ANTHROPIC, idempotency_token="dispatch-42"))
    assert fake.requests[0].headers["idempotency-key"] == "dispatch-42"

    fake.requests.clear()
    await build(ANTHROPIC, fake).complete(request(ANTHROPIC))
    assert "idempotency-key" not in fake.requests[0].headers


async def test_tool_definitions_go_out_in_the_documented_shape(fake: FakeProvider) -> None:
    """`input_schema` is the Messages API's name for it.

    What comes *back* from a tool call is vendor-neutral and is asserted in
    the conformance suite; what goes out is this wire format.
    """
    fake.payload = anthropic_answer()
    await build(ANTHROPIC, fake).complete(
        request(
            ANTHROPIC,
            tools=[
                providers.ToolDefinition(
                    name="web_search",
                    description="Search the web.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                )
            ],
        )
    )

    assert fake.requests[0].json["tools"] == [
        {
            "name": "web_search",
            "description": "Search the web.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]


async def test_an_unknown_block_type_is_ignored_rather_than_fatal(
    fake: FakeProvider,
) -> None:
    """A provider adding a block type must not break a deployment that does
    not use it, while the blocks that were understood still answer.

    Content blocks are a Messages API concept, so this cannot be a shared
    test: another provider's equivalent is a different shape.
    """
    fake.payload = anthropic_answer(
        content=[
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Four."},
        ]
    )
    completion = await build(ANTHROPIC, fake).complete(request(ANTHROPIC))
    assert completion.text == "Four."


def test_every_failure_member_has_a_retryability_answer() -> None:
    """A member added without a decision about retrying is a member whose
    behaviour nobody chose.

    T6 added `TIMEOUT` and this is what would have caught it being left out
    of the retryable set - the non-vacuity pass found that removing it from
    that tuple broke no test.
    """
    expected = {
        providers.ProviderFailure.RATE_LIMITED: True,
        providers.ProviderFailure.OVERLOADED: True,
        providers.ProviderFailure.UNAVAILABLE: True,
        providers.ProviderFailure.TIMEOUT: True,
        providers.ProviderFailure.UNAUTHORIZED: False,
        providers.ProviderFailure.INVALID_REQUEST: False,
        providers.ProviderFailure.MALFORMED_RESPONSE: False,
    }
    assert set(expected) == set(providers.ProviderFailure), "a member has no answer here"
    for failure, retryable in expected.items():
        assert failure.is_retryable is retryable, failure.value
