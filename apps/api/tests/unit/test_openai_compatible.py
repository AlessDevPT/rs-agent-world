"""The chat-completions wire shape, and the decisions that are only visible here.

**What is left here is what should name a protocol.** The behaviour every
provider owes a caller lives in `test_provider_conformance.py`, parametrised
over `provider_cases.CASES`, which this provider is now one of. This file
asserts the things that are only meaningful about *this* protocol - the path,
the auth header, the field names, where the system prompt goes - and, more
than that, it pins the five wire decisions that were settled by document
review rather than by taste. Each of those is a place where the obvious
choice is wrong, so each gets a test that fails if someone quietly makes the
obvious choice later:

1. `base_url` carries the full API prefix and only `/chat/completions` is
   appended - the opposite of the sibling adapter, because every server in
   this family documents a base URL that already includes its version.
2. The system prompt is the first message, not a top-level field.
3. The cap is `max_completion_tokens`, and `max_tokens` is never sent.
4. A tool's schema goes in `function.parameters`.
5. Tool call arguments arrive as a JSON string, and a blank one means no
   arguments rather than a parse failure.

The requests still go through the real `GuardedClient` to a real socket; see
`provider_cases` for why that is not a mocked transport.
"""

from __future__ import annotations

from provider_cases import (
    OPENAI,
    FakeProvider,
    build,
    build_at,
    openai_answer,
    request,
    resolve_loopback,
)

from agentworld.infra import net, providers
from agentworld.infra.providers import openai_compatible


async def test_the_request_is_the_documented_shape(fake: FakeProvider) -> None:
    fake.payload = openai_answer()
    await build(OPENAI, fake).complete(
        request(
            OPENAI,
            system="Be brief.",
            temperature=0.2,
            stop_sequences=["STOP"],
            max_output_tokens=99,
        )
    )

    sent = fake.requests[0]
    assert sent.method == "POST"
    assert sent.target == "/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {OPENAI.api_key}"
    assert sent.json == {
        "model": OPENAI.model_id,
        "max_completion_tokens": 99,
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "2+2?"},
        ],
        "temperature": 0.2,
        "stop": ["STOP"],
    }


async def test_the_stored_base_url_carries_the_whole_api_prefix(fake: FakeProvider) -> None:
    """The decision this adapter is least likely to be able to change later.

    Every server in this family documents a base URL that already ends in a
    version prefix, and those prefixes are not all `/v1`: one is `/openai/v1`,
    another `/inference/v1`, another `/api/v1`, and one gateway's is four
    segments. So an operator stores what their provider documents and this
    appends one path - which also means the prefix has to survive untouched,
    and a documented trailing slash must not become a doubled separator.
    """
    fake.payload = openai_answer()
    client = net.GuardedClient(fake.policy(), resolve=resolve_loopback)
    provider = build_at(OPENAI, client, f"http://provider.test:{fake.port}/openai/v1/")
    await provider.complete(request(OPENAI))

    assert fake.requests[0].target == "/openai/v1/chat/completions"


async def test_the_system_prompt_is_the_first_message_not_a_field(fake: FakeProvider) -> None:
    """There is no top-level `system` here, and the role is not a `Role`.

    `Role` has no system member on purpose - a system prompt is not a turn,
    and a vocabulary that could express one mid-conversation would invite it -
    so this adapter synthesizes the message and the caller cannot.
    """
    fake.payload = openai_answer()
    await build(OPENAI, fake).complete(request(OPENAI, system="Be brief."))

    body = fake.requests[0].json
    assert "system" not in body
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]

    fake.requests.clear()
    await build(OPENAI, fake).complete(request(OPENAI))
    assert [m["role"] for m in fake.requests[0].json["messages"]] == ["user"]


async def test_the_cap_is_max_completion_tokens_and_never_max_tokens(
    fake: FakeProvider,
) -> None:
    """`max_tokens` is deprecated and is refused by the current reasoning
    models, so sending it - or sending both - fails exactly where it matters."""
    fake.payload = openai_answer()
    await build(OPENAI, fake).complete(request(OPENAI, max_output_tokens=64))

    body = fake.requests[0].json
    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body


