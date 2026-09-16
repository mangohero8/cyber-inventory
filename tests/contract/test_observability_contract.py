"""Logging, correlation and build provenance.

Covers F-15 (startup/shutdown logging, never exercised before because the
shared client does not run the lifespan), F-08 (no audit events for update or
delete) and F-14 (500 responses lose their correlation headers).
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import JsonLogFormatter, app
from tests.support import API, asset, known_defect

COMMIT = "0123456789abcdef0123456789abcdef01234567"
PROVENANCE_KEYS = {
    "service",
    "version",
    "commit",
    "commit_short",
    "build_time",
    "environment",
}


@pytest.fixture
def provenance(monkeypatch):
    """A fully stamped build, as CI produces."""
    monkeypatch.setenv("GIT_COMMIT", COMMIT)
    monkeypatch.setenv("BUILD_TIME", "2026-09-15T12:00:00Z")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.delenv("SERVICE_NAME", raising=False)


def rendered(record) -> dict:
    """The log line exactly as it reaches stdout, parsed back."""
    line = JsonLogFormatter().format(record)
    assert "\n" not in line, "a log record must be a single line"
    return json.loads(line)


# --------------------------------------------------------------------------
# Startup and shutdown (F-15)
# --------------------------------------------------------------------------


def test_startup_logs_the_build_provenance(provenance, app_events):
    with TestClient(app):
        pass

    events = app_events(event="startup")
    assert len(events) == 1
    start = events[0]
    assert start["level"] == "INFO"
    assert start["message"] == "service starting"
    assert start["commit"] == COMMIT
    assert start["commit_short"] == COMMIT[:7]
    assert start["build_time"] == "2026-09-15T12:00:00Z"
    assert start["environment"] == "test"
    assert start["service"] == "cyber-inventory"


def test_startup_warns_when_provenance_is_missing(monkeypatch, app_events):
    """The signal that an image was not built by CI."""
    monkeypatch.delenv("GIT_COMMIT", raising=False)

    with TestClient(app):
        pass

    events = app_events(event="provenance_missing")
    assert len(events) == 1
    assert events[0]["level"] == "WARNING"


def test_startup_does_not_warn_when_provenance_is_present(provenance, app_events):
    with TestClient(app):
        pass

    assert app_events(event="provenance_missing") == []


def test_shutdown_is_logged_after_startup(provenance, app_events):
    with TestClient(app):
        assert app_events(event="shutdown") == []

    lifecycle = [
        item["event"]
        for item in app_events()
        if item.get("event") in {"startup", "shutdown"}
    ]
    assert lifecycle == ["startup", "shutdown"]


def test_startup_log_and_version_endpoint_agree(provenance, app_events):
    with TestClient(app) as started:
        version = started.get("/version").json()

    start = app_events(event="startup")[0]
    assert {key: start[key] for key in version} == version


# --------------------------------------------------------------------------
# Request logging (OBS-01, OBS-08)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("get", "/health", None, 200),
        ("post", API, asset(), 201),
        ("get", f"{API}/{uuid.uuid4()}", None, 404),
        ("post", API, {"hostname": "orphan"}, 422),
    ],
    ids=["health-200", "create-201", "missing-404", "invalid-422"],
)
def test_one_structured_request_log_per_request(
    client, app_events, method, path, body, status
):
    kwargs = {"json": body} if body is not None else {}

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == status
    lines = app_events(message="request handled")
    assert len(lines) == 1
    line = lines[0]
    assert line["level"] == "INFO"
    assert line["method"] == method.upper()
    assert line["path"] == path
    assert line["status"] == status
    assert isinstance(line["duration_ms"], float)
    assert line["request_id"] == response.headers["X-Request-ID"]


def test_log_lines_are_single_line_json_with_cloud_logging_severity(
    client, app_events
):
    client.get("/health")

    payload = rendered(app_events(message="request handled")[0]["record"])

    assert payload["severity"] == "INFO"
    assert payload["logger"] == "cyber-inventory"
    assert {"message", "timestamp", "request_id", "method", "path", "status"} <= set(
        payload
    )


def test_request_bodies_and_query_strings_are_not_logged(client, caplog):
    """OBS-08: no inventory content leaks into the log stream."""
    caplog.set_level(logging.INFO)
    secret_owner = f"owner-{uuid.uuid4().hex}"
    secret_tag = f"tag-{uuid.uuid4().hex}"

    client.post(API, json=asset(owner=secret_owner))
    client.get(API, params={"tag": secret_tag})

    stream = "\n".join(
        JsonLogFormatter().format(record)
        for record in caplog.records
        if record.name == "cyber-inventory"
    )
    assert secret_owner not in stream
    assert secret_tag not in stream


def test_trace_context_is_adopted_as_the_request_id(client, app_events):
    """Cloud Run's trace id is reused so app logs line up with platform logs."""
    response = client.get(
        "/health", headers={"X-Cloud-Trace-Context": "abc123def456/9999;o=1"}
    )

    assert response.headers["X-Request-ID"] == "abc123def456"
    assert app_events(message="request handled")[0]["request_id"] == "abc123def456"


