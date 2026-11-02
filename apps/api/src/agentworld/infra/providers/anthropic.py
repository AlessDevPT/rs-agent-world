"""The Messages API, over the guarded client.

**No vendor SDK.** Every provider call has to go through `infra/net`, and an
SDK brings its own HTTP client with its own connection handling, its own
redirect policy and its own resolution - none of which is the pinned,
address-validated one this system requires. Using the SDK and then trying to
make it borrow our transport means depending on an internal of theirs that
can change in a patch release. The request this makes is one POST with a JSON
body; writing it directly costs about eighty lines and removes an entire
class of "which client actually made that call" questions.

The cost is honest and worth stating: new API features arrive when someone
adds them here, rather than with a dependency bump. For a surface this narrow
- one endpoint, one response shape - that is a trade worth making. It would
not be for a provider whose API was large.

Nothing in this file is reachable from a type a caller holds: callers get
`Completion` and `ProviderError` from `base.py`, which name no vendor.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from agentworld.infra.logging import get_logger
from agentworld.infra.net import GuardedClient, OutboundError, OutboundFailure
from agentworld.infra.providers.base import (
    Completion,
    CompletionRequest,
    ProviderError,
    ProviderFailure,
    StopReason,
    ToolCall,
    ToolDefinition,
    Usage,
)

SLUG: Final = "anthropic"
DEFAULT_BASE_URL: Final = "https://api.anthropic.com"

#: The dated API version header. Pinned rather than tracking "latest",
#: because a response shape changing underneath a running deployment is the
#: failure this header exists to prevent.
API_VERSION: Final = "2023-06-01"

_STOP_REASONS: Final[Mapping[str, StopReason]] = {
    "end_turn": StopReason.COMPLETED,
    "max_tokens": StopReason.MAX_TOKENS,
    "tool_use": StopReason.TOOL_USE,
    "stop_sequence": StopReason.STOPPED,
}

#: Status to failure. Anything not here falls to the ranges in `_failure_for`.
_FAILURE_BY_STATUS: Final[Mapping[int, ProviderFailure]] = {
    400: ProviderFailure.INVALID_REQUEST,
    401: ProviderFailure.UNAUTHORIZED,
    403: ProviderFailure.UNAUTHORIZED,
    404: ProviderFailure.INVALID_REQUEST,
    413: ProviderFailure.INVALID_REQUEST,
    422: ProviderFailure.INVALID_REQUEST,
    429: ProviderFailure.RATE_LIMITED,
    529: ProviderFailure.OVERLOADED,
}

log = get_logger(__name__)


class AnthropicProvider:
    """`ModelProvider` over the Messages API.

    Holds no credential of its own: `api_key` is passed in by the caller,
    which got it from `CredentialService.use` immediately before this call
    and holds it for no longer than this call takes.
    """

    def __init__(
        self,
        client: GuardedClient,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @property
    def slug(self) -> str:
        return SLUG

    async def complete(self, request: CompletionRequest) -> Completion:
        body = _request_body(request)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        }
        if request.idempotency_token is not None:
            headers["idempotency-key"] = request.idempotency_token

        try:
            response = await self._client.request(
                "POST",
                f"{self._base_url}/v1/messages",
                headers=headers,
                content=json.dumps(body).encode(),
            )
        except OutboundError as exc:
            # The network layer has already normalised this and already
            # refused to say anything specific about the destination.
            raise ProviderError(
                _failure_for_outbound(exc.failure), reason=exc.failure.value
            ) from None

        if response.status_code != 200:
            raise _error_for(response.status_code, response.headers)
        return _completion(response.content)


def _request_body(request: CompletionRequest) -> dict[str, Any]:
    """The wire shape. Optional fields are omitted rather than sent as null."""
    body: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_output_tokens,
        "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
    }
    if request.system is not None:
        body["system"] = request.system
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.stop_sequences:
        body["stop_sequences"] = list(request.stop_sequences)
    if request.tools:
        body["tools"] = [_tool(tool) for tool in request.tools]
    return body


def _tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
    }


def _completion(payload: bytes) -> Completion:
    """Parse a 200 into the neutral vocabulary.

    Every failure to understand the body is `MALFORMED_RESPONSE` and not
    retryable: the provider is answering, and asking again is likely to
    produce the same thing this code could not read. The reason names the
    field, never the content - a parse error that quoted the body would put
    the model's output into a log line that nobody expected to contain one.
    """
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        raise _malformed("the body is not JSON") from None
    if not isinstance(document, dict):
        raise _malformed("the body is not an object")

    blocks = document.get("content")
    if not isinstance(blocks, list):
        raise _malformed("content is missing or not a list")

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise _malformed("a content block is not an object")
        match block.get("type"):
            case "text":
                text_parts.append(str(block.get("text", "")))
            case "tool_use":
                tool_calls.append(_tool_call(block))
            case _:
                # A block type this version does not know. Dropped rather
                # than raised on: a provider adding a block type should not
                # break a deployment that does not use it, and the text and
                # tool calls that were understood are still a usable answer.
                log.info("provider_block_type_ignored", block_type=str(block.get("type")))

    return Completion(
        text="".join(text_parts),
        usage=_usage(document.get("usage")),
        stop_reason=_STOP_REASONS.get(str(document.get("stop_reason")), StopReason.OTHER),
        model=str(document.get("model", "")),
        tool_calls=tuple(tool_calls),
    )


def _tool_call(block: Mapping[str, Any]) -> ToolCall:
    arguments = block.get("input")
    if not isinstance(arguments, dict):
        raise _malformed("a tool_use block has no input object")
    return ToolCall(
        id=str(block.get("id", "")),
        name=str(block.get("name", "")),
        arguments=arguments,
    )


def _usage(raw: object) -> Usage:
    """Token counts, which a caller records as metrics and bills against.

    Missing counts are an error rather than a zero. A completion that
    silently reports costing nothing is worse than one that fails: the first
    is a spend report that is quietly wrong.
    """
    if not isinstance(raw, dict):
        raise _malformed("usage is missing")
    try:
        return Usage(
            input_tokens=int(raw["input_tokens"]),
            output_tokens=int(raw["output_tokens"]),
        )
    except (KeyError, TypeError, ValueError):
        raise _malformed("usage is missing a token count") from None


def _error_for(status: int, headers: Mapping[str, str]) -> ProviderError:
    """Classify a non-200. The body is not read."""
    failure = _failure_for(status)
    retry_after = _retry_after(headers) if failure is ProviderFailure.RATE_LIMITED else None
    return ProviderError(failure, reason=f"HTTP {status}", retry_after_seconds=retry_after)


def _failure_for(status: int) -> ProviderFailure:
    if (known := _FAILURE_BY_STATUS.get(status)) is not None:
        return known
    if 500 <= status < 600:
        return ProviderFailure.UNAVAILABLE
    # A 2xx that is not 200, or a 3xx the guarded client did not follow.
    # Neither is a usable completion and neither will become one.
    return ProviderFailure.MALFORMED_RESPONSE


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """`Retry-After` in seconds, when it is a number we can use.

    Only the delta-seconds form. The HTTP-date form is legal and this
    provider does not send it; parsing a date into a wait would mean
    trusting the caller's clock against the provider's, which is how a
    skewed host ends up waiting for an hour or not at all.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _failure_for_outbound(failure: OutboundFailure) -> ProviderFailure:
    """A network failure, in provider terms.

    `BLOCKED_DESTINATION` becomes `INVALID_REQUEST` rather than
    `UNAVAILABLE`: a provider base URL that the egress policy refuses is a
    configuration mistake, and retrying it only spends attempts.
    """
    match failure:
        case OutboundFailure.BLOCKED_DESTINATION:
            return ProviderFailure.INVALID_REQUEST
        case OutboundFailure.TLS_FAILURE:
            return ProviderFailure.INVALID_REQUEST
        case _:
            return ProviderFailure.UNAVAILABLE


def _malformed(what: str) -> ProviderError:
    return ProviderError(ProviderFailure.MALFORMED_RESPONSE, reason=what)


def build(client: GuardedClient, *, api_key: str, base_url: str | None = None) -> AnthropicProvider:
    """The registry's entry point. See `registry.py`."""
    return AnthropicProvider(client, api_key=api_key, base_url=base_url or DEFAULT_BASE_URL)
