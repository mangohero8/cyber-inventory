"""Tests for the asset endpoints.

Each of these exists because a specific realistic bug would slip through
without it. None of them are here to raise a coverage number.
"""

from __future__ import annotations


def test_create_returns_201_and_the_stored_asset(client, sample_asset):
    response = client.post("/api/v1/assets", json=sample_asset)

    assert response.status_code == 201
    body = response.json()
    assert body["hostname"] == "web-01.corp.local"
    assert body["id"]  # server assigns the id, client cannot
    assert body["first_seen"]


def test_client_cannot_set_its_own_id(client, sample_asset):
    """Server-owned fields are ignored if a client sends them.

    Letting a caller choose its own primary key invites collisions and
    overwrites. The create model simply has no `id` field, so extra input
    is dropped.
    """
    response = client.post(
        "/api/v1/assets", json={**sample_asset, "id": "i-picked-this"}
    )

    assert response.status_code == 201
    assert response.json()["id"] != "i-picked-this"


def test_hostname_is_lowercased(client, sample_asset):
    """WEB-01 and web-01 are the same machine.

    Without normalization the inventory double-counts hosts, which is a
    quiet, plausible-looking data-quality bug.
    """
    response = client.post(
        "/api/v1/assets", json={**sample_asset, "hostname": "WEB-01.CORP.LOCAL"}
    )

    assert response.json()["hostname"] == "web-01.corp.local"


def test_duplicate_hostname_is_a_conflict_not_a_bad_request(
    client, sample_asset
):
    """409, not 400.

    The request is perfectly well-formed; it just conflicts with state
    that already exists. Clients retry 409s differently than 400s, so the
    distinction is not cosmetic.
    """
    client.post("/api/v1/assets", json=sample_asset)

    response = client.post(
        "/api/v1/assets", json={**sample_asset, "ip_address": "10.0.0.6"}
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_invalid_ip_is_rejected(client, sample_asset):
    response = client.post(
        "/api/v1/assets", json={**sample_asset, "ip_address": "999.1.1.1"}
    )
    assert response.status_code == 422


def test_ipv6_is_accepted(client, sample_asset):
    """Rejecting IPv6 is a classic oversight in inventory tools."""
    response = client.post(
        "/api/v1/assets",
        json={**sample_asset, "ip_address": "2001:db8::1"},
    )
    assert response.status_code == 201


def test_tags_are_deduplicated_and_lowercased(client, sample_asset):
    response = client.post(
        "/api/v1/assets",
        json={**sample_asset, "tags": ["Prod", "prod", " PROD ", "dmz"]},
    )
    assert response.json()["tags"] == ["prod", "dmz"]


def test_missing_required_field_is_rejected(client):
    response = client.post("/api/v1/assets", json={"hostname": "orphan"})
    assert response.status_code == 422


def test_get_unknown_asset_is_404(client):
    assert client.get("/api/v1/assets/does-not-exist").status_code == 404


def test_list_is_filterable(client, sample_asset):
    client.post("/api/v1/assets", json=sample_asset)
    client.post(
        "/api/v1/assets",
        json={
            **sample_asset,
            "hostname": "db-01",
            "ip_address": "10.0.0.9",
            "criticality": "low",
            "tags": ["staging"],
        },
    )

    assert len(client.get("/api/v1/assets").json()) == 2
    assert len(client.get("/api/v1/assets?criticality=high").json()) == 1
    assert len(client.get("/api/v1/assets?tag=staging").json()) == 1
    assert len(client.get("/api/v1/assets?tag=nonexistent").json()) == 0


def test_list_ordering_is_stable(client, sample_asset):
    """Unordered list endpoints produce tests that fail at random.

    Sorting in the store is cheap insurance against a whole category of
    flaky test.
    """
    for host in ["zeta-01", "alpha-01", "mid-01"]:
        client.post(
            "/api/v1/assets",
            json={**sample_asset, "hostname": host, "ip_address": "10.0.0.1"},
        )

    hostnames = [a["hostname"] for a in client.get("/api/v1/assets").json()]
    assert hostnames == sorted(hostnames)


def test_patch_updates_only_supplied_fields(client, sample_asset):
    """The classic partial-update bug: unsent fields get wiped to null.

    This test is the reason `exclude_unset=True` exists in the store.
    """
    created = client.post("/api/v1/assets", json=sample_asset).json()

    updated = client.patch(
        f"/api/v1/assets/{created['id']}", json={"criticality": "critical"}
    ).json()

    assert updated["criticality"] == "critical"
    assert updated["owner"] == "platform-team"  # untouched
    assert updated["operating_system"] == "Ubuntu 22.04"  # untouched


def test_patch_refreshes_last_seen(client, sample_asset):
    created = client.post("/api/v1/assets", json=sample_asset).json()

    updated = client.patch(
        f"/api/v1/assets/{created['id']}", json={"owner": "security-team"}
    ).json()

    assert updated["last_seen"] >= created["last_seen"]
    assert updated["first_seen"] == created["first_seen"]


def test_patch_unknown_asset_is_404(client):
    response = client.patch("/api/v1/assets/nope", json={"owner": "x"})
    assert response.status_code == 404


def test_delete_then_get_is_404(client, sample_asset):
    created = client.post("/api/v1/assets", json=sample_asset).json()

    assert client.delete(f"/api/v1/assets/{created['id']}").status_code == 204
    assert client.get(f"/api/v1/assets/{created['id']}").status_code == 404


def test_hostname_is_reusable_after_delete(client, sample_asset):
    """Deleting must free the hostname.

    If the uniqueness index is not cleaned up on delete, re-registering a
    rebuilt machine fails with a confusing 409 about an asset that no
    longer exists.
    """
    created = client.post("/api/v1/assets", json=sample_asset).json()
    client.delete(f"/api/v1/assets/{created['id']}")

    assert client.post("/api/v1/assets", json=sample_asset).status_code == 201


def test_delete_unknown_asset_is_404(client):
    assert client.delete("/api/v1/assets/nope").status_code == 404


def test_stats_counts_by_group(client, sample_asset):
    client.post("/api/v1/assets", json=sample_asset)
    client.post(
        "/api/v1/assets",
        json={
            **sample_asset,
            "hostname": "ws-01",
            "asset_type": "workstation",
            "criticality": "low",
        },
    )

    body = client.get("/api/v1/stats").json()

    assert body["total"] == 2
    assert body["by_criticality"] == {"high": 1, "low": 1}
    assert body["by_type"] == {"server": 1, "workstation": 1}
