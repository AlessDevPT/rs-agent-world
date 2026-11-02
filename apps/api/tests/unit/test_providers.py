"""The provider surface, against a server that speaks the protocol.

Deterministic and offline, as Q3 requires: no API key, no network, nothing
that behaves differently on a Tuesday. But *not* a mocked transport - the
requests go through the real `GuardedClient` to a real socket, because half
of what is worth testing here is the seam between the two. A mock transport
would happily deliver a request the egress policy would have refused, and
`test_a_provider_cannot_reach_a_blocked_base_url` is the test that matters
most in this file.

The server records what it was sent, so the assertions are about the bytes on
the wire rather than about what this code intended to put there.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from agentworld.infra import net, providers
from agentworld.infra.providers import anthropic, registry

API_KEY = "sk-ant-test-ZZnotarealkeyZZ"


@dataclass
class Recorded:
    """One request as the server received it."""

    method: str
    target: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.body)
        return document


@dataclass
class FakeProvider:
    """A server that answers with whatever the test told it to."""

    port: int = 0
    status: int = 200
    payload: bytes = b"{}"
    extra_headers: dict[str, str] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://provider.test:{self.port}"

    def policy(self) -> net.EgressPolicy:
        return net.EgressPolicy(
            allowed_ports=frozenset({self.port}),
            allowed_networks=frozenset({ipaddress.ip_network("127.0.0.0/8")}),
        )


async def _resolve(_host: str, _port: int) -> Sequence[net.Address]:
    return [ipaddress.ip_address("127.0.0.1")]


@pytest.fixture
async def fake() -> AsyncIterator[FakeProvider]:
    state = FakeProvider()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = await reader.readexactly(length) if length else b""
        state.requests.append(Recorded(method, target, headers, body))

        extra = "".join(f"{k}: {v}\r\n" for k, v in state.extra_headers.items())
        writer.write(
            f"HTTP/1.1 {state.status} X\r\nContent-Type: application/json\r\n{extra}"
            f"Content-Length: {len(state.payload)}\r\nConnection: close\r\n\r\n".encode()
            + state.payload
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state.port = server.sockets[0].getsockname()[1]
    async with server:
        yield state


def _answer(**overrides: Any) -> bytes:
    document: dict[str, Any] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "Four."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    document.update(overrides)
    return json.dumps(document).encode()


def _provider(fake: FakeProvider) -> providers.ModelProvider:
    client = net.GuardedClient(fake.policy(), resolve=_resolve)
    return registry.build(anthropic.SLUG, client, api_key=API_KEY, base_url=fake.base_url)


def _request(**overrides: Any) -> providers.CompletionRequest:
    fields: dict[str, Any] = {
        "model": "claude-opus-5",
        "messages": [providers.Message(role=providers.Role.USER, content="2+2?")],
        "max_output_tokens": 64,
    }
    fields.update(overrides)
    return providers.CompletionRequest(**fields)


# ----------------------------------------------------------- the happy path ---


async def test_a_one_message_completion_returns_content_and_token_counts(
    fake: FakeProvider,
) -> None:
    """The acceptance criterion, minus the live call.

    Token counts are asserted because a completion that silently reports
    costing nothing is worse than one that fails: the first is a spend
    report that is quietly wrong.
    """
    fake.payload = _answer()
    completion = await _provider(fake).complete(_request())

    assert completion.text == "Four."
    assert completion.usage.input_tokens == 12
    assert completion.usage.output_tokens == 3
    assert completion.usage.total_tokens == 15
    assert completion.stop_reason is providers.StopReason.COMPLETED
    assert completion.model == "claude-opus-5"
    assert completion.tool_calls == ()


async def test_the_request_is_the_documented_shape(fake: FakeProvider) -> None:
    fake.payload = _answer()
    await _provider(fake).complete(
        _request(
            system="Be brief.",
            temperature=0.2,
            stop_sequences=["STOP"],
            max_output_tokens=99,
        )
    )

    sent = fake.requests[0]
    assert sent.method == "POST"
    assert sent.target == "/v1/messages"
    assert sent.headers["x-api-key"] == API_KEY
    assert sent.headers["anthropic-version"] == anthropic.API_VERSION
    assert sent.json == {
        "model": "claude-opus-5",
        "max_tokens": 99,
        "messages": [{"role": "user", "content": "2+2?"}],
        "system": "Be brief.",
        "temperature": 0.2,
        "stop_sequences": ["STOP"],
    }


async def test_optional_fields_are_omitted_rather_than_sent_as_null(
    fake: FakeProvider,
) -> None:
    fake.payload = _answer()
    await _provider(fake).complete(_request())
    assert set(fake.requests[0].json) == {"model", "max_tokens", "messages"}


async def test_an_idempotency_token_is_forwarded_when_there_is_one(
    fake: FakeProvider,
) -> None:
    """A retry after an uncertain outcome must not be billed or served twice."""
    fake.payload = _answer()
    await _provider(fake).complete(_request(idempotency_token="dispatch-42"))
    assert fake.requests[0].headers["idempotency-key"] == "dispatch-42"

    fake.requests.clear()
    await _provider(fake).complete(_request())
    assert "idempotency-key" not in fake.requests[0].headers


# ------------------------------------------------------------------- tools ---


async def test_tool_definitions_go_out_and_tool_calls_come_back(
    fake: FakeProvider,
) -> None:
    """The interface carries tools. Nothing here calls one.

    A model answering with a tool call is the model saying what it would
    like done; turning that into an action is the step runner's decision,
    and until Phase 3a adds approvals there is no runner that will.
    """
    fake.payload = _answer(
        content=[
            {"type": "text", "text": "Let me look."},
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "web_search",
                "input": {"query": "weather"},
            },
        ],
        stop_reason="tool_use",
    )
    completion = await _provider(fake).complete(
        _request(
            tools=[
                providers.ToolDefinition(
                    name="web_search",
                    description="Search the web.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                )
            ]
        )
    )

    assert fake.requests[0].json["tools"] == [
        {
            "name": "web_search",
            "description": "Search the web.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]
    assert completion.stop_reason is providers.StopReason.TOOL_USE
    assert completion.text == "Let me look."
    assert [(c.id, c.name, dict(c.arguments)) for c in completion.tool_calls] == [
        ("toolu_01", "web_search", {"query": "weather"})
    ]


async def test_an_unknown_block_type_is_ignored_rather_than_fatal(
    fake: FakeProvider,
) -> None:
    """A provider adding a block type must not break a deployment that does
    not use it, while the blocks that were understood still answer."""
    fake.payload = _answer(
        content=[
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Four."},
        ]
    )
    completion = await _provider(fake).complete(_request())
    assert completion.text == "Four."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", providers.StopReason.COMPLETED),
        ("max_tokens", providers.StopReason.MAX_TOKENS),
        ("tool_use", providers.StopReason.TOOL_USE),
        ("stop_sequence", providers.StopReason.STOPPED),
        ("something_new", providers.StopReason.OTHER),
        (None, providers.StopReason.OTHER),
    ],
)
async def test_every_stop_reason_is_mapped_not_passed_through(
    fake: FakeProvider, raw: str | None, expected: providers.StopReason
) -> None:
    """So a new vendor string cannot reach a caller that switches on this."""
    fake.payload = _answer(stop_reason=raw)
    completion = await _provider(fake).complete(_request())
    assert completion.stop_reason is expected


# -------------------------------------------------------------- classification ---


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


@pytest.mark.parametrize(
    ("status", "failure", "retryable"),
    [
        (400, providers.ProviderFailure.INVALID_REQUEST, False),
        (401, providers.ProviderFailure.UNAUTHORIZED, False),
        (403, providers.ProviderFailure.UNAUTHORIZED, False),
        (404, providers.ProviderFailure.INVALID_REQUEST, False),
        (422, providers.ProviderFailure.INVALID_REQUEST, False),
        (429, providers.ProviderFailure.RATE_LIMITED, True),
        (500, providers.ProviderFailure.UNAVAILABLE, True),
        (503, providers.ProviderFailure.UNAVAILABLE, True),
        (529, providers.ProviderFailure.OVERLOADED, True),
    ],
)
async def test_each_status_is_classified(
    fake: FakeProvider,
    status: int,
    failure: providers.ProviderFailure,
    retryable: bool,
) -> None:
    fake.status = status
    fake.payload = b'{"error": {"message": "nope"}}'
    with pytest.raises(providers.ProviderError) as excinfo:
        await _provider(fake).complete(_request())
    assert excinfo.value.failure is failure
    assert excinfo.value.failure.is_retryable is retryable


async def test_a_rate_limit_honours_retry_after(fake: FakeProvider) -> None:
    """The provider knows when it will serve this key again; we are guessing."""
    fake.status = 429
    fake.extra_headers = {"Retry-After": "42"}
    with pytest.raises(providers.ProviderError) as excinfo:
        await _provider(fake).complete(_request())
    assert excinfo.value.failure is providers.ProviderFailure.RATE_LIMITED
    assert excinfo.value.retry_after_seconds == 42.0


@pytest.mark.parametrize("header", [None, "not-a-number", "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_a_rate_limit_without_a_usable_header_says_so(
    fake: FakeProvider, header: str | None
) -> None:
    """The HTTP-date form is legal and deliberately not parsed.

    Turning a date into a wait means trusting this host's clock against the
    provider's, which is how a skewed machine waits for an hour or not at
    all. `None` means "use your own backoff", which is a safe answer.
    """
    fake.status = 429
    fake.extra_headers = {} if header is None else {"Retry-After": header}
    with pytest.raises(providers.ProviderError) as excinfo:
        await _provider(fake).complete(_request())
    assert excinfo.value.retry_after_seconds is None


async def test_a_redirect_is_not_a_completion(fake: FakeProvider) -> None:
    """The guarded client does not follow it, so it arrives as a non-200."""
    fake.status = 302
    fake.extra_headers = {"Location": "http://169.254.169.254/"}
    with pytest.raises(providers.ProviderError) as excinfo:
        await _provider(fake).complete(_request())
    assert excinfo.value.failure is providers.ProviderFailure.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    ("payload", "because"),
    [
        (b"not json at all", "not JSON"),
        (b"[]", "not an object"),
        (b'{"usage": {"input_tokens": 1, "output_tokens": 1}}', "content is missing"),
        (b'{"content": [{"type":"text","text":"x"}]}', "usage is missing"),
        (
            b'{"content": [{"type":"text","text":"x"}], "usage": {"input_tokens": 1}}',
            "token count",
        ),
        (
            b'{"content": [{"type":"tool_use","id":"t","name":"n"}],'
            b' "usage": {"input_tokens":1,"output_tokens":1}}',
            "input object",
        ),
    ],
)
async def test_a_body_this_cannot_read_is_malformed_and_terminal(
    fake: FakeProvider, payload: bytes, because: str
) -> None:
    """Not retryable: the provider is answering, and asking again is likely
    to produce the same thing this code could not read."""
    fake.payload = payload
    with pytest.raises(providers.ProviderError) as excinfo:
        await _provider(fake).complete(_request())
    assert excinfo.value.failure is providers.ProviderFailure.MALFORMED_RESPONSE
    assert excinfo.value.failure.is_retryable is False
    assert because in excinfo.value.reason


async def test_no_error_carries_the_key_or_the_body(fake: FakeProvider) -> None:
    """A provider's error text can quote the request, and the request
    contains whatever the agent was working on."""
    fake.status = 401
    fake.payload = json.dumps({"error": {"message": f"invalid key {API_KEY}"}}).encode()
    with pytest.raises(providers.ProviderError) as excinfo:
        await _provider(fake).complete(_request())

    rendered = f"{excinfo.value} {excinfo.value!r} {excinfo.value.reason}"
    assert API_KEY not in rendered
    assert "invalid key" not in rendered


# -------------------------------------------------------- through the guard ---


async def test_a_provider_cannot_reach_a_blocked_base_url() -> None:
    """The acceptance criterion, asserted behaviourally rather than by the
    import contract alone.

    A base URL pointing at the metadata address is refused by the egress
    policy, not by anything in the provider - which is only true because the
    provider has no client of its own to reach for. It is handed one.
    """
    client = net.GuardedClient()
    provider = registry.build(
        anthropic.SLUG,
        client,
        api_key=API_KEY,
        base_url="http://169.254.169.254",
    )
    with pytest.raises(providers.ProviderError) as excinfo:
        await provider.complete(_request())

    # Terminal, not retryable: a base URL the egress policy refuses is a
    # configuration mistake, and retrying it only spends attempts.
    assert excinfo.value.failure is providers.ProviderFailure.INVALID_REQUEST
    assert excinfo.value.failure.is_retryable is False
    # And nothing about which range refused it.
    assert "169.254" not in f"{excinfo.value} {excinfo.value.reason}"


async def test_a_provider_that_cannot_connect_is_unavailable() -> None:
    """A network failure is retryable where a blocked destination is not."""
    listener = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    listener.close()
    await listener.wait_closed()

    policy = net.EgressPolicy(
        allowed_ports=frozenset({port}),
        allowed_networks=frozenset({ipaddress.ip_network("127.0.0.0/8")}),
    )
    provider = registry.build(
        anthropic.SLUG,
        net.GuardedClient(policy, resolve=_resolve),
        api_key=API_KEY,
        base_url=f"http://provider.test:{port}",
    )
    with pytest.raises(providers.ProviderError) as excinfo:
        await provider.complete(_request())
    assert excinfo.value.failure is providers.ProviderFailure.UNAVAILABLE
    assert excinfo.value.failure.is_retryable is True


# ---------------------------------------------------------------- registry ---


def test_the_registry_knows_what_this_build_can_serve() -> None:
    assert anthropic.SLUG in registry.available()


def test_an_unknown_slug_is_a_configuration_error_not_a_call_failure() -> None:
    """Nothing was attempted, so there is no call to classify."""
    with pytest.raises(registry.UnknownProviderError, match="made-up"):
        registry.build("made-up", net.GuardedClient(), api_key=API_KEY)


def test_a_built_provider_answers_to_the_slug_it_was_asked_for() -> None:
    provider = registry.build(anthropic.SLUG, net.GuardedClient(), api_key=API_KEY)
    assert provider.slug == anthropic.SLUG


def test_the_protocol_is_satisfied_structurally() -> None:
    """`ModelProvider` is a Protocol, so this is what checks it at runtime."""
    provider: providers.ModelProvider = anthropic.AnthropicProvider(
        net.GuardedClient(), api_key=API_KEY
    )
    assert callable(provider.complete)
