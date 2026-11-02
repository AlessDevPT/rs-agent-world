"""Which implementation serves a `provider_slug`, and nothing else.

The registry is the whole of "adding a provider adds a file and an entry".
It holds no model names and no capabilities: a model id lives in a
`model_profiles` row as data, because the set of models changes weekly and a
list of them in source is a list that is wrong by the time it ships.

A base URL may be overridden per deployment. That is how a proxy, a regional
endpoint or a gateway is pointed at, and it is also how the deterministic
tests reach a local server speaking the same protocol - which is the same
mechanism, not a test hook. Whatever it is set to still goes through the
guarded client and is still validated against the egress policy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from agentworld.infra.net import GuardedClient
from agentworld.infra.providers import anthropic
from agentworld.infra.providers.base import ModelProvider

#: slug -> how to build it. One entry per implementation.
_BUILDERS: Final[Mapping[str, Callable[..., ModelProvider]]] = {
    anthropic.SLUG: anthropic.build,
}


class UnknownProviderError(ValueError):
    """A `provider_slug` with no implementation in this build.

    A `ValueError` rather than a `ProviderError`: nothing was attempted, so
    there is no call to classify. It is a configuration mistake - a profile
    naming a provider this deployment does not have - and the service layer
    turns it into a validation failure naming the slug, which is safe
    because the slug is something the caller wrote.
    """


def available() -> frozenset[str]:
    """Every slug this build can serve. For the providers endpoint, later."""
    return frozenset(_BUILDERS)


def build(
    slug: str,
    client: GuardedClient,
    *,
    api_key: str,
    base_url: str | None = None,
) -> ModelProvider:
    """The provider for `slug`, over the client it must use.

    The client is an argument rather than something each provider makes for
    itself, so there is no path by which a provider acquires an unguarded
    one: it does not construct a client, it is handed one.
    """
    builder = _BUILDERS.get(slug)
    if builder is None:
        raise UnknownProviderError(slug)
    return builder(client, api_key=api_key, base_url=base_url)
