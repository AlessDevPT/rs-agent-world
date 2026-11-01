"""Parsing an event, and the compatibility promises as executable claims.

The protocol is the contract integrators build against, so the promises in
`docs/03-API.md` §5 are tested here rather than described. Pure functions, no
fixtures, no database - the awkward cases are cheap, which is the argument for
having put the parser in the domain layer.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from agentworld.domain.enums import EventSeverity
from agentworld.domain.errors import ValidationFailedError
from agentworld.domain.protocol import (
    KNOWN_EVENTS,
    PROTOCOL_VERSION,
    SubjectKind,
    folds,
    is_custom,
    parse_envelope,
    spec_for,
)
from agentworld.domain.protocol.envelope import MAX_DEDUP_KEY, MAX_LABELS


def event(**overrides: Any) -> dict[str, Any]:
    """A valid `workflow.step_succeeded`, from the proposal's own example."""
    base: dict[str, Any] = {
        "dedup_key": "exec-9f2a:step-research:1",
        "type": "workflow.step_succeeded",
        "protocol_version": PROTOCOL_VERSION,
        "occurred_at": "2026-11-04T07:00:11.204Z",
        "subject": {"kind": "execution_step", "ref": "exec-9f2a:research"},
        "correlation": {"execution_ref": "exec-9f2a", "agent_key": "researcher"},
        "severity": "info",
        "labels": {"region": "eu-west-1"},
        "payload": {"output_summary": "6 competitors, 14 sources", "tokens": 8421},
    }
    base.update(overrides)
    return base


def field_of(caught: pytest.ExceptionInfo[ValidationFailedError]) -> str:
    return str(caught.value.context["field"])


# ------------------------------------------------------------- the happy path ---


def test_the_proposals_example_parses() -> None:
    envelope = parse_envelope(event())

    assert envelope.type == "workflow.step_succeeded"
    assert envelope.namespace == "workflow"
    assert envelope.subject.kind == "execution_step"
    assert envelope.subject.known_kind is SubjectKind.EXECUTION_STEP
    assert envelope.correlation.execution_ref == "exec-9f2a"
    assert envelope.correlation.agent_key == "researcher"
    assert envelope.severity is EventSeverity.INFO
    assert envelope.payload["tokens"] == 8421
    assert envelope.occurred_at == dt.datetime(2026, 11, 4, 7, 0, 11, 204000, tzinfo=dt.UTC)


def test_an_envelope_is_immutable() -> None:
    """It is evidence of what a producer said, not a working copy."""
    envelope = parse_envelope(event())
    with pytest.raises(AttributeError):
        envelope.severity = EventSeverity.ERROR  # type: ignore[misc]


def test_only_the_subject_and_type_are_required_beyond_the_header() -> None:
    """A bare heartbeat relates to nothing else, and that is not an error."""
    envelope = parse_envelope(
        {
            "dedup_key": "agent-1:hb:900",
            "type": "agent.heartbeat",
            "protocol_version": PROTOCOL_VERSION,
            "occurred_at": "2026-11-04T07:00:00Z",
            "subject": {"kind": "agent", "ref": "researcher"},
        }
    )
    assert envelope.correlation.is_empty()
    assert envelope.labels == {}
    assert envelope.payload == {}
    assert envelope.severity is EventSeverity.INFO


# --------------------------------------------- promise 1: unknown types stored ---


def test_an_unknown_event_type_is_accepted() -> None:
    """The first promise. A producer ahead of this release still works."""
    envelope = parse_envelope(event(type="workflow.step_teleported"))
    assert envelope.type == "workflow.step_teleported"
    assert spec_for(envelope.type) is None
    assert not folds(envelope.type)


def test_an_unknown_type_is_not_checked_against_a_subject_kind() -> None:
    """There is no expectation to check it against, so inventing one would
    reject exactly the producer the promise exists to protect."""
    envelope = parse_envelope(
        event(type="galaxy.collapsed", subject={"kind": "nebula", "ref": "n-1"})
    )
    assert envelope.subject.known_kind is None


def test_a_custom_type_is_accepted_and_never_folds() -> None:
    envelope = parse_envelope(event(type="custom.sales_quota_reached"))
    assert is_custom(envelope.type)
    assert not folds(envelope.type)


# ------------------------------------------ promise 2: unknown fields preserved ---


def test_unknown_payload_fields_are_preserved() -> None:
    envelope = parse_envelope(event(payload={"tokens": 1, "invented_later": {"a": [1, 2]}}))
    assert envelope.payload["invented_later"] == {"a": [1, 2]}


def test_unknown_correlation_keys_are_preserved() -> None:
    """So a later producer's correlation survives an earlier server."""
    envelope = parse_envelope(
        event(correlation={"execution_ref": "exec-9f2a", "shipment_ref": "shp-7"})
    )
    assert envelope.correlation.execution_ref == "exec-9f2a"
    assert envelope.correlation.extra == {"shipment_ref": "shp-7"}


def test_unknown_envelope_fields_are_preserved() -> None:
    envelope = parse_envelope(event(trace_context={"traceparent": "00-abc"}))
    assert envelope.extra == {"trace_context": {"traceparent": "00-abc"}}


# -------------------------------------------------- what is rejected, and why ---


def test_a_different_protocol_major_is_refused() -> None:
    """Folding a 2.x event under a 1.x type would be silently wrong, which is
    worse than refusing it."""
    with pytest.raises(ValidationFailedError) as caught:
        parse_envelope(event(protocol_version="2.0"))
    assert field_of(caught) == "protocol_version"


def test_a_later_minor_is_accepted() -> None:
    """By promise a minor only adds, and what it adds is preserved above."""
    assert parse_envelope(event(protocol_version="1.7")).protocol_version == "1.7"


