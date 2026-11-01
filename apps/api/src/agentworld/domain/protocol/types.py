"""The event envelope, as the domain sees it.

Frozen dataclasses with no framework in sight, like every other domain type.
An envelope is what a producer sent, parsed and validated but not interpreted:
`envelope.py` builds one, `taxonomy.py` says which types are known, and the
reducers decide what each one means.

Two shapes here are the ones the previous protocol got wrong, and they are
worth naming. `subject` replaces a flat `agent_key` / `task_ref` / `run_ref`
trio: an execution, a step and an approval are all subjects, and bolting each
onto an agent-shaped envelope yields six optional ids and no room for a
seventh. `correlation` then carries everything else the event relates to, so
the subject never has to be a composite.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from agentworld.domain.enums import EventSeverity


class SubjectKind(StrEnum):
    """What an event can be about.

    Stored as text rather than a native enum, and parsed leniently: a producer
    on a later protocol minor may send a kind this release has never heard of,
    and the protocol's first promise is that unknown things are stored rather
    than rejected. `Subject.kind` is therefore a plain string, and this enum is
    the set a *known* event type is allowed to use.
    """

    AGENT = "agent"
    TASK = "task"
    RUN = "run"
    WORKFLOW_EXECUTION = "workflow_execution"
    EXECUTION_STEP = "execution_step"
    APPROVAL = "approval"
    TOOL_CALL = "tool_call"
    PROJECT = "project"

    @classmethod
    def parse(cls, raw: str) -> SubjectKind | None:
        """The member for `raw`, or None if this release does not know it."""
        try:
            return cls(raw)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class Subject:
    """What the event is about: a kind, and a producer-chosen reference.

    `ref` is the producer's own name for the thing - `exec-9f2a:research`,
    `researcher`, `run-3f9a`. It is deliberately not a database id: a producer
    cannot know one, and requiring it would make the protocol depend on our
    storage. Resolving a ref to a row is ingest's job, and it may fail without
    the event being invalid.
    """

    kind: str
    ref: str

    @property
    def known_kind(self) -> SubjectKind | None:
        return SubjectKind.parse(self.kind)


@dataclass(frozen=True, slots=True)
class Correlation:
    """Everything else the event relates to, by producer-chosen reference.

    Every field is optional because an event may legitimately relate to
    nothing else: a bare heartbeat has a subject and no correlation at all.
    `extra` keeps keys this release does not know, so a later producer's
    correlation survives a round trip through an earlier server.
    """

    agent_key: str | None = None
    task_ref: str | None = None
    run_ref: str | None = None
    execution_ref: str | None = None
    execution_step_ref: str | None = None
    workflow_slug: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    #: The keys this release reads. Anything else lands in `extra`.
    #: `ClassVar` is not decoration: a bare annotation here would make this a
    #: dataclass field, and so a constructor argument.
    KNOWN_KEYS: ClassVar[tuple[str, ...]] = (
        "agent_key",
        "task_ref",
        "run_ref",
        "execution_ref",
        "execution_step_ref",
        "workflow_slug",
    )

    def is_empty(self) -> bool:
        return not any(
            (
                self.agent_key,
                self.task_ref,
                self.run_ref,
                self.execution_ref,
                self.execution_step_ref,
                self.workflow_slug,
                self.extra,
            )
        )


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """One event as a producer sent it, validated and not yet interpreted.

    `received_at` is absent on purpose. It is the server's clock, assigned when
    the row is written, and an envelope that carried one would invite a
    producer to set it. The skew between the two is recorded by ingest - see
    `skew_ms` - and never silently corrected, because a clock that is wrong is
    a fact about the producer worth keeping.
    """

    dedup_key: str
    type: str
    protocol_version: str
    occurred_at: datetime
    subject: Subject
    correlation: Correlation = field(default_factory=Correlation)
    severity: EventSeverity = EventSeverity.INFO
    labels: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    #: Envelope-level keys this release does not know, preserved verbatim.
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        """`workflow` for `workflow.step_succeeded`."""
        return self.type.split(".", 1)[0]

    def skew_ms(self, received_at: datetime) -> int:
        """How far ahead of the server the producer's clock ran, in ms.

        Negative means the producer is behind, which is the ordinary case: an
        event takes time to arrive. Large positive values mean a clock set in
        the future, which is worth surfacing rather than clamping.
        """
        return round((self.occurred_at - received_at).total_seconds() * 1000)
