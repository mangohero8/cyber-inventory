"""Fixtures for the functional-service contract tests.

The autouse `clean_store` fixture and the shared `client` come from
tests/conftest.py and apply here too.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import store as store_module
from app.main import app

APP_LOGGER = "cyber-inventory"


@pytest.fixture
def lenient_client() -> TestClient:
    """A client that hands back 500 responses instead of re-raising.

    The shared `client` re-raises server exceptions into the test, which hides
    what an HTTP caller actually receives. Tests about 5xx behaviour need the
    real response: status, body and headers.
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def broken_store(monkeypatch) -> None:
    """Make the readiness dependency check (`AssetStore.count`) raise."""

    def fail(self):
        raise RuntimeError("store is down")

    monkeypatch.setattr(store_module.AssetStore, "count", fail)


@pytest.fixture
def failing_list(monkeypatch) -> None:
    """Make listing raise, producing an unhandled exception inside a route."""

    def fail(self, **kwargs):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(store_module.AssetStore, "list", fail)


@pytest.fixture
def app_events(caplog) -> Callable[..., list[dict[str, Any]]]:
    """Collect records from the application logger as flat dicts.

    Each item is {"message", "level", "record", **extra_fields} -- the same
    fields Cloud Logging indexes from the JSON line on stdout.
    """
    caplog.set_level(logging.INFO)

    def collect(
        message: str | None = None, event: str | None = None
    ) -> list[dict[str, Any]]:
        items = []
        for record in caplog.records:
            if record.name != APP_LOGGER:
                continue
            fields = dict(getattr(record, "extra_fields", {}))
            if message is not None and record.getMessage() != message:
                continue
            if event is not None and fields.get("event") != event:
                continue
            items.append(
                {
                    "message": record.getMessage(),
                    "level": record.levelname,
                    "record": record,
                    **fields,
                }
            )
        return items

    return collect