def test_an_unknown_severity_is_refused() -> None:
    """`events.severity` is a native enum, so there is nowhere to put a label
    it does not have. Coercing to `info` would record something the producer
    never sent."""
    with pytest.raises(ValidationFailedError) as caught:
        parse_envelope(event(severity="catastrophic"))
    assert field_of(caught) == "severity"
    assert "catastrophic" in str(caught.value.context["value"])


def test_a_naive_occurred_at_is_refused() -> None:
    """A missing offset is an hours-off bug that reads as a reordered event."""
    with pytest.raises(ValidationFailedError) as caught:
        parse_envelope(event(occurred_at="2026-11-04T07:00:11"))
    assert field_of(caught) == "occurred_at"


def test_a_known_type_about_the_wrong_subject_is_refused() -> None:
    """A producer bug, catchable at the door: a step event about an agent
    cannot be folded into anything."""
    with pytest.raises(ValidationFailedError) as caught:
        parse_envelope(event(subject={"kind": "agent", "ref": "researcher"}))
    assert field_of(caught) == "subject.kind"
    assert "execution_step" in caught.value.detail


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"dedup_key": None}, "dedup_key"),
        ({"dedup_key": "   "}, "dedup_key"),
        ({"dedup_key": "k" * (MAX_DEDUP_KEY + 1)}, "dedup_key"),
        ({"type": "Workflow.Step"}, "type"),
        ({"type": "workflow"}, "type"),
        ({"type": "workflow..step"}, "type"),
        ({"protocol_version": "1"}, "protocol_version"),
        ({"protocol_version": None}, "protocol_version"),
        ({"occurred_at": None}, "occurred_at"),
        ({"occurred_at": "not a date"}, "occurred_at"),
        ({"occurred_at": 1762239611}, "occurred_at"),
        ({"subject": None}, "subject"),
        ({"subject": {"kind": "execution_step"}}, "subject.ref"),
        ({"subject": {"ref": "exec-9f2a:research"}}, "subject.kind"),
        ({"correlation": {"agent_key": ""}}, "correlation.agent_key"),
        ({"correlation": {"agent_key": 7}}, "correlation.agent_key"),
        ({"correlation": "exec-9f2a"}, "correlation"),
        ({"labels": {"region": {"nested": True}}}, "labels.region"),
        ({"labels": {"": "eu"}}, "labels"),
        ({"labels": [1, 2]}, "labels"),
        ({"payload": "a string"}, "payload"),
    ],
)
def test_malformed_events_name_the_field_to_fix(overrides: dict[str, Any], field: str) -> None:
    """Every refusal points a producer at the thing to change.

    An error that says only "invalid event" makes an integrator bisect their
    own payload, which is the experience this is meant to prevent.
    """
    with pytest.raises(ValidationFailedError) as caught:
        parse_envelope(event(**overrides))
    assert field_of(caught) == field


def test_too_many_labels_are_refused() -> None:
    """An unbounded label set is how a queryable table stops being one."""
    with pytest.raises(ValidationFailedError) as caught:
        parse_envelope(event(labels={f"k{i}": "v" for i in range(MAX_LABELS + 1)}))
    assert field_of(caught) == "labels"


# ------------------------------------------------------------- clocks and skew ---


def test_skew_is_measured_against_the_server_and_not_corrected() -> None:
    """A producer whose clock is wrong is a fact worth keeping about it."""
    envelope = parse_envelope(event(occurred_at="2026-11-04T07:00:00Z"))
    received = dt.datetime(2026, 11, 4, 7, 0, 2, tzinfo=dt.UTC)

    assert envelope.skew_ms(received) == -2000
    assert envelope.occurred_at == dt.datetime(2026, 11, 4, 7, 0, 0, tzinfo=dt.UTC)


def test_a_clock_set_in_the_future_reads_as_positive_skew() -> None:
    """Surfaced rather than clamped: clamping hides the misconfiguration."""
    envelope = parse_envelope(event(occurred_at="2026-11-04T08:00:00Z"))
    received = dt.datetime(2026, 11, 4, 7, 0, 0, tzinfo=dt.UTC)
    assert envelope.skew_ms(received) == 3_600_000


def test_an_offset_other_than_utc_is_accepted_and_normalised_by_comparison() -> None:
    """A producer in another zone is not a producer with a broken clock."""
    envelope = parse_envelope(event(occurred_at="2026-11-04T09:00:11.204+02:00"))
    assert envelope.occurred_at == dt.datetime(2026, 11, 4, 7, 0, 11, 204000, tzinfo=dt.UTC)


# ------------------------------------------------------------------- taxonomy ---


def test_every_known_event_type_is_well_formed() -> None:
    """The registry is hand-written, so it gets the same check as input."""
    for event_type, spec in KNOWN_EVENTS.items():
        assert (
            parse_envelope(
                event(
                    type=event_type,
                    subject={"kind": spec.subjects[0].value, "ref": "r-1"},
                )
            ).type
            == event_type
        )


def test_no_known_event_describes_editing_a_definition() -> None:
    """Replaying the log must never re-create a definition, so the types that
    would let it are absent rather than ignored."""
    forbidden = {"created", "updated", "deleted", "published", "edited", "configured"}
    offenders = [
        event_type
        for event_type in KNOWN_EVENTS
        if event_type.split(".", 1)[1] in forbidden
        # `task.created` is a unit of work appearing, not a definition change.
        and not event_type.startswith("task.")
    ]
    assert offenders == []


def test_log_and_metric_events_fold_into_no_state() -> None:
    """They are recorded. Folding them would make the log its own state."""
    assert not folds("log.emitted")
    assert not folds("metric.recorded")