# --------------------------------------------------------------------------
# Unhandled errors (RUN-01)
# --------------------------------------------------------------------------


def test_unhandled_exception_is_logged_as_an_error_with_a_traceback(
    lenient_client, failing_list, app_events
):
    response = lenient_client.get(API)

    assert response.status_code == 500
    errors = app_events(message="unhandled exception")
    assert len(errors) == 1
    assert errors[0]["level"] == "ERROR"
    assert errors[0]["path"] == API
    assert errors[0]["request_id"]
    payload = rendered(errors[0]["record"])
    assert payload["severity"] == "ERROR"
    assert "simulated store failure" in payload["exception"]


# --------------------------------------------------------------------------
# Provenance on every response (OBS-02, OBS-03)
# --------------------------------------------------------------------------


def test_version_exposes_exactly_the_provenance_fields(client, provenance):
    body = client.get("/version").json()

    assert set(body) == PROVENANCE_KEYS
    assert body == {
        "service": "cyber-inventory",
        "version": "0.1.0",
        "commit": COMMIT,
        "commit_short": COMMIT[:7],
        "build_time": "2026-09-15T12:00:00Z",
        "environment": "test",
    }


def test_provenance_is_unknown_rather_than_faked_when_absent(client, monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("BUILD_TIME", raising=False)

    body = client.get("/version").json()

    assert body["commit"] == "unknown"
    assert body["commit_short"] == "unknown"
    assert body["build_time"] == "unknown"
    assert client.get("/health").headers["X-Build-Commit"] == "unknown"


def test_every_non_500_response_carries_the_correlation_headers(client, provenance):
    """OBS-02/03: any response a client can get identifies the build."""
    created = client.post(API, json=asset())
    asset_id = created.json()["id"]
    responses = {
        "201-create": created,
        "200-get": client.get(f"{API}/{asset_id}"),
        "409-duplicate": client.post(API, json=asset()),
        "422-invalid": client.post(API, json={"hostname": "orphan"}),
        "404-missing": client.get(f"{API}/{uuid.uuid4()}"),
        "405-method": client.put(API),
        "204-delete": client.delete(f"{API}/{asset_id}"),
    }

    for label, response in responses.items():
        assert response.headers.get("X-Request-ID"), label
        assert response.headers.get("X-Build-Commit") == COMMIT[:7], label


# --------------------------------------------------------------------------
# Confirmed defects -- strict xfails carrying their finding IDs
# --------------------------------------------------------------------------


@known_defect(
    "F-08",
    "PATCH emits no domain audit event, so an update leaves only a generic "
    "request log line -- and in an in-memory service the log is the only "
    "durable record",
)
def test_update_emits_an_asset_updated_event(client, app_events):
    created = client.post(API, json=asset()).json()

    client.patch(f"{API}/{created['id']}", json={"owner": "security-team"})

    events = app_events(event="asset_updated")
    assert len(events) == 1
    assert events[0]["asset_id"] == created["id"]


@known_defect(
    "F-08",
    "DELETE emits no domain audit event, so deletions are unaudited: nothing "
    "records which asset was removed, or that it ever existed",
)
def test_delete_emits_an_asset_deleted_event(client, app_events):
    created = client.post(API, json=asset()).json()

    client.delete(f"{API}/{created['id']}")

    events = app_events(event="asset_deleted")
    assert len(events) == 1
    assert events[0]["asset_id"] == created["id"]


def test_create_emits_an_asset_created_event(client, app_events):
    """The one audit event that does exist today."""
    created = client.post(API, json=asset()).json()

    events = app_events(event="asset_created")

    assert len(events) == 1
    assert events[0]["asset_id"] == created["id"]
    assert events[0]["hostname"] == created["hostname"]


@known_defect(
    "F-14",
    "the request middleware re-raises before it sets any header, so a 500 "
    "carries no X-Request-ID -- correlation is lost exactly when it matters",
)
def test_500_response_carries_a_request_id(lenient_client, failing_list):
    response = lenient_client.get(API)
    if response.status_code != 500:
        pytest.fail(f"precondition: expected a 500, got {response.status_code}")

    assert response.headers.get("X-Request-ID")


@known_defect(
    "F-14",
    "a 500 also carries no X-Build-Commit, so an error response cannot be "
    "attributed to a build",
)
def test_500_response_carries_the_build_commit(lenient_client, failing_list):
    response = lenient_client.get(API)
    if response.status_code != 500:
        pytest.fail(f"precondition: expected a 500, got {response.status_code}")

    assert response.headers.get("X-Build-Commit")
