"""Tests for build provenance -- the commit-to-production trace.

These are the highest-value tests in the repo. If provenance silently
breaks, every other debugging session gets harder, and nothing else would
notice.
"""

from __future__ import annotations

from app.version import UNKNOWN, get_build_info


def test_version_reports_injected_commit(monkeypatch, client):
    """The commit stamped at build time shows up at /version."""
    monkeypatch.setenv("GIT_COMMIT", "a1b2c3d4e5f6a7b8c9d0")
    monkeypatch.setenv("BUILD_TIME", "2026-08-15T12:00:00Z")
    monkeypatch.setenv("ENVIRONMENT", "production")

    body = client.get("/version").json()

    assert body["commit"] == "a1b2c3d4e5f6a7b8c9d0"
    assert body["commit_short"] == "a1b2c3d"
    assert body["environment"] == "production"


def test_version_is_unknown_when_not_injected(monkeypatch, client):
    """No provenance means 'unknown', never a crash and never a fake value.

    'unknown' is the signal that CI did not build this image. A deploy
    smoke test checks for exactly this.
    """
    monkeypatch.delenv("GIT_COMMIT", raising=False)

    body = client.get("/version").json()

    assert body["commit"] == UNKNOWN
    assert body["commit_short"] == UNKNOWN


def test_empty_env_var_counts_as_missing(monkeypatch):
    """An empty string is treated as absent, not as a valid commit.

    CI systems set variables to "" surprisingly often. Without this,
    /version would report an empty commit and look successful.
    """
    monkeypatch.setenv("GIT_COMMIT", "   ")

    assert get_build_info().commit == UNKNOWN
    assert get_build_info().is_traceable is False


def test_is_traceable_flag(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "deadbeef")
    assert get_build_info().is_traceable is True


def test_every_response_carries_the_commit(monkeypatch, client):
    """The commit rides on every response header, not just /version.

    This means anyone with curl can identify the running build from any
    endpoint they happen to be hitting.
    """
    monkeypatch.setenv("GIT_COMMIT", "abcdef1234567890")

    response = client.get("/healthz")

    assert response.headers["X-Build-Commit"] == "abcdef1"


def test_request_id_is_returned(client):
    """Every response gets a request id, so a user report maps to a log line."""
    response = client.get("/healthz")
    assert response.headers.get("X-Request-ID")


def test_trace_header_is_reused_when_present(client):
    """Cloud Run sends a trace id; we adopt it instead of inventing our own.

    Reusing it makes our application logs line up with the platform's own
    request logs. Inventing a second id means two parallel sets of logs
    that can never be correlated.
    """
    response = client.get(
        "/healthz",
        headers={"X-Cloud-Trace-Context": "abc123def456/9999;o=1"},
    )
    assert response.headers["X-Request-ID"] == "abc123def456"
