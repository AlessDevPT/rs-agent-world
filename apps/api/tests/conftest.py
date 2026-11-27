from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworld.main import create_app
from agentworld.settings import Settings


@pytest.fixture
def settings() -> Settings:
    # `realtime_publish_in_api=False` for the same reason the integration
    # package turns it off: the deployment default starts a publisher loop in
    # the lifespan, and a unit test that only wanted a route table would then
    # be polling a database it has no business touching.
    return Settings(
        environment="test",
        log_format="json",
        event_bus="inprocess",
        realtime_publish_in_api=False,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
