"""The execution, as the decision function sees it.

Two rules shape everything here, and both are about keeping `advance` a
function rather than a procedure.

**Nothing is looked up.** Every fact the decision needs is a field: the step
statuses, the outputs prior steps produced, whether an approval has been
granted. Assembling this from rows is the service layer's job, and it is the
layer that can apply tenant scoping. A pure function that could reach for a
row would need a session, and a session is a database.

**Nothing is read from the environment.** `now` is a field, not a call. A
decision function that reads a clock is not deterministic, and the point of
determinism here is that a production incident becomes a unit test: feed the
recorded state sequence back in and get the same decisions. That property
dies the first time anything inside calls `datetime.now()`.

The same applies to generated identity. Nothing in a decision carries a uuid,
because nothing here may invent one - `dispatches.id` and
`idempotency_token` come from database defaults, where they are generated
once per row rather than once per call.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentworld.domain.enums import ExecutionStatus, StepStatus


@dataclass(frozen=True, slots=True)
class StepState:
    """Where one step has got to, and what it produced.

    `attempt` is the attempt currently in flight or last finished, matching
    `dispatches.attempt` - which is immutable per row since T0, so the pair
    (step, attempt) identifies one unit of work for the whole of its life.
    """

    status: StepStatus = StepStatus.PENDING
    attempt: int = 0
    #: Whatever the step produced, for later steps' expressions to read.
    #: `None` and an empty mapping are different: the first means "nothing
    #: recorded", the second means "recorded, and empty".
    output: Mapping[str, Any] | None = None
    #: The attempt an approval was granted for, or None. An approval granted
    #: for attempt 1 does not authorise attempt 2: the whole point of
    #: re-asking is that the work changed.
    approved_attempt: int | None = None
    #: When a retry or a wait becomes due. Compared against `state.now`.
    run_after: dt.datetime | None = None
    #: Why it failed, carried so a `FailExecution` can quote something
    #: truthful without the decision function inventing a message.
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """One execution's whole observable condition.

    `steps` may be sparse: a step with no entry here has never been reached
    and is treated as `PENDING`. That is deliberate - an execution row is
    created before its step rows exist, and `advance` is called immediately
    after, so the first call sees an empty mapping and has to work.
    """

    status: ExecutionStatus
    #: Injected, never read. See the module docstring.
    now: dt.datetime
    #: The execution's input, which expressions reach as `$.input.…`.
    input: Mapping[str, Any] = field(default_factory=dict)
    steps: Mapping[str, StepState] = field(default_factory=dict)
    #: Copied onto the execution when it started, so editing the policy does
    #: not change the retry budget of work already in flight.
    max_attempts: int = 3

    def step(self, step_key: str) -> StepState:
        """The state of `step_key`, or a pristine `PENDING` one."""
        return self.steps.get(step_key, StepState())
