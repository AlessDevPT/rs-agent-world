"""What `advance` is allowed to decide.

A closed union. The service layer matches on it exhaustively and turns each
member into an event, a state change, and - for `DispatchStep` alone - an
outbox row. That asymmetry is the whole architecture: `MutationTarget` has no
`dispatches` member, so no reducer can create work, and `DispatchStep` is the
only decision whose application writes to the outbox.

**Only the decisions this phase emits are here.** Three more are named in
`02-ORCHESTRATION.md` §4 and are deliberately absent:

- `RequestApproval` arrives with approvals, in Phase 3a. There is no
  `approvals` table yet, so a decision nothing can apply would be a branch
  with no behaviour and a test that asserts the branch exists.
- `RetryStep` arrives with the retry policy, in T8. Classifying a failure as
  retryable needs the backoff, the jitter, the attempt budget and the
  consequential-work exception, and inventing a shape for those now means
  T8 inheriting a guess.
- `PauseExecution` is not an `advance` output at all, on reflection. Pausing
  is something an operator does to an execution; `advance` *observes* a
  paused execution and declines to act. A decision function that could
  decide to pause would need a reason to, and there is none.

Adding a member is a small, visible change: the union, the service's match,
and whatever emits it. That is cheaper than carrying three members nothing
emits and nothing applies.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentworld.domain.enums import DispatchKind, SideEffect


@dataclass(frozen=True, slots=True)
class DispatchStep:
    """Work to be performed outside this process.

    The only decision whose application writes a `dispatches` row, and
    therefore the only one that can cause a side effect. Everything about it
    is resolved at decision time rather than looked up at execution time:
    `side_effect_class` is copied here from the tool's classification so
    that editing the tool while work is in flight cannot change the retry
    policy of work already decided.

    `instruction` carries credential *references* and never credential
    material - `infra/secrets` resolves those at the point of use.
    """

    step_key: str
    attempt: int
    kind: DispatchKind
    side_effect_class: SideEffect
    instruction: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompleteStep:
    """A step that finished inside this process, with no side effect.

    `condition` and `transform` are pure: evaluating them *is* performing
    them, so there is nothing to dispatch and nothing that could fail
    halfway.
    """

    step_key: str
    output: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkipStep:
    """A step that will not run because the branch reaching it was not taken.

    Distinct from a failure: nothing went wrong, and the execution can still
    complete. `reason` is shown to a user looking at why a branch is grey.
    """

    step_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class FailExecution:
    """The execution cannot continue.

    `reason` is written for a person reading an execution that stopped. It
    never carries anything resolved from a credential, a response body or an
    internal address - `advance` has access to none of those, which is one
    of the quieter benefits of it being pure.
    """

    reason: str
    #: The step that caused it, when one did. `None` for a whole-graph
    #: problem such as a definition that no longer validates.
    step_key: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteExecution:
    """Every step has reached a terminal state and none of them failed."""

    output: Mapping[str, Any] = field(default_factory=dict)
    #: When the decision was taken, from `state.now` rather than a clock.
    decided_at: dt.datetime | None = None


type Decision = DispatchStep | CompleteStep | SkipStep | FailExecution | CompleteExecution