async def test_optional_fields_are_omitted_rather_than_sent_as_null(
    fake: FakeProvider,
) -> None:
    """The field names are this protocol's; that none is null is every
    provider's, and is asserted in the conformance suite as well."""
    fake.payload = openai_answer()
    await build(OPENAI, fake).complete(request(OPENAI))
    assert set(fake.requests[0].json) == {"model", "max_completion_tokens", "messages"}


async def test_nothing_the_caller_did_not_ask_for_is_sent(fake: FakeProvider) -> None:
    """Three fields a convenience wrapper would add, and why none is here.

    Newer models reject an explicit `n` even when it is 1; `stream` would make
    the response a different format entirely; and `user` is ignored by at
    least one server in this family and identifies the caller either way.
    """
    fake.payload = openai_answer()
    await build(OPENAI, fake).complete(request(OPENAI, system="Be brief.", temperature=0.5))

    body = fake.requests[0].json
    for absent in ("n", "stream", "user", "tool_choice", "max_tokens"):
        assert absent not in body, f"{absent} was sent and nobody asked for it"


async def test_an_idempotency_token_is_forwarded_when_there_is_one(
    fake: FakeProvider,
) -> None:
    """Best effort, and known to be so.

    No server in this family documents honouring this header, which is why
    the guarantee a caller actually gets after an uncertain outcome comes from
    the dispatch policy sending a timeout to a person, not from here. It is
    still sent: an unknown header costs nothing, where an unknown body field
    could be refused by a strict server.
    """
    fake.payload = openai_answer()
    await build(OPENAI, fake).complete(request(OPENAI, idempotency_token="dispatch-42"))
    assert fake.requests[0].headers["idempotency-key"] == "dispatch-42"

    fake.requests.clear()
    await build(OPENAI, fake).complete(request(OPENAI))
    assert "idempotency-key" not in fake.requests[0].headers


async def test_tool_definitions_go_out_in_the_documented_shape(fake: FakeProvider) -> None:
    """`function.parameters` is this protocol's name for the schema.

    The sibling provider calls the same thing `input_schema` at the top level
    of the tool. What comes *back* from a tool call is vendor-neutral and is
    asserted in the conformance suite; what goes out is this wire format.
    """
    fake.payload = openai_answer()
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    await build(OPENAI, fake).complete(
        request(
            OPENAI,
            tools=[
                providers.ToolDefinition(
                    name="web_search", description="Search the web.", input_schema=schema
                )
            ],
        )
    )

    assert fake.requests[0].json["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web.",
                "parameters": schema,
            },
        }
    ]


async def test_a_tool_call_with_blank_arguments_is_an_empty_mapping(
    fake: FakeProvider,
) -> None:
    """A function may be defined with an empty parameter list, so a server
    answering one with an empty string is answering correctly - and
    `json.loads("")` raises, so this is a decision rather than a default.

    The alternative was to call it malformed, which would make a no-argument
    tool call fail outright. If that is ever reconsidered, this test is the
    one that has to change, which is the point of it.
    """
    for blank in ("", "   "):
        fake.requests.clear()
        fake.payload = openai_answer(
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_01",
                                "type": "function",
                                "function": {"name": "ping", "arguments": blank},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        )
        completion = await build(OPENAI, fake).complete(request(OPENAI))

        assert [(c.name, dict(c.arguments)) for c in completion.tool_calls] == [("ping", {})]


async def test_a_null_content_becomes_an_empty_string(fake: FakeProvider) -> None:
    """A tool-use turn answers with `content: null`.

    `str(None)` would make that the word "None", which the step output would
    then carry verbatim and `_unusable` would count as the model having said
    something. An empty string is what it actually said.
    """
    fake.payload = openai_answer(
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": None},
                "finish_reason": "stop",
            }
        ]
    )
    completion = await build(OPENAI, fake).complete(request(OPENAI))
    assert completion.text == ""


async def test_the_default_base_url_is_the_documented_api_prefix() -> None:
    """Built without a base URL, this reaches the hosted endpoint.

    Deliberate rather than an oversight - see `build` - and asserted because
    the default carries the version prefix, which is the whole convention in
    one constant. A profile for another server in this family that forgets
    `base_url` therefore sends its key here, which is why `build` logs it.
    """
    assert openai_compatible.DEFAULT_BASE_URL == "https://api.openai.com/v1"

    provider = openai_compatible.build(net.GuardedClient(), api_key="irrelevant")
    assert provider.slug == OPENAI.slug
