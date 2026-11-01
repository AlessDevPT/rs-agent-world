"""How a composite reference is spelled.

The proposal's example trace uses `exec-9f2a:research` as the ref of an
execution step and `exec-9f2a` as the execution's own, so the convention was
always there - but it was only ever shown, never stated, and a reducer cannot
fold a step event without knowing it. So it is written down here:

> The ref of an `execution_step` subject is `<execution_ref>:<step_key>`.
> Everything up to the **first** colon is the execution; everything after is
> the step key, colons and all.

First colon rather than last, because a step key is author-chosen and may
contain one, while an execution ref is server-generated and will not.

A producer that sends a bare step key instead is not refused - the event is
valid and is recorded - but it cannot be folded, because nothing identifies
which execution it belongs to. `correlation.execution_ref` is the supported
way to be explicit, and when both are present they must agree: a mismatch is
a producer bug that would otherwise fold a step onto the wrong execution.
"""

from __future__ import annotations

from dataclasses import dataclass

#: What separates an execution ref from a step key.
SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class StepReference:
    execution_ref: str
    step_key: str


def split_step_ref(ref: str, *, execution_ref: str | None = None) -> StepReference | None:
    """Split `<execution_ref>:<step_key>`, or None if it cannot be split.

    None means "recorded, folds into nothing", which is a legitimate outcome
    rather than an error - see the module docstring. The one case that *is* an
    error, a declared execution that disagrees with the ref's prefix, also
    returns None here: the caller records the event and the mismatch, because
    guessing which of the two the producer meant would be worse than folding
    neither.
    """
    execution, separator, step_key = ref.partition(SEPARATOR)
    if not separator or not execution or not step_key:
        # A bare key, or a trailing colon. Either way there is no execution.
        return None
    if execution_ref is not None and execution_ref != execution:
        return None
    return StepReference(execution_ref=execution, step_key=step_key)
