"""What an outbound failure is allowed to say.

`06-OUTBOUND-SECURITY.md` section 5 asks for two things that cannot both be
true of the same value: a fixed set of failure kinds including
`blocked_destination`, and that "an attacker gets the same answer from a
blocked address as from one that does not exist". If the kind is what the
caller sees, those contradict.

So the kind is **internal**. It goes to the log, and it decides whether a
retry could ever help. What leaves this module towards a caller is
`public_detail`, and `BLOCKED_DESTINATION` and `UNREACHABLE` share one -
which is the sentence section 5 actually asks for.

`TIMEOUT`, `TLS_FAILURE` and `HTTP_ERROR` keep their own wording, and that is
not an inconsistency. A timeout is self-evident from how long the caller
waited; a TLS failure can only happen after a connection was established, so
it already tells the caller the host exists; an HTTP error means the request
arrived. None of them reveals anything the caller did not already have.

Nothing here carries a response body, a header, a resolved address or an
upstream error string. Those exist on the exception for the log and are
deliberately absent from `public_detail`.
"""

from __future__ import annotations

from enum import StrEnum

from agentworld.domain.errors import OutboundRequestFailedError

#: Said for a destination that was refused and for one that does not answer.
#: One sentence for both, so neither can be told from the other.
_UNREACHABLE_DETAIL = "The destination could not be reached"


class OutboundFailure(StrEnum):
    """Why an outbound request failed, in terms a retry policy can use."""

    #: Refused by the egress policy, before any connection was attempted.
    BLOCKED_DESTINATION = "blocked_destination"
    #: DNS did not answer, or the connection was refused or reset.
    UNREACHABLE = "unreachable"
    #: Connect or total timeout elapsed.
    TIMEOUT = "timeout"
    #: The TLS handshake failed: a bad certificate, a name mismatch.
    TLS_FAILURE = "tls_failure"
    #: A response arrived and the caller treats its status as a failure.
    HTTP_ERROR = "http_error"

    @property
    def is_retryable(self) -> bool:
        """Whether waiting could change the answer.

        A blocked destination will not become allowed, and a certificate
        will not start verifying, so both are terminal. The caller decides
        what to do about an `HTTP_ERROR`, because that depends on the status
        and this layer does not interpret statuses.

        Note the tension, which `10-T1`-style honesty requires stating
        rather than hiding: a terminal `BLOCKED_DESTINATION` and a retried
        `UNREACHABLE` are indistinguishable in their message and
        distinguishable in how often they are attempted. Closing that is a
        decision for the retry policy in T8 - either blocked destinations
        burn the same attempts, or retries are not observable per attempt.
        """
        return self in (OutboundFailure.UNREACHABLE, OutboundFailure.TIMEOUT)

    @property
    def public_detail(self) -> str:
        """The only text about this failure that may reach a caller."""
        match self:
            case OutboundFailure.BLOCKED_DESTINATION | OutboundFailure.UNREACHABLE:
                return _UNREACHABLE_DETAIL
            case OutboundFailure.TIMEOUT:
                return "The destination did not respond in time"
            case OutboundFailure.TLS_FAILURE:
                return "The destination's TLS certificate could not be verified"
            case OutboundFailure.HTTP_ERROR:
                return "The destination returned an error"


class OutboundError(Exception):
    """An outbound request that did not produce a usable response.

    `reason` is for the server-side log and names what actually happened -
    which blocked range, which socket error. It is never rendered towards a
    caller: `public_detail` is. The two are separate attributes rather than
    one formatted string so that putting the wrong one in a response has to
    be a deliberate act rather than a default.
    """

    def __init__(self, failure: OutboundFailure, *, reason: str) -> None:
        super().__init__(failure.public_detail)
        self.failure = failure
        self.reason = reason

    @property
    def public_detail(self) -> str:
        return self.failure.public_detail

    def __repr__(self) -> str:
        """Deliberately not including `reason`.

        A `repr` ends up in tracebacks, in debugger frames and in log lines
        written by code that did not think about this module. The kind is
        safe there; the reason names an internal address.
        """
        return f"OutboundError({self.failure.value})"


def as_domain_error(exc: OutboundError) -> OutboundRequestFailedError:
    """The caller-facing form of an outbound failure.

    The conversion is here rather than at each call site so that there is
    one place where the decision "what may a caller be told" is taken. It
    carries `failure` in the error's context for the log, and `reason` is
    not in it: `DomainError.context` is splatted into the server log for a
    5xx, which is fine, but it is also the dictionary a future author might
    reasonably decide to render, and the reason names an internal address.
    """
    return OutboundRequestFailedError(exc.public_detail, failure=exc.failure.value)
