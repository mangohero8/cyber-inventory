"""F-07 -- the core CRUD contract, pinned as it behaves today.

Every test in this file passes against the current implementation. They are
permanent regression tests: a database migration, a refactor or a defect fix
must not silently change what a client observes. Matrix IDs (CRUD-xx, VAL-xx)
refer to docs/FUNCTIONAL_SERVICE_AUDIT.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from tests.support import API, ASSET_KEYS, STATS, asset


def utc(value: str) -> datetime:
    """Parse an API timestamp, insisting it is serialized as UTC."""
    assert value.endswith("Z"), f"timestamp is not UTC-suffixed: {value!r}"
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def test_create_returns_the_complete_normalized_representation(client):
    """CRUD-01: every field comes back normalized, plus server-owned fields."""
    response = client.post(
        API,
        json=asset(
            hostname="  Web-01.Corp.Local ",
            ip_address="2001:0db8:0000:0000:0000:0000:0000:0001",
            tags=["Prod", "prod", " DMZ "],
        ),
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == ASSET_KEYS
    assert body["hostname"] == "web-01.corp.local"
    assert body["ip_address"] == "2001:db8::1"
    assert body["asset_type"] == "server"
    assert body["criticality"] == "high"
    assert body["owner"] == "platform-team"
    assert body["operating_system"] == "Ubuntu 22.04"
    assert body["tags"] == ["prod", "dmz"]
    assert uuid.UUID(body["id"]).version == 4
    assert utc(body["first_seen"]) <= utc(body["last_seen"])


def test_create_applies_defaults_for_optional_fields(client):
    """CRUD-01: omitted optional fields get their documented defaults."""
    minimal = {
        "hostname": "min-01",
        "ip_address": "10.0.0.1",
        "asset_type": "container",
        "owner": "team",
    }

    body = client.post(API, json=minimal).json()

    assert body["criticality"] == "medium"
    assert body["operating_system"] is None
    assert body["tags"] == []


def test_get_after_create_returns_the_identical_representation(client):
    """CRUD-03: read-after-write on a single instance."""
    created = client.post(API, json=asset()).json()

    response = client.get(f"{API}/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_list_items_match_their_get_representation(client):
    """CRUD-04: list and GET never disagree about an asset."""
    ids = [
        client.post(API, json=asset(hostname=host)).json()["id"]
        for host in ("beta-01", "alpha-01")
    ]

    listed = {item["id"]: item for item in client.get(API).json()}

    assert set(listed) == set(ids)
    for asset_id in ids:
        assert listed[asset_id] == client.get(f"{API}/{asset_id}").json()


PATCHABLE = [
    ("ip_address", "192.168.1.20"),
    ("asset_type", "workstation"),
    ("criticality", "critical"),
    ("owner", "security-team"),
    ("operating_system", "Debian 12"),
    ("tags", ["edge"]),
]


@pytest.mark.parametrize(
    ("field", "value"), PATCHABLE, ids=[field for field, _ in PATCHABLE]
)
def test_patch_changes_only_the_supplied_field_and_is_stored(client, field, value):
    """CRUD-05: one field changes; everything but last_seen is untouched."""
    created = client.post(API, json=asset()).json()

    response = client.patch(f"{API}/{created['id']}", json={field: value})

    assert response.status_code == 200
    updated = response.json()
    assert updated[field] == value
    untouched = ASSET_KEYS - {field, "last_seen"}
    assert {k: updated[k] for k in untouched} == {k: created[k] for k in untouched}
    assert client.get(f"{API}/{created['id']}").json() == updated


def test_patch_advances_last_seen_and_preserves_identity(client):
    """CRUD-06: id, hostname and first_seen are immutable through PATCH."""
    created = client.post(API, json=asset()).json()

    updated = client.patch(
        f"{API}/{created['id']}", json={"owner": "security-team"}
    ).json()

    assert updated["id"] == created["id"]
    assert updated["hostname"] == created["hostname"]
    assert updated["first_seen"] == created["first_seen"]
    assert utc(updated["last_seen"]) >= utc(created["last_seen"])


def test_patch_never_changes_immutable_fields(client):
    """CRUD-09: hostname and id cannot be changed by PATCH.

    Holds today (the fields are ignored) and after F-12 is fixed (the request
    is rejected), so it is a permanent guard either way.
    """
    created = client.post(API, json=asset()).json()

    client.patch(
        f"{API}/{created['id']}",
        json={"hostname": "renamed-01", "id": "hijacked", "owner": "x-team"},
    )

    stored = client.get(f"{API}/{created['id']}").json()
    assert stored["hostname"] == created["hostname"]
    assert stored["id"] == created["id"]
    assert client.get(f"{API}/hijacked").status_code == 404


@pytest.mark.parametrize(
    "variant",
    ["web-01.corp.local", " WEB-01.CORP.LOCAL ", "Web-01.Corp.Local"],
    ids=["exact", "upper-padded", "mixed-case"],
)
def test_duplicate_hostname_conflicts_and_leaves_the_original_untouched(
    client, variant
):
    """CRUD-13: 409 for any case/whitespace variant, with no side effects."""
    original = client.post(API, json=asset()).json()

    response = client.post(
        API, json=asset(hostname=variant, ip_address="10.9.9.9", owner="intruder")
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "asset with hostname 'web-01.corp.local' already exists"
    }
    assert client.get(f"{API}/{original['id']}").json() == original
    assert len(client.get(API).json()) == 1


def test_delete_returns_empty_204_and_the_asset_is_gone_everywhere(client):
    """CRUD-11: GET, PATCH, DELETE all 404 afterwards; list and stats agree."""
    keep = client.post(API, json=asset(hostname="keep-01")).json()
    doomed = client.post(API, json=asset(hostname="doomed-01")).json()
    url = f"{API}/{doomed['id']}"

    response = client.delete(url)

    assert response.status_code == 204
    assert response.content == b""
    assert client.get(url).status_code == 404
    assert client.patch(url, json={"owner": "x"}).status_code == 404
    assert client.delete(url).status_code == 404
    assert [item["id"] for item in client.get(API).json()] == [keep["id"]]
    assert client.get(STATS).json()["total"] == 1


def test_hostname_reuse_after_delete_issues_a_new_id(client):
    """CRUD-12: the uniqueness index is released and identity is not reused."""
    first = client.post(API, json=asset()).json()
    client.delete(f"{API}/{first['id']}")

    second = client.post(API, json=asset())

    assert second.status_code == 201
    assert second.json()["id"] != first["id"]


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_missing_asset_is_404_with_a_stable_json_body(client, method):
    """VAL-13: the same 404 contract on every per-asset route."""
    url = f"{API}/{uuid.uuid4()}"
    kwargs = {"json": {"owner": "x"}} if method == "patch" else {}

    response = getattr(client, method)(url, **kwargs)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "asset not found"}


def test_stats_on_an_empty_inventory(client):
    """CRUD-14: the empty shape is explicit, not an error or a missing key."""
    assert client.get(STATS).json() == {
        "total": 0,
        "by_criticality": {},
        "by_type": {},
    }


def test_stats_track_create_patch_and_delete(client):
    """CRUD-14: aggregates move with every mutation; empty buckets vanish."""

    def stats():
        return client.get(STATS).json()

    web = client.post(
        API, json=asset(hostname="web-01", criticality="high", asset_type="server")
    ).json()
    client.post(
        API,
        json=asset(hostname="ws-01", criticality="low", asset_type="workstation"),
    )
    assert stats() == {
        "total": 2,
        "by_criticality": {"high": 1, "low": 1},
        "by_type": {"server": 1, "workstation": 1},
    }

    client.patch(
        f"{API}/{web['id']}", json={"criticality": "low", "asset_type": "container"}
    )
    assert stats() == {
        "total": 2,
        "by_criticality": {"low": 2},
        "by_type": {"container": 1, "workstation": 1},
    }

    client.delete(f"{API}/{web['id']}")
    assert stats() == {
        "total": 1,
        "by_criticality": {"low": 1},
        "by_type": {"workstation": 1},
    }


def test_complete_crud_lifecycle(client):
    """CRUD-15 (in-process): create, read, list, filter, patch, delete."""
    created = client.post(API, json=asset(hostname="life-01", tags=["tier-1"]))
    assert created.status_code == 201
    body = created.json()
    url = f"{API}/{body['id']}"

    assert client.get(url).json() == body
    assert client.get(API, params={"tag": "tier-1"}).json() == [body]

    patched = client.patch(url, json={"criticality": "critical", "tags": ["tier-0"]})
    assert patched.status_code == 200
    assert patched.json()["criticality"] == "critical"
    assert client.get(url).json() == patched.json()
    assert client.get(API, params={"criticality": "critical"}).json() == [
        patched.json()
    ]
    assert client.get(API, params={"tag": "tier-1"}).json() == []

    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    assert client.get(API).json() == []
    assert client.get(STATS).json()["total"] == 0
