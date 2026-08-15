"""Shared test setup.

conftest.py is a special filename: pytest loads it automatically and makes
everything in it available to every test file in this directory. You never
import it yourself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import get_store


@pytest.fixture(autouse=True)
def clean_store():
    """Empty the asset list before and after every single test.

    `autouse=True` means this runs for every test without any test asking
    for it. That matters because the store is a module-level singleton that
    survives between tests -- without this, test A's leftover data changes
    test B's result, and which tests pass starts depending on what order
    they ran in. That class of bug is miserable to track down, so we prevent
    it structurally instead of relying on discipline.
    """
    store = get_store()
    store.clear()
    yield
    store.clear()


@pytest.fixture
def client() -> TestClient:
    """A fake HTTP client that talks to the app directly, no network."""
    return TestClient(app)


@pytest.fixture
def sample_asset() -> dict:
    """One valid asset payload, for tests that just need something to exist."""
    return {
        "hostname": "web-01.corp.local",
        "ip_address": "10.0.0.5",
        "asset_type": "server",
        "criticality": "high",
        "owner": "platform-team",
        "operating_system": "Ubuntu 22.04",
        "tags": ["prod", "dmz"],
    }
