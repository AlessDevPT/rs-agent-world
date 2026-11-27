"""Fixtures for the unit suite.

The `fake` provider server is here rather than in `provider_cases.py`
because a fixture has to be visible to pytest in the module that asks for
it, and two test modules ask for this one - the conformance suite and the
Anthropic wire-shape tests. The server itself lives on `FakeProvider`; this
is only its lifecycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from provider_cases import FakeProvider


@pytest.fixture
async def fake() -> AsyncIterator[FakeProvider]:
    state = FakeProvider()
    server = await asyncio.start_server(state.handle, "127.0.0.1", 0)
    state.port = server.sockets[0].getsockname()[1]
    async with server:
        yield state
