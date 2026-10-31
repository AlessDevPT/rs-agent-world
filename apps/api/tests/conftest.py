from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworld.main import create_app
from agentworld.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", log_format="json", event_bus="inprocess")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
