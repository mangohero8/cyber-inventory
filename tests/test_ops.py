"""Tests for the operational endpoints.

These get tested for the same reason you test anything else: the platform
depends on them, and if they lie, the platform makes bad decisions about
your traffic.
"""

from __future__ import annotations


def test_healthz_is_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_does_not_depend_on_the_store(client, monkeypatch):
    """Liveness must stay green even when a dependency is broken.

    This is the whole reason liveness and readiness are separate. If a
    broken dependency turned liveness red, the platform would restart every
    healthy container during a dependency outage -- escalating a partial
    failure into a total one.
    """
    from app import store as store_module

    def explode():
        raise RuntimeError("store is down")

    monkeypatch.setattr(store_module.AssetStore, "count", lambda self: explode())

    assert client.get("/healthz").status_code == 200


def test_readyz_reports_dependencies(client):
    body = client.get("/readyz").json()
    assert body["status"] == "ok"
    assert body["checks"]["store"] == "ok"


def test_openapi_schema_is_generated(client):
    """Catches route definitions that are individually valid but collide."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/assets" in response.json()["paths"]
