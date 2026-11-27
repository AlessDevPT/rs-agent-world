"""Chat completions, over the guarded client.

**The slug `openai` names a protocol, not a company.** One adapter serves the
hosted API and every server that speaks the same request and response shape;
which server a call reaches is decided by `base_url` on the model profile, not
by the slug. That is the whole reason this file is named for compatibility
rather than for a vendor: a second slug would only be worth adding if some
server needed a genuinely different request, and then it would be a second
file, which is the property `registry.py` exists to keep.

The honest limit of that: `_compatible` in `services/definitions.py` only
checks that a credential's slug equals the profile's, so a hosted key and a
self-hosted server's key are indistinguishable to it. What stops a key going
to the wrong place is the operator setting `base_url` correctly, and nothing
else.

**No vendor SDK**, for the reasons `anthropic.py` gives at length: an SDK
brings its own HTTP client, and every outbound call here has to go through
the pinned, address-validated one in `infra/net`. One POST with a JSON body
does not need a dependency.

## The wire decisions, and why

Settled against the published OpenAPI document (`info.version: 2.3.0`) rather
than from memory, because most of them are places where the obvious choice is
wrong.

**`base_url` holds the full API prefix** and this appends `/chat/completions`.
That is the opposite of the sibling adapter, which stores an origin and
appends a versioned path - and it is deliberate. Every compatible server
documents a `base_url` that already carries its version prefix, and those
prefixes are not uniformly `/v1`: `/openai/v1`, `/inference/v1`, `/api/v1`,
and one gateway whose prefix is four segments and does not end in a version
at all. The published document splits it the same way, with the version in
`servers[0].url` and `/chat/completions` as the path. So an operator stores
the string their provider documents, unedited, and the two adapters disagree
about what a base URL means because their two ecosystems do.

**The system prompt is a message, not a field.** It goes in first with role
`system`. The newer spelling is `developer`, which the document says replaces
`system` for its reasoning models - but no compatible server documents
accepting it, and `system` is the older and more widely implemented of the
two. Neither works on every hosted model, so if one turns out to be needed
per profile, the shape is a `system_role` parameter beside `base_url`, not a
list of model names in here.

**`max_completion_tokens`, never `max_tokens`.** The latter is deprecated and
is refused outright by the current reasoning models. The cost of this choice
is real and worth naming: a lenient server that does not know the new field
ignores the cap rather than refusing it, so generation runs until the egress
timeout - which `StepRunner` reads as an uncertain outcome and sends to a
person. A server that refuses the field is the better failure of the two.

**Tool definitions carry a `parameters` schema**, not an `input_schema`, and
the JSON Schema is passed through unexamined - including a name this layer
does not validate, which the server will reject if it is malformed.

**Tool call arguments arrive as a JSON string**, which is the one place the
two providers' wire formats disagree where a caller can see it, so this
parses it into the mapping `ToolCall` promises. An empty or blank string
becomes an empty mapping: a tool with no parameters called with no arguments
is a real answer, and the document says a function may be defined with an
empty parameter list. Anything else that will not parse into an object is
`MALFORMED_RESPONSE`.

That last classification is deliberate and slightly wrong, which is better
said than hidden. The document itself warns that a model does not always
generate valid JSON here, so this is the one malformed case where trying
again might genuinely succeed - but `ProviderFailure.MALFORMED_RESPONSE` is
not retryable as a property of the vocabulary, not of this file, and
reporting it as something retryable would misname it.

**Four helpers here duplicate `anthropic.py`**: the `Retry-After` parse, the
outbound-failure mapping, the non-200 classifier and the malformed-response
constructor. They are identical today and the duplication is deliberate for
now, so that adding a provider did not require editing a shipped one. If a
third provider arrives, or either copy changes, they should move to a shared
module in this package - two copies of a retry rule is exactly the drift this
codebase avoids elsewhere.
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

SLUG: Final = "openai"

#: The published document's own `servers[0].url`, version prefix included.
#: Overridden per profile for every other server that speaks this protocol.
DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"

#: The role a system prompt is sent under. Not a `Role` member: `Role` has
#: none on purpose, because a system prompt is not a turn in a conversation
#: and must not be placeable in the middle of one.
SYSTEM_ROLE: Final = "system"

#: Wire string -> vocabulary. `content_filter` and the deprecated
#: `function_call` are **absent on purpose**, not forgotten: both fall to
#: `OTHER`. A content filter is a refusal, which this vocabulary has no member
#: for - adding one would be a change for every provider, so it is not made
#: here - and `function_call` would promise tool calls that this file does not
#: read, since it reads only `tool_calls`.
#:
#: `STOPPED` is unreachable for this provider: the wire has one `stop` for
#: both a natural ending and a stop sequence, and guessing which it was is
#: worse than saying `COMPLETED`. Nothing in the system switches on `STOPPED`.
_FINISH_REASONS: Final[Mapping[str, StopReason]] = {
    "stop": StopReason.COMPLETED,
    "length": StopReason.MAX_TOKENS,
    "tool_calls": StopReason.TOOL_USE,
}

#: Status to failure. Anything not here falls to the ranges in `_failure_for`.
#: No 529 - that is the sibling provider's overload status, and this protocol
#: sheds load with 503, which the 5xx range already covers.
_FAILURE_BY_STATUS: Final[Mapping[int, ProviderFailure]] = {
    400: ProviderFailure.INVALID_REQUEST,
    401: ProviderFailure.UNAUTHORIZED,
    403: ProviderFailure.UNAUTHORIZED,
    404: ProviderFailure.INVALID_REQUEST,
    413: ProviderFailure.INVALID_REQUEST,
    422: ProviderFailure.INVALID_REQUEST,
    429: ProviderFailure.RATE_LIMITED,
}

log = get_logger(__name__)


class OpenAICompatibleProvider:
    """`ModelProvider` over the chat completions protocol.

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
        #: Trailing slash removed because one documented base URL ends in
        #: one, and `//chat/completions` is a different path.
        self._base_url = base_url.rstrip("/")

    @property
    def slug(self) -> str:
        return SLUG

    async def complete(self, request: CompletionRequest) -> Completion:
        body = _request_body(request)
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "accept": "application/json",
        }
        if request.idempotency_token is not None:
            # Best effort, and said plainly: no server in this family
            # documents honouring this header. What actually protects a retry
            # after an uncertain outcome is `StepRunner` sending a timeout to
            # a person instead of trying again.
            headers["idempotency-key"] = request.idempotency_token

        try:
            response = await self._client.request(
                "POST",
                f"{self._base_url}/chat/completions",
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
    """The wire shape. Optional fields are omitted rather than sent as null.

    Nothing is sent that was not asked for. `n`, `stream` and `user` are
    absent deliberately: the first is rejected by newer models even as `n=1`,
    the second would make this a different parse entirely, and the third is
    ignored by at least one server and is caller-identifying either way.
    """
    body: dict[str, Any] = {
        "model": request.model,
        "max_completion_tokens": request.max_output_tokens,
        "messages": _messages(request),
    }
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.stop_sequences:
        # Passed through unexamined. The document allows at most four, and a
        # fifth is a refusal from the server rather than a silent truncation
        # here - the caller asked for five and should be told, not guessed at.
        body["stop"] = list(request.stop_sequences)
    if request.tools:
        body["tools"] = [_tool(tool) for tool in request.tools]
    return body


def _messages(request: CompletionRequest) -> list[dict[str, str]]:
    """The system prompt first, when there is one, then the turns."""
    messages: list[dict[str, str]] = []
    if request.system is not None:
        messages.append({"role": SYSTEM_ROLE, "content": request.system})
    messages.extend({"role": m.role.value, "content": m.content} for m in request.messages)
    return messages


def _tool(tool: ToolDefinition) -> dict[str, Any]:
    """`parameters` is this protocol's name for the schema."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        },
    }


def _completion(payload: bytes) -> Completion:
    """Parse a 200 into the neutral vocabulary.

    Every failure to understand the body is `MALFORMED_RESPONSE` and not
    retryable: the provider is answering, and asking again is likely to
    produce the same thing this code could not read. The reason names the
    field, never the content - a parse error that quoted the body would put
    the model's output into a log line that nobody expected to contain one.
    """
    document = _document(payload)
    choice = _first_choice(document)
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _malformed("the first choice has no message object")

    return Completion(
        text=_text(message.get("content")),
        usage=_usage(document.get("usage")),
        stop_reason=_FINISH_REASONS.get(str(choice.get("finish_reason")), StopReason.OTHER),
        model=str(document.get("model", "")),
        tool_calls=_tool_calls(message.get("tool_calls")),
    )


def _document(payload: bytes) -> Mapping[str, Any]:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        raise _malformed("the body is not JSON") from None
    if not isinstance(document, dict):
        raise _malformed("the body is not an object")
    return document


def _first_choice(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """The one choice this layer asks for, since it never sends `n`."""
    choices = document.get("choices")
    if not isinstance(choices, list):
        raise _malformed("choices is missing or not a list")
    if not choices:
        # A 200 with no choices is not an answer, and the document does not
        # permit it. Caught here so it is a classified failure rather than an
        # IndexError the dispatch loop would read as a programming error.
        raise _malformed("choices is empty")
    first = choices[0]
    if not isinstance(first, dict):
        raise _malformed("a choice is not an object")
    return first


def _text(raw: object) -> str:
    """`content` is a string or null on the wire, and null means no text.

    A tool-use turn answers with `content: null`, which must become the empty
    string rather than the word "None" - the step output carries this
    verbatim, and `_unusable` in the dispatch service reads it to decide
    whether the model said anything at all.

    Anything else is malformed rather than coerced. `str()` on a list of
    content parts would put a Python repr - `[{'type': 'text', ...}]` - into
    a step's recorded output and call it the model's answer, which is the one
    way this adapter could be quietly wrong instead of loudly broken. The
    response schema says string or null, so a server sending parts here is
    not one this code can read.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise _malformed("content is not a string")
    return raw


def _tool_calls(raw: object) -> tuple[ToolCall, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _malformed("tool_calls is not a list")
    return tuple(_tool_call(call) for call in raw)


def _tool_call(raw: object) -> ToolCall:
    if not isinstance(raw, dict):
        raise _malformed("a tool call is not an object")
    function = raw.get("function")
    if not isinstance(function, dict):
        raise _malformed("a tool call has no function object")
    return ToolCall(
        id=str(raw.get("id", "")),
        name=str(function.get("name", "")),
        arguments=_arguments(function.get("arguments")),
    )


def _arguments(raw: object) -> Mapping[str, Any]:
    """The arguments string, as the mapping `ToolCall` promises.

    Blank means no arguments rather than a parse failure: a function may be
    defined with an empty parameter list, and a server answering one with an
    empty string is answering correctly. Anything else that does not parse
    into an object is malformed - see the module docstring for why that is
    terminal even though the model, not the protocol, is what got it wrong.
    """
    if not isinstance(raw, str):
        raise _malformed("a tool call's arguments are not a string")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise _malformed("a tool call's arguments are not JSON") from None
    if not isinstance(parsed, dict):
        raise _malformed("a tool call's arguments are not a JSON object")
    arguments: dict[str, Any] = parsed
    return arguments


def _usage(raw: object) -> Usage:
    """Token counts, which a caller records as metrics and bills against.

    Missing counts are an error rather than a zero, for the reason the
    sibling adapter gives: a completion that silently reports costing nothing
    is worse than one that fails, because the first is a spend report that is
    quietly wrong. The document declares these three required but gives them
    a default of zero, and `usage` itself is **not** in the response's
    required list - so a server may legally answer without it, and a server
    that does cannot be used here. That is the trade, taken deliberately.

    `total_tokens` is ignored rather than cross-checked against the sum: a
    server that counts reasoning tokens in the total but not in the
    completion count would fail an otherwise good answer.
    """
    if not isinstance(raw, dict):
        raise _malformed("usage is missing")
    try:
        return Usage(
            input_tokens=int(raw["prompt_tokens"]),
            output_tokens=int(raw["completion_tokens"]),
        )
    except (KeyError, TypeError, ValueError):
        raise _malformed("usage is missing a token count") from None


def _error_for(status: int, headers: Mapping[str, str]) -> ProviderError:
    """Classify a non-200. The body is not read.

    One consequence worth knowing: this protocol answers 429 both for a
    temporary rate limit and for an exhausted quota or spend limit, and
    telling them apart means reading `error.code` out of the body. So an
    exhausted key is retried as if waiting would help, until the attempt
    budget runs out. Reading the body narrowly - the code, never the message -
    would classify it as terminal and would still carry nothing; that is a
    change to make when a live run shows it costing something, not before.
    """
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

    Only the delta-seconds form. The HTTP-date form is legal, and parsing it
    into a wait would mean trusting this host's clock against the provider's,
    which is how a skewed machine waits for an hour or not at all.
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
        case OutboundFailure.TIMEOUT:
            # Carried through rather than folded into `UNAVAILABLE`: the
            # request was sent, so whether it was served is unknown, and the
            # caller needs that distinction to decide between retrying and
            # escalating.
            return ProviderFailure.TIMEOUT
        case _:
            return ProviderFailure.UNAVAILABLE


def _malformed(what: str) -> ProviderError:
    return ProviderError(ProviderFailure.MALFORMED_RESPONSE, reason=what)


def build(
    client: GuardedClient, *, api_key: str, base_url: str | None = None
) -> OpenAICompatibleProvider:
    """The registry's entry point. See `registry.py`.

    A profile with no `base_url` gets the hosted endpoint. That is a real
    choice rather than an oversight: it is the common case, and refusing to
    build without one would raise an exception the dispatch loop reads as a
    programming error and retries. It is logged, because a key for some other
    server in this family would then be sent to the hosted endpoint, and an
    operator debugging that 401 deserves a line saying where the call went.
    """
    if not base_url:
        log.info("provider_default_base_url_used", provider=SLUG)
    return OpenAICompatibleProvider(client, api_key=api_key, base_url=base_url or DEFAULT_BASE_URL)
