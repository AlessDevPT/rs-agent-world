"""The provider harness, and one case per implementation.

Extracted from `test_providers.py` with no behaviour change, so that a
second provider becomes a `ProviderCase` in `CASES` rather than a second
copy of the same twenty tests. `test_provider_conformance.py` imports
`CASES` and names no vendor; what is left in `test_providers.py` is the
Anthropic wire shape, which is supposed to name one.

**A case carries the three tables that genuinely differ between
providers** - the statuses it answers with, the stop-reason strings it
sends, and the bodies this code cannot read. The *property* under test is
shared and lives in the conformance suite; only the strings are per-vendor.
A provider added with a table missing a row is a provider that has not been
held to that property, which is why the tables are fields on the case
rather than constants in the suite.

**Not a mock transport.** `FakeProvider` is a real socket reached through
the real `GuardedClient`, because half of what is worth testing here is the
seam between an adapter and the egress policy: a mock transport would
happily deliver a request the policy would have refused, and
`test_a_provider_cannot_reach_a_blocked_base_url` is the test that matters
most in the suite.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from agentworld.infra import net, providers
from agentworld.infra.providers import anthropic, openai_compatible, registry


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
    """A server that answers with whatever the test told it to.

    `handle` is a method rather than a closure over the fixture so that the
    recording and the canned answer live on the same object the test
    configures.
    """

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

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
        self.requests.append(Recorded(method, target, headers, body))

        extra = "".join(f"{k}: {v}\r\n" for k, v in self.extra_headers.items())
        writer.write(
            f"HTTP/1.1 {self.status} X\r\nContent-Type: application/json\r\n{extra}"
            f"Content-Length: {len(self.payload)}\r\nConnection: close\r\n\r\n".encode()
            + self.payload
        )
        await writer.drain()
        writer.close()


async def resolve_loopback(_host: str, _port: int) -> Sequence[net.Address]:
    """Resolution the policy can allow, so `provider.test` is a local socket."""
    return [ipaddress.ip_address("127.0.0.1")]


@dataclass(frozen=True)
class ProviderCase:
    """One implementation, and the vendor strings the shared tests need."""

    slug: str
    model_id: str
    api_key: str

    #: A 200 this adapter understands, and what it must yield from one.
    answer: Callable[[], bytes]
    text: str
    input_tokens: int
    output_tokens: int

    #: A 200 carrying one text part and one tool call.
    tool_answer: Callable[[], bytes]
    tool_text: str
    tool_calls: tuple[tuple[str, str, dict[str, Any]], ...]

    #: A 200 whose stop reason is the given wire string, and the mapping
    #: every one of those strings must produce. `None` stands for a response
    #: with no stop reason at all.
    answer_with_stop: Callable[[str | None], bytes]
    stop_reasons: tuple[tuple[str | None, providers.StopReason], ...]

    #: status -> failure, and whether that failure is retryable.
    statuses: tuple[tuple[int, providers.ProviderFailure, bool], ...]

    #: A 200 body this adapter cannot read, and a fragment its reason must
    #: name. The fragment is asserted so that a parse failure says which
    #: field was wrong rather than only that something was.
    malformed: tuple[tuple[bytes, str], ...]


# ------------------------------------------------------------------ anthropic ---

ANTHROPIC_KEY = "sk-ant-test-ZZnotarealkeyZZ"
ANTHROPIC_MODEL = "claude-opus-5"


def anthropic_answer(**overrides: Any) -> bytes:
    document: dict[str, Any] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": ANTHROPIC_MODEL,
        "content": [{"type": "text", "text": "Four."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    document.update(overrides)
    return json.dumps(document).encode()


def _anthropic_with_stop(raw: str | None) -> bytes:
    return anthropic_answer(stop_reason=raw)


def _anthropic_tool_answer() -> bytes:
    return anthropic_answer(
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


ANTHROPIC = ProviderCase(
    slug=anthropic.SLUG,
    model_id=ANTHROPIC_MODEL,
    api_key=ANTHROPIC_KEY,
    answer=anthropic_answer,
    text="Four.",
    input_tokens=12,
    output_tokens=3,
    tool_answer=_anthropic_tool_answer,
    tool_text="Let me look.",
    tool_calls=(("toolu_01", "web_search", {"query": "weather"}),),
    answer_with_stop=_anthropic_with_stop,
    stop_reasons=(
        ("end_turn", providers.StopReason.COMPLETED),
        ("max_tokens", providers.StopReason.MAX_TOKENS),
        ("tool_use", providers.StopReason.TOOL_USE),
        ("stop_sequence", providers.StopReason.STOPPED),
        ("something_new", providers.StopReason.OTHER),
        (None, providers.StopReason.OTHER),
    ),
    statuses=(
        (400, providers.ProviderFailure.INVALID_REQUEST, False),
        (401, providers.ProviderFailure.UNAUTHORIZED, False),
        (403, providers.ProviderFailure.UNAUTHORIZED, False),
        (404, providers.ProviderFailure.INVALID_REQUEST, False),
        (422, providers.ProviderFailure.INVALID_REQUEST, False),
        (429, providers.ProviderFailure.RATE_LIMITED, True),
        (500, providers.ProviderFailure.UNAVAILABLE, True),
        (503, providers.ProviderFailure.UNAVAILABLE, True),
        (529, providers.ProviderFailure.OVERLOADED, True),
    ),
    malformed=(
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
    ),
)


# ------------------------------------------------------------------- openai ---

OPENAI_KEY = "sk-test-ZZnotarealkeyZZ"
OPENAI_MODEL = "gpt-5-mini"

#: Deliberately different from the other case's counts. If a shared test
#: passed because it was reading the wrong case's expectations, equal numbers
#: would hide it.
OPENAI_INPUT_TOKENS = 21
OPENAI_OUTPUT_TOKENS = 5


def openai_answer(**overrides: Any) -> bytes:
    document: dict[str, Any] = {
        "id": "chatcmpl-01",
        "object": "chat.completion",
        "created": 1764200000,
        "model": OPENAI_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Four."},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": OPENAI_INPUT_TOKENS,
            "completion_tokens": OPENAI_OUTPUT_TOKENS,
            "total_tokens": OPENAI_INPUT_TOKENS + OPENAI_OUTPUT_TOKENS,
        },
    }
    document.update(overrides)
    return json.dumps(document).encode()


def openai_choice(message: dict[str, Any], finish_reason: str | None) -> list[dict[str, Any]]:
    """One choice, with `finish_reason` omitted entirely when it is None.

    Omitted rather than null, because "the field is absent" is the case the
    mapping has to answer for - a server that stops reporting it must not
    produce a stop reason this vocabulary does not have.
    """
    choice: dict[str, Any] = {"index": 0, "message": message}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return [choice]


def _openai_with_stop(raw: str | None) -> bytes:
    return openai_answer(
        choices=openai_choice({"role": "assistant", "content": "Four."}, raw),
    )


def _openai_tool_answer() -> bytes:
    return openai_answer(
        choices=openai_choice(
            {
                "role": "assistant",
                "content": "Let me look.",
                "tool_calls": [
                    {
                        "id": "call_01",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            # A JSON *string*, which is the one place the two
                            # wire formats disagree where a caller could see
                            # it. The conformance suite asserts they cannot.
                            "arguments": '{"query": "weather"}',
                        },
                    }
                ],
            },
            "tool_calls",
        ),
    )


def _openai_body(choices: Any = None, usage: Any = None) -> bytes:
    """A malformed-case body, assembled so each row isolates one fault."""
    document: dict[str, Any] = {}
    if choices is not None:
        document["choices"] = choices
    if usage is not None:
        document["usage"] = usage
    return json.dumps(document).encode()


_OPENAI_GOOD_USAGE: Final = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
_OPENAI_TEXT_MESSAGE: Final = {"role": "assistant", "content": "x"}


def _openai_tool_body(arguments: Any) -> bytes:
    """A tool call whose `arguments` is whatever the row is testing."""
    return _openai_body(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "n", "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        usage=_OPENAI_GOOD_USAGE,
    )


OPENAI = ProviderCase(
    slug=openai_compatible.SLUG,
    model_id=OPENAI_MODEL,
    api_key=OPENAI_KEY,
    answer=openai_answer,
    text="Four.",
    input_tokens=OPENAI_INPUT_TOKENS,
    output_tokens=OPENAI_OUTPUT_TOKENS,
    tool_answer=_openai_tool_answer,
    tool_text="Let me look.",
    tool_calls=(("call_01", "web_search", {"query": "weather"}),),
    answer_with_stop=_openai_with_stop,
    #: No row maps to `STOPPED`: this wire has one `stop` for both a natural
    #: ending and a stop sequence, so that member is unreachable here. The
    #: gap is deliberate and visible, which is why these tables are per-case.
    stop_reasons=(
        ("stop", providers.StopReason.COMPLETED),
        ("length", providers.StopReason.MAX_TOKENS),
        ("tool_calls", providers.StopReason.TOOL_USE),
        # A refusal, which the vocabulary has no member for.
        ("content_filter", providers.StopReason.OTHER),
        # Deprecated on the wire, and this adapter never reads the matching
        # response field, so promising a tool call would be a lie.
        ("function_call", providers.StopReason.OTHER),
        ("something_new", providers.StopReason.OTHER),
        (None, providers.StopReason.OTHER),
    ),
    #: No 529 - that is the other vendor's overload status. 503 is this
    #: protocol's, and it is covered by the 5xx range rather than by a row of
    #: its own in the adapter.
    statuses=(
        (400, providers.ProviderFailure.INVALID_REQUEST, False),
        (401, providers.ProviderFailure.UNAUTHORIZED, False),
        (403, providers.ProviderFailure.UNAUTHORIZED, False),
        (404, providers.ProviderFailure.INVALID_REQUEST, False),
        (413, providers.ProviderFailure.INVALID_REQUEST, False),
        (422, providers.ProviderFailure.INVALID_REQUEST, False),
        (429, providers.ProviderFailure.RATE_LIMITED, True),
        (500, providers.ProviderFailure.UNAVAILABLE, True),
        (503, providers.ProviderFailure.UNAVAILABLE, True),
    ),
    malformed=(
        (b"not json at all", "not JSON"),
        (b"[]", "not an object"),
        (_openai_body(usage=_OPENAI_GOOD_USAGE), "choices is missing"),
        (_openai_body(choices=[], usage=_OPENAI_GOOD_USAGE), "choices is empty"),
        (_openai_body(choices=["nope"], usage=_OPENAI_GOOD_USAGE), "a choice is not"),
        (_openai_body(choices=[{"index": 0}], usage=_OPENAI_GOOD_USAGE), "no message object"),
        (_openai_body(choices=[{"message": _OPENAI_TEXT_MESSAGE}]), "usage is missing"),
        (
            _openai_body(choices=[{"message": _OPENAI_TEXT_MESSAGE}], usage={"prompt_tokens": 1}),
            "token count",
        ),
        # Content parts, which this protocol allows in a *request* and not in
        # a response. Coercing them with `str()` would record a Python repr
        # as the model's answer, so they are refused instead.
        (
            _openai_body(
                choices=[{"message": {"role": "assistant", "content": [{"text": "x"}]}}],
                usage=_OPENAI_GOOD_USAGE,
            ),
            "content is not a string",
        ),
        (_openai_tool_body("{not json"), "arguments are not JSON"),
        (_openai_tool_body("[1, 2]"), "not a JSON object"),
        (_openai_tool_body(3), "are not a string"),
        (
            _openai_body(
                choices=[{"message": {"role": "assistant", "tool_calls": "no"}}],
                usage=_OPENAI_GOOD_USAGE,
            ),
            "tool_calls is not a list",
        ),
    ),
)


#: Every implementation this build serves. A provider is added here, and the
#: conformance suite then holds it to the same properties as the first.
CASES: tuple[ProviderCase, ...] = (ANTHROPIC, OPENAI)


def case_id(case: ProviderCase) -> str:
    return case.slug


# -------------------------------------------------------------------- builders ---


def build(case: ProviderCase, fake: FakeProvider) -> providers.ModelProvider:
    """The provider under test, over a client pinned to the fake's socket."""
    client = net.GuardedClient(fake.policy(), resolve=resolve_loopback)
    return registry.build(case.slug, client, api_key=case.api_key, base_url=fake.base_url)


def build_at(
    case: ProviderCase, client: net.GuardedClient, base_url: str
) -> providers.ModelProvider:
    """The provider under test over a client the caller chose.

    Used by the two tests that are about the client rather than the adapter:
    a base URL the egress policy refuses, and a port nothing is listening on.
    """
    return registry.build(case.slug, client, api_key=case.api_key, base_url=base_url)


def request(case: ProviderCase, **overrides: Any) -> providers.CompletionRequest:
    fields: dict[str, Any] = {
        "model": case.model_id,
        "messages": [providers.Message(role=providers.Role.USER, content="2+2?")],
        "max_output_tokens": 64,
    }
    fields.update(overrides)
    return providers.CompletionRequest(**fields)
