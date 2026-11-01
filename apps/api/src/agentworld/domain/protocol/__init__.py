"""The canonical agent protocol - the one part of the system that is not replaceable.

`version` is the contract number, `types` the envelope, `taxonomy` the event
types this release folds, and `envelope` the parser. Nothing here imports a
framework, a database or a provider, which is what lets an integrator's test
build an event without standing anything up.
"""

from agentworld.domain.protocol.envelope import parse_envelope
from agentworld.domain.protocol.taxonomy import (
    CUSTOM_NAMESPACE,
    KNOWN_EVENTS,
    KNOWN_NAMESPACES,
    EventSpec,
    folds,
    is_custom,
    spec_for,
)
from agentworld.domain.protocol.types import (
    Correlation,
    EventEnvelope,
    Subject,
    SubjectKind,
)
from agentworld.domain.protocol.version import (
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    PROTOCOL_VERSION,
)

__all__ = [
    "CUSTOM_NAMESPACE",
    "KNOWN_EVENTS",
    "KNOWN_NAMESPACES",
    "PROTOCOL_MAJOR",
    "PROTOCOL_MINOR",
    "PROTOCOL_VERSION",
    "Correlation",
    "EventEnvelope",
    "EventSpec",
    "Subject",
    "SubjectKind",
    "folds",
    "is_custom",
    "parse_envelope",
    "spec_for",
]
