"""One surface, `complete`, and the vocabulary it speaks.

Everything here is provider-neutral by construction: there is no field whose
meaning comes from one vendor's API, and no vendor's name appears in this
file. A provider is a file in this package plus a registry entry, and the
second implementation is written in this phase specifically to find out
whether that is true or merely intended.

**Tool definitions are in the request and tool calls are in the response, and
nothing here executes anything.** A model answering with a tool call is the
model saying what it would like done. Turning that into an action is the
step runner's decision, gated by the grant that allowed the tool and - once
Phase 3a adds them - by an approval. A successful model response is not
authority to act, and this layer could not act on one if it wanted to: it has
no tool registry, no dispatcher and no network except the one call it just
made.

They are here rather than deferred because text-in-text-out is portable
between any two providers by accident, and tool calling is where provider
APIs actually differ. An interface that has never carried a tool definition
has not been tested as an interface.

**Multi-part content is not modelled.** `Message.content` is text. Images,
documents and citations are real provider features and each has a different
shape; modelling them before a caller needs one means guessing at three
schemas and getting the abstraction from the guess rather than from the use.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class Role(StrEnum):
    """Who said a message.

    There is no `system` member: a system prompt is not a turn in the
    conversation, and the two providers this phase implements both carry it
    out of band. `CompletionRequest.system` is where it goes.
    """

    USER = "user"
    ASSISTANT = "assistant"


class StopReason(StrEnum):
    """Why the model stopped generating."""

    #: It finished what it was saying.
    COMPLETED = "completed"
    #: It hit `max_output_tokens`. The text is a fragment.
    MAX_TOKENS = "max_tokens"
    #: It wants a tool called. `Completion.tool_calls` is non-empty.
    TOOL_USE = "tool_use"
    #: It produced one of `stop_sequences`.
    STOPPED = "stopped"
    #: Something else, including a reason this vocabulary does not have a
    #: member for. Mapped rather than passed through, so a new vendor string
    #: cannot reach a caller that switches on this.
    OTHER = "other"


class ProviderFailure(StrEnum):
    """Why a call failed, in terms a retry policy can act on.

    The same shape as `infra.net.OutboundFailure` and for the same reason:
    the caller needs to decide whether to try again, and that decision must
    not be made by matching on a message.
    """

    #: 429. `ProviderError.retry_after_seconds` may carry the header.
    RATE_LIMITED = "rate_limited"
    #: 401 or 403. The key is wrong, revoked, or lacks access to the model.
    UNAUTHORIZED = "unauthorized"
    #: 400 or 422. The request is wrong and will be wrong next time.
    INVALID_REQUEST = "invalid_request"
    #: The provider is up and shedding load.
    OVERLOADED = "overloaded"
    #: 5xx, or the request never arrived.
    UNAVAILABLE = "unavailable"
    #: A 200 this code could not read. Not retryable: the provider is
    #: answering, and answering with something unexpected again is the
    #: likely outcome.
    MALFORMED_RESPONSE = "malformed_response"

    @property
    def is_retryable(self) -> bool:
        """Whether trying again could plausibly give a different answer.

        `UNAUTHORIZED` and `INVALID_REQUEST` are terminal because nothing
        about waiting changes them, and retrying a 400 three times turns one
        caller's mistake into three times the spend.
        """
        return self in (
            ProviderFailure.RATE_LIMITED,
            ProviderFailure.OVERLOADED,
            ProviderFailure.UNAVAILABLE,
        )


class ProviderError(Exception):
    """A provider call that produced no usable completion.

    `reason` is for the server-side log. It never carries the response body:
    a provider's error text can quote the request, and the request contains
    whatever the agent was working on.
    """

    def __init__(
        self,
        failure: ProviderFailure,
        *,
        reason: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"The model provider call failed ({failure.value})")
        self.failure = failure
        self.reason = reason
        #: From `Retry-After` when the provider sent one. Honoured by the
        #: retry policy in preference to its own backoff, because the
        #: provider knows when it will serve this key again and the client
        #: is guessing.
        self.retry_after_seconds = retry_after_seconds

    def __repr__(self) -> str:
        return f"ProviderError({self.failure.value})"


@dataclass(frozen=True, slots=True)
class Message:
    """One turn. `content` is text; see the module docstring."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool the model may ask for, as a name and a JSON Schema.

    The schema is passed through to the provider unexamined. Validating it
    here would mean this layer owning a JSON Schema implementation and
    disagreeing with the provider's about edge cases, which helps nobody.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The model asking for a tool to be called. Nothing here calls it."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    """What the call cost, in the only unit every provider reports."""

    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """One call's worth of input.

    `model` is a string this layer does not interpret: it comes from a
    `model_profiles` row, which stores it as data precisely so that the
    domain never has to know which models exist.
    """

    model: str
    messages: Sequence[Message]
    max_output_tokens: int
    system: str | None = None
    temperature: float | None = None
    stop_sequences: Sequence[str] = ()
    tools: Sequence[ToolDefinition] = ()
    #: Forwarded to providers that accept one, so a retry after an uncertain
    #: outcome cannot be billed or served twice. `None` means the caller has
    #: no opinion; the dispatch's token is the one that belongs here.
    idempotency_token: str | None = None


@dataclass(frozen=True, slots=True)
class Completion:
    """What came back, in this vocabulary rather than the provider's."""

    text: str
    usage: Usage
    stop_reason: StopReason
    #: Echoed from the response, which may differ from the requested model
    #: when a provider resolves an alias. Recorded rather than assumed.
    model: str
    tool_calls: Sequence[ToolCall] = field(default_factory=tuple)


class ModelProvider(Protocol):
    """What every provider implements, and the whole of it.

    One method. A provider that needs a second one to be useful is a
    provider whose shape has leaked into its callers, which is the thing
    this Protocol exists to prevent.
    """

    @property
    def slug(self) -> str:
        """The `model_profiles.provider_slug` this implementation answers to."""
        ...

    async def complete(self, request: CompletionRequest) -> Completion: ...
