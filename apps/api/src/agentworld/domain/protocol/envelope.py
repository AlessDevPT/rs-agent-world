"""Parse a raw event into an envelope, or say precisely why not.

This is where the compatibility promises either hold or quietly stop holding,
so each one is implemented as a named rule below rather than left to the
reader. The shape of the decision throughout: **reject what we cannot store
faithfully, keep everything else.**

What that means in the two directions that are easy to get wrong:

- An unknown event *type* is accepted. So is an unknown subject kind, an
  unknown correlation key and any payload at all. These have somewhere to go -
  `type` and `subject_kind` are text columns, `payload` is jsonb - so storing
  them costs nothing and refusing them would break a producer that is ahead of
  us, which is the whole point of a versioned protocol.
- An unknown `severity` is **rejected**, because `events.severity` is a native
  enum and there is nowhere to put a label it does not have. Coercing silently
  to `info` was the alternative, and it would write a value the producer never
  sent. A loud refusal beats a quiet lie.

No network, no database, no clock: `parse_envelope` is a pure function of its
input, which is what makes the awkward cases cheap to test.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from agentworld.domain.enums import EventSeverity
from agentworld.domain.errors import ValidationFailedError
from agentworld.domain.protocol.taxonomy import is_custom, spec_for
from agentworld.domain.protocol.types import Correlation, EventEnvelope, Subject, SubjectKind
from agentworld.domain.protocol.version import PROTOCOL_MAJOR

#: `namespace.name`, lowercase, at least one dot. The pattern is strict
#: because a type string ends up in metric names, log fields and URLs.
_TYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

_VERSION = re.compile(r"^(\d+)\.(\d+)$")

#: Bounds, every one of them because the value reaches an index, a label set
#: or a renderer. `dedup_key` is the one that matters most: it carries a unique
#: index per partition, and an unbounded key is an unbounded index entry.
MAX_DEDUP_KEY: Final = 200
MAX_TYPE: Final = 100
MAX_REF: Final = 200
MAX_LABELS: Final = 32
MAX_LABEL_KEY: Final = 64
MAX_LABEL_VALUE: Final = 256

#: Envelope keys this release reads. Anything else is kept in `extra`.
_KNOWN_FIELDS: Final = frozenset(
    {
        "dedup_key",
        "type",
        "protocol_version",
        "occurred_at",
        "subject",
        "correlation",
        "severity",
        "labels",
        "payload",
    }
)


def parse_envelope(raw: Mapping[str, Any]) -> EventEnvelope:
    """Validate `raw` and return an envelope.

    Raises `ValidationFailedError` with a `field` in its context, so the API
    layer can point a producer at the thing to fix rather than at the event.
    """
    if not isinstance(raw, Mapping):  # pragma: no cover - defensive
        raise _invalid("event", "an event must be an object")

    event_type = _required_string(raw, "type", MAX_TYPE)
    if not _TYPE.match(event_type):
        raise _invalid(
            "type",
            "a type is lowercase `namespace.name`, for example `agent.heartbeat`",
            value=event_type,
        )

    subject = _parse_subject(raw.get("subject"), event_type)

    return EventEnvelope(
        dedup_key=_required_string(raw, "dedup_key", MAX_DEDUP_KEY),
        type=event_type,
        protocol_version=_parse_version(raw.get("protocol_version")),
        occurred_at=_parse_occurred_at(raw.get("occurred_at")),
        subject=subject,
        correlation=_parse_correlation(raw.get("correlation")),
        severity=_parse_severity(raw.get("severity")),
        labels=_parse_labels(raw.get("labels")),
        payload=_parse_mapping(raw.get("payload"), "payload"),
        extra={key: value for key, value in raw.items() if key not in _KNOWN_FIELDS},
    )


# ------------------------------------------------------------------- fields ---


def _parse_version(raw: Any) -> str:
    """Accept any minor of our major, and nothing else.

    A later *minor* is fine: by promise, it only adds things, and the fields
    this release does not know are preserved rather than read. A different
    *major* is not fine, and accepting one would mean folding events whose
    meaning may have changed under types we recognise - which is worse than
    refusing them, because it is silent.
    """
    if raw is None:
        raise _invalid("protocol_version", "a protocol_version is required")
    if not isinstance(raw, str) or not (match := _VERSION.match(raw)):
        raise _invalid("protocol_version", "a protocol_version looks like `1.0`", value=raw)

    major = int(match.group(1))
    if major != PROTOCOL_MAJOR:
        raise _invalid(
            "protocol_version",
            f"this server speaks protocol {PROTOCOL_MAJOR}.x",
            value=raw,
        )
    return raw


def _parse_occurred_at(raw: Any) -> datetime:
    """Require an offset. A naive timestamp is a silent hours-off bug.

    There is no sensible default: assuming UTC would be right for most
    producers and quietly wrong for the one whose clock is local, and the
    error that follows looks like a reordered event rather than a missing `Z`.
    """
    if raw is None:
        raise _invalid("occurred_at", "an occurred_at is required")
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _invalid(
                "occurred_at", "an occurred_at is an ISO-8601 timestamp", value=raw
            ) from exc
    else:
        raise _invalid("occurred_at", "an occurred_at is an ISO-8601 timestamp", value=raw)

    if parsed.tzinfo is None:
        raise _invalid(
            "occurred_at",
            "an occurred_at needs a timezone offset, for example `2026-11-04T07:00:00Z`",
            value=raw,
        )
    return parsed


def _parse_subject(raw: Any, event_type: str) -> Subject:
    """A kind and a ref, with the kind checked only for types we know.

    The asymmetry is the point. For a known type, a wrong subject kind is a
    producer bug worth catching at the door - `workflow.step_succeeded` about
    an agent cannot be folded into anything. For an unknown type we have no
    expectation to check it against, and inventing one would reject the next
    version of the protocol.
    """
    if not isinstance(raw, Mapping):
        raise _invalid("subject", "a subject is an object with `kind` and `ref`", value=raw)

    kind = _required_string(raw, "kind", MAX_REF, prefix="subject.")
    ref = _required_string(raw, "ref", MAX_REF, prefix="subject.")

    spec = spec_for(event_type)
    if spec is not None and SubjectKind.parse(kind) not in spec.subjects:
        allowed = ", ".join(sorted(member.value for member in spec.subjects))
        raise _invalid(
            "subject.kind",
            f"`{event_type}` is about {allowed}",
            value=kind,
        )
    return Subject(kind=kind, ref=ref)


def _parse_correlation(raw: Any) -> Correlation:
    if raw is None:
        return Correlation()
    if not isinstance(raw, Mapping):
        raise _invalid("correlation", "a correlation is an object", value=raw)

    known: dict[str, str | None] = {}
    for key in Correlation.KNOWN_KEYS:
        value = raw.get(key)
        if value is None:
            known[key] = None
        elif isinstance(value, str) and value.strip():
            known[key] = value
        elif isinstance(value, str):
            # An empty string is not a reference. Treating it as one would
            # have ingest look up a row named "".
            raise _invalid(f"correlation.{key}", "a reference cannot be empty")
        else:
            raise _invalid(f"correlation.{key}", "a reference is a string", value=value)

    extra = {key: value for key, value in raw.items() if key not in Correlation.KNOWN_KEYS}
    return Correlation(**known, extra=extra)


def _parse_severity(raw: Any) -> EventSeverity:
    if raw is None:
        return EventSeverity.INFO
    if not isinstance(raw, str):
        raise _invalid("severity", "a severity is a string", value=raw)
    try:
        return EventSeverity(raw)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in EventSeverity)
        raise _invalid("severity", f"a severity is one of {allowed}", value=raw) from exc


def _parse_labels(raw: Any) -> Mapping[str, str]:
    """Bounded, and string-valued.

    An unbounded label set is how a queryable table stops being queryable, and
    a label whose value is an object is a payload field that has wandered into
    the index.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _invalid("labels", "labels are an object of string values", value=raw)
    if len(raw) > MAX_LABELS:
        raise _invalid("labels", f"at most {MAX_LABELS} labels", value=len(raw))

    labels: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or len(key) > MAX_LABEL_KEY:
            raise _invalid("labels", f"a label name is 1-{MAX_LABEL_KEY} characters", value=key)
        if not isinstance(value, str) or len(value) > MAX_LABEL_VALUE:
            raise _invalid(
                f"labels.{key}",
                f"a label value is a string of at most {MAX_LABEL_VALUE} characters",
                value=value,
            )
        labels[key] = value
    return labels


def _parse_mapping(raw: Any, field: str) -> Mapping[str, Any]:
    """Anything JSON-shaped, kept verbatim.

    No schema per event type, deliberately. A payload schema would make every
    new producer field a server release, and the promise is the opposite:
    unknown fields are preserved.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _invalid(field, f"a {field} is an object", value=raw)
    return dict(raw)


# ------------------------------------------------------------------ helpers ---


def _required_string(raw: Mapping[str, Any], field: str, limit: int, *, prefix: str = "") -> str:
    value = raw.get(field)
    if value is None:
        raise _invalid(f"{prefix}{field}", f"a {field} is required")
    if not isinstance(value, str):
        raise _invalid(f"{prefix}{field}", f"a {field} is a string", value=value)
    stripped = value.strip()
    if not stripped:
        raise _invalid(f"{prefix}{field}", f"a {field} cannot be empty")
    if len(stripped) > limit:
        raise _invalid(
            f"{prefix}{field}", f"a {field} is at most {limit} characters", value=len(stripped)
        )
    return stripped


def _invalid(field: str, detail: str, **context: Any) -> ValidationFailedError:
    return ValidationFailedError(detail, field=field, **context)


__all__ = ["is_custom", "parse_envelope", "spec_for"]
