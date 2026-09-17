"""Health and readiness semantics on the canonical paths.

Covers F-10 and F-09. The pre-existing tests exercise only /healthz and
/readyz -- the Kubernetes aliases that Cloud Run's frontend reserves and never
forwards. The paths Cloud Run and the deploy workflow actually use are
/health and /ready, so those are pinned here.
"""

from __future__ import annotations

import pytest

from tests.support import known_defect


def test_canonical_health_is_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"process": "ok"}}


def test_canonical_ready_is_ok_and_reports_the_store(client):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"process": "ok", "store": "ok"},
    }


@pytest.mark.parametrize(
    ("canonical", "alias"), [("/health", "/healthz"), ("/ready", "/readyz")]
)
def test_kubernetes_aliases_match_the_canonical_paths(client, canonical, alias):
    first, second = client.get(canonical), client.get(alias)

    assert (first.status_code, first.json()) == (second.status_code, second.json())


def test_liveness_stays_ok_when_the_store_is_broken(client, broken_store):
    """HLT-02 on the canonical path."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_body_reports_the_broken_dependency(client, broken_store):
    """The body is already right; only the status code is wrong (F-09)."""
    assert client.get("/ready").json() == {
        "status": "degraded",
        "checks": {"process": "ok", "store": "error"},
    }


def test_openapi_documents_exactly_the_public_routes(client):
    """HLT-07: all public operations present; aliases stay out of the schema."""
    paths = client.get("/openapi.json").json()["paths"]

    assert {path: set(ops) for path, ops in paths.items()} == {
        "/health": {"get"},
        "/ready": {"get"},
        "/version": {"get"},
        "/api/v1/assets": {"get", "post"},
        "/api/v1/assets/{asset_id}": {"get", "patch", "delete"},
        "/api/v1/stats": {"get"},
    }


def test_interactive_docs_are_served(client):
    response = client.get("/docs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@known_defect(
    "F-09",
    "a failed readiness check returns HTTP 200 with status 'degraded'; "
    "probes, load balancers and `curl -sf` in deploy.yml treat it as ready",
)
@pytest.mark.parametrize("path", ["/ready", "/readyz"])
def test_readiness_returns_503_when_not_ready(client, broken_store, path):
    assert client.get(path).status_code == 503
