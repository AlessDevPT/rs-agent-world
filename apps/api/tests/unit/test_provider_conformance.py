"""One suite every provider passes, parametrised over the implementations.

**This file names no vendor.** Everything vendor-specific is a field on a
`ProviderCase` in `provider_cases.py`; what is asserted here is the
behaviour `base.py` promises a caller, which is the same for every provider
or the abstraction is not one. The wire shape of any single provider - its
endpoint, its header names, its request body - is asserted beside that
provider's adapter instead, because a shared test that knew those could
only know one vendor's.

T13's criterion is this suite: *"Both providers pass one shared conformance
suite, parametrised over the provider builder, with the Anthropic
wire-shape test left beside the adapter it describes."* With one case in
`CASES` it does not yet discharge that criterion - it is the half that can
be built before a second adapter exists, and the second adapter's work is
then a case rather than a file of new tests.

The requests go through the real `GuardedClient` to a real socket; see
`provider_cases` for why that is not a mocked transport.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

import pytest
from provider_cases import (
    CASES,
    FakeProvider,
    ProviderCase,
    build,
    build_at,
    case_id,
    request,
    resolve_loopback,
)

from agentworld.infra import net, providers
from agentworld.infra.providers import registry

#: Flattened per-case tables, so one provider's missing row cannot be read
#: as the suite passing for it.
STATUS_CASES = [
    pytest.param(case, status, failure, retryable, id=f"{case.slug}-{status}")
    for case in CASES
    for status, failure, retryable in case.statuses
]
STOP_REASON_CASES = [
    pytest.param(case, raw, expected, id=f"{case.slug}-{raw}")
    for case in CASES
    for raw, expected in case.stop_reasons
]
MALFORMED_CASES = [
    pytest.param(case, payload, because, id=f"{case.slug}-{because.replace(' ', '-')}")
    for case in CASES
    for payload, because in case.malformed
]


def _nulls_in(document: Any, path: str = "") -> list[str]:
    """Every path in a JSON document whose value is null."""
    match document:
        case None:
            return [path or "<root>"]
        case dict():
            return [p for key, value in document.items() for p in _nulls_in(value, f"{path}.{key}")]
        case list():
            return [
                p
                for index, value in enumerate(document)
                for p in _nulls_in(value, f"{path}[{index}]")
            ]
        case _:
            return []


# ----------------------------------------------------------------- the answer ---


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_a_completion_returns_content_and_token_counts(
    case: ProviderCase, fake: FakeProvider
) -> None:
    """The acceptance criterion, minus the live call.

    Token counts are asserted because a completion that silently reports
    costing nothing is worse than one that fails: the first is a spend
    report that is quietly wrong.
    """
    fake.payload = case.answer()
    completion = await build(case, fake).complete(request(case))

    assert completion.text == case.text
    assert completion.usage.input_tokens == case.input_tokens
    assert completion.usage.output_tokens == case.output_tokens
    assert completion.usage.total_tokens == case.input_tokens + case.output_tokens
    assert completion.stop_reason is providers.StopReason.COMPLETED
    assert completion.model == case.model_id
    assert completion.tool_calls == ()


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_nothing_optional_is_sent_as_null(case: ProviderCase, fake: FakeProvider) -> None:
    """An omitted field is omitted, not sent as an explicit null.

    The vendor-neutral half of the wire-shape test beside each adapter: a
    provider that sends `"temperature": null` is sending a value, and some
    providers reject one where they would have accepted the field's
    absence.
    """
    fake.payload = case.answer()
    await build(case, fake).complete(request(case))

    body = fake.requests[0].json
    assert body, "the request body was empty, so this asserts nothing"
    assert _nulls_in(body) == []


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_tool_calls_come_back_in_the_neutral_vocabulary(
    case: ProviderCase, fake: FakeProvider
) -> None:
    """A tool call arrives as `ToolCall`, with its arguments as a mapping.

    Nothing here calls one. A model answering with a tool call is the model
    saying what it would like done; turning that into an action is the step
    runner's decision, and until Phase 3a adds approvals there is no runner
    that will.

    The arguments are asserted as a `dict` because that is the one place the
    providers' wire formats are known to disagree - one sends an object and
    another a JSON string - and the caller must not be able to tell.
    """
    fake.payload = case.tool_answer()
    completion = await build(case, fake).complete(request(case))

    assert completion.stop_reason is providers.StopReason.TOOL_USE
    assert completion.text == case.tool_text
    assert [(c.id, c.name, dict(c.arguments)) for c in completion.tool_calls] == [
        (call_id, name, arguments) for call_id, name, arguments in case.tool_calls
    ]


@pytest.mark.parametrize(("case", "raw", "expected"), STOP_REASON_CASES)
async def test_every_stop_reason_is_mapped_not_passed_through(
    case: ProviderCase, raw: str | None, expected: providers.StopReason, fake: FakeProvider
) -> None:
    """So a new vendor string cannot reach a caller that switches on this."""
    fake.payload = case.answer_with_stop(raw)
    completion = await build(case, fake).complete(request(case))
    assert completion.stop_reason is expected


# ------------------------------------------------------------- classification ---


@pytest.mark.parametrize(("case", "status", "failure", "retryable"), STATUS_CASES)
async def test_each_status_is_classified(
    case: ProviderCase,
    status: int,
    failure: providers.ProviderFailure,
    retryable: bool,
    fake: FakeProvider,
) -> None:
    fake.status = status
    fake.payload = b'{"error": {"message": "nope"}}'
    with pytest.raises(providers.ProviderError) as excinfo:
        await build(case, fake).complete(request(case))
    assert excinfo.value.failure is failure
    assert excinfo.value.failure.is_retryable is retryable


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_a_rate_limit_honours_retry_after(case: ProviderCase, fake: FakeProvider) -> None:
    """The provider knows when it will serve this key again; we are guessing."""
    fake.status = 429
    fake.extra_headers = {"Retry-After": "42"}
    with pytest.raises(providers.ProviderError) as excinfo:
        await build(case, fake).complete(request(case))
    assert excinfo.value.failure is providers.ProviderFailure.RATE_LIMITED
    assert excinfo.value.retry_after_seconds == 42.0


@pytest.mark.parametrize("header", [None, "not-a-number", "Wed, 21 Oct 2026 07:28:00 GMT"])
@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_a_rate_limit_without_a_usable_header_says_so(
    case: ProviderCase, header: str | None, fake: FakeProvider
) -> None:
    """The HTTP-date form is legal and deliberately not parsed.

    Turning a date into a wait means trusting this host's clock against the
    provider's, which is how a skewed machine waits for an hour or not at
    all. `None` means "use your own backoff", which is a safe answer.
    """
    fake.status = 429
    fake.extra_headers = {} if header is None else {"Retry-After": header}
    with pytest.raises(providers.ProviderError) as excinfo:
        await build(case, fake).complete(request(case))
    assert excinfo.value.retry_after_seconds is None


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_a_redirect_is_not_a_completion(case: ProviderCase, fake: FakeProvider) -> None:
    """The guarded client does not follow it, so it arrives as a non-200."""
    fake.status = 302
    fake.extra_headers = {"Location": "http://169.254.169.254/"}
    with pytest.raises(providers.ProviderError) as excinfo:
        await build(case, fake).complete(request(case))
    assert excinfo.value.failure is providers.ProviderFailure.MALFORMED_RESPONSE


@pytest.mark.parametrize(("case", "payload", "because"), MALFORMED_CASES)
async def test_a_body_this_cannot_read_is_malformed_and_terminal(
    case: ProviderCase, payload: bytes, because: str, fake: FakeProvider
) -> None:
    """Not retryable: the provider is answering, and asking again is likely
    to produce the same thing this code could not read."""
    fake.payload = payload
    with pytest.raises(providers.ProviderError) as excinfo:
        await build(case, fake).complete(request(case))
    assert excinfo.value.failure is providers.ProviderFailure.MALFORMED_RESPONSE
    assert excinfo.value.failure.is_retryable is False
    assert because in excinfo.value.reason


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_no_error_carries_the_key_or_the_body(case: ProviderCase, fake: FakeProvider) -> None:
    """A provider's error text can quote the request, and the request
    contains whatever the agent was working on."""
    fake.status = 401
    fake.payload = f'{{"error": {{"message": "invalid key {case.api_key}"}}}}'.encode()
    with pytest.raises(providers.ProviderError) as excinfo:
        await build(case, fake).complete(request(case))

    rendered = f"{excinfo.value} {excinfo.value!r} {excinfo.value.reason}"
    assert case.api_key not in rendered
    assert "invalid key" not in rendered


# -------------------------------------------------------- through the guard ---


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_a_provider_cannot_reach_a_blocked_base_url(case: ProviderCase) -> None:
    """The acceptance criterion, asserted behaviourally rather than by the
    import contract alone.

    A base URL pointing at the metadata address is refused by the egress
    policy, not by anything in the provider - which is only true because the
    provider has no client of its own to reach for. It is handed one.
    """
    provider = build_at(case, net.GuardedClient(), "http://169.254.169.254")
    with pytest.raises(providers.ProviderError) as excinfo:
        await provider.complete(request(case))

    # Terminal, not retryable: a base URL the egress policy refuses is a
    # configuration mistake, and retrying it only spends attempts.
    assert excinfo.value.failure is providers.ProviderFailure.INVALID_REQUEST
    assert excinfo.value.failure.is_retryable is False
    # And nothing about which range refused it.
    assert "169.254" not in f"{excinfo.value} {excinfo.value.reason}"


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_a_provider_that_cannot_connect_is_unavailable(case: ProviderCase) -> None:
    """A network failure is retryable where a blocked destination is not."""
    listener = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    listener.close()
    await listener.wait_closed()

    policy = net.EgressPolicy(
        allowed_ports=frozenset({port}),
        allowed_networks=frozenset({ipaddress.ip_network("127.0.0.0/8")}),
    )
    provider = build_at(
        case,
        net.GuardedClient(policy, resolve=resolve_loopback),
        f"http://provider.test:{port}",
    )
    with pytest.raises(providers.ProviderError) as excinfo:
        await provider.complete(request(case))
    assert excinfo.value.failure is providers.ProviderFailure.UNAVAILABLE
    assert excinfo.value.failure.is_retryable is True


# ---------------------------------------------------------------- registry ---


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_every_case_is_a_slug_this_build_can_serve(case: ProviderCase) -> None:
    """`available()` is what `/v1/meta` publishes and what a model profile
    may name, so a case outside it would be testing an unreachable adapter."""
    assert case.slug in registry.available()


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_a_built_provider_answers_to_the_slug_it_was_asked_for(case: ProviderCase) -> None:
    provider = registry.build(case.slug, net.GuardedClient(), api_key=case.api_key)
    assert provider.slug == case.slug


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_the_protocol_is_satisfied_structurally(case: ProviderCase) -> None:
    """`ModelProvider` is a Protocol, so this is what checks it at runtime."""
    provider: providers.ModelProvider = registry.build(
        case.slug, net.GuardedClient(), api_key=case.api_key
    )
    assert callable(provider.complete)


def test_an_unknown_slug_is_a_configuration_error_not_a_call_failure() -> None:
    """Nothing was attempted, so there is no call to classify.

    Not parametrised: it is about the registry rather than about any
    implementation in it.
    """
    with pytest.raises(registry.UnknownProviderError, match="made-up"):
        registry.build("made-up", net.GuardedClient(), api_key="irrelevant")
