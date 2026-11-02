"""Model providers: the only package that knows a vendor exists.

Callers import `ModelProvider`, `CompletionRequest`, `Completion` and
`ProviderError` from here and never name an implementation. The one place a
slug turns into code is `registry.build`, and the slug arrives as data from a
`model_profiles` row.

Two rules make that structural rather than aspirational, both in
`pyproject.toml`: a provider SDK may not be imported outside this package,
and `httpx` may not be imported outside `infra/net` - so a provider cannot
quietly acquire its own HTTP client either. `test_no_vendor_outside_providers`
adds the third, which no import contract can express: that a vendor's *name*
does not appear in source outside this package, in a string, a comment or a
constant.
"""

from __future__ import annotations

from agentworld.infra.providers.base import (
    Completion,
    CompletionRequest,
    Message,
    ModelProvider,
    ProviderError,
    ProviderFailure,
    Role,
    StopReason,
    ToolCall,
    ToolDefinition,
    Usage,
)
from agentworld.infra.providers.registry import (
    UnknownProviderError,
    available,
    build,
)

__all__ = [
    "Completion",
    "CompletionRequest",
    "Message",
    "ModelProvider",
    "ProviderError",
    "ProviderFailure",
    "Role",
    "StopReason",
    "ToolCall",
    "ToolDefinition",
    "UnknownProviderError",
    "Usage",
    "available",
    "build",
]
