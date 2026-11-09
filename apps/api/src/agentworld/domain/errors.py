"""Domain errors.

These carry no HTTP knowledge. `agentworld.api.errors` is the only place that
maps them onto status codes and RFC 9457 problem documents, which keeps the
domain layer usable from a worker, a CLI or a test with no HTTP in sight.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for every expected, modelled failure."""

    code = "domain_error"
    title = "Domain error"

    def __init__(self, detail: str, **context: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.context = context


class NotFoundError(DomainError):
    """The resource does not exist, or is invisible to this tenant.

    Deliberately one error for both: distinguishing them leaks the existence of
    other tenants' resources (docs/03-API.md section 2).
    """

    code = "not_found"
    title = "Not found"


class AuthenticationFailedError(DomainError):
    """No usable credential was presented, or it did not verify.

    One error for every cause - expired, wrong signature, wrong audience,
    revoked key, malformed header, credential for the other plane. The caller
    learns only that authentication failed; which of those it was goes to the
    logs. An error that distinguishes them is a probing oracle.

    `challenge` becomes the `WWW-Authenticate` response header, so the 401
    says which scheme the endpoint actually wants.
    """

    code = "authentication_failed"
    title = "Authentication failed"

    def __init__(self, detail: str, *, challenge: str | None = None, **context: Any) -> None:
        super().__init__(detail, **context)
        self.challenge = challenge


class PermissionDeniedError(DomainError):
    code = "permission_denied"
    title = "Permission denied"


class ConflictError(DomainError):
    code = "conflict"
    title = "Conflict"


class ValidationFailedError(DomainError):
    code = "validation_failed"
    title = "Validation failed"


class CredentialUnreadableError(DomainError):
    """A stored credential exists and this server cannot decrypt it.

    A server fault, not a caller fault, so it is a 500: a retired
    key-encryption key, a restore that moved a row between workspaces, or
    tampering. Which of those it was goes to the logs with the row's id and
    `key_id`; the caller is told only that it could not be decrypted, because
    the difference is diagnostic for an operator and an oracle for anyone
    else.
    """

    code = "credential_unreadable"
    title = "Credential unreadable"


class OutboundRequestFailedError(DomainError):
    """A request this server made on a caller's behalf did not succeed.

    502, because the failure is upstream of this server rather than in the
    caller's request. The detail comes from `OutboundFailure.public_detail`
    and is deliberately uninformative: a blocked destination and a host that
    does not exist produce the same sentence, so an agent cannot use failure
    messages to map an internal network. Which it really was goes to the log
    with the reason attached.
    """

    code = "outbound_request_failed"
    title = "Outbound request failed"


class WorkflowNotPublishableError(ValidationFailedError):
    """A draft that cannot be published, with every reason.

    Carries `findings` as its own attribute rather than in `context`, because
    `LOCATOR_KEYS` deliberately keeps `context` out of a response - and these
    are the one thing the caller must see. "No" on its own is useless to the
    person who has to fix the graph, which is the same reason `validate`
    returns a report rather than a boolean.

    `agentworld.api.errors` renders them into the problem document's
    `errors` array, the same shape a pydantic failure produces, so a client
    has one parser for both.
    """

    code = "workflow_not_publishable"
    title = "Workflow not publishable"

    def __init__(self, detail: str, *, findings: list[dict[str, str]], **context: Any) -> None:
        super().__init__(detail, **context)
        self.findings = findings


class DelegationNotCompilableError(WorkflowNotPublishableError):
    """A delegation this release will not turn into a workflow, with reasons.

    A subclass rather than a sibling, and deliberately: the findings travel
    on the same attribute, render into the same `errors` array and resolve to
    the same 422, so whatever eventually exposes compiling needs no new error
    handling. Only the `code` differs, because "this delegation fans out" and
    "this graph has a dangling edge" are not the same refusal and a client
    that retried one would be wrong about the other.
    """

    code = "delegation_not_compilable"
    title = "Delegation not compilable"


class AgentNotTestableError(WorkflowNotPublishableError):
    """An agent `agent.test` will not run, with the reasons.

    The same shape as `DelegationNotCompilableError` and for the same
    reasons: findings on the attribute, rendered into the same `errors`
    array, resolving to the same 422. A distinct `code` because "this agent
    only reports its own activity" is not a graph problem and a client that
    offered to fix the graph would be offering nonsense.
    """

    code = "agent_not_testable"
    title = "Agent not testable"


class RateLimitedError(DomainError):
    code = "rate_limited"
    title = "Rate limited"

    def __init__(self, detail: str, retry_after_seconds: int, **context: Any) -> None:
        super().__init__(detail, **context)
        self.retry_after_seconds = retry_after_seconds


class DependencyUnavailableError(DomainError):
    """A backing service is down. Safe for the caller to retry with backoff."""

    code = "dependency_unavailable"
    title = "Dependency unavailable"
