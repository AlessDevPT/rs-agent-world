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
from typing import Any

from agentworld.infra import net, providers
from agentworld.infra.providers import anthropic, registry


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


#: Every implementation this build serves. A provider is added here, and the
#: conformance suite then holds it to the same properties as the first.
CASES: tuple[ProviderCase, ...] = (ANTHROPIC,)


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
