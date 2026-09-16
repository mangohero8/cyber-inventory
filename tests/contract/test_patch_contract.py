"""PATCH semantics: enums, validation parity with create, null handling.

Covers F-11 (enums), F-06 (PATCH breaks create invariants), F-12 (unknown
fields), F-16 (blank owner) and F-17 (null semantics).

Approved null contract: `null` clears a nullable field (`operating_system`);
required / non-nullable fields reject `null` with 422.
"""

from __future__ import annotations

import pytest

from tests.support import (
    API,
    ASSET_KEYS,
    ASSET_TYPES,
    CRITICALITIES,
    INVALID_ENUM_VALUES,
    asset,
    known_defect,
    validation_error,
)


@pytest.fixture
def created(client) -> dict:
    response = client.post(API, json=asset(operating_system="Ubuntu 22.04"))
    if response.status_code != 201:
        pytest.fail(f"precondition: create returned {response.status_code}")
    return response.json()


def url_for(record: dict) -> str:
    return f"{API}/{record['id']}"


# --------------------------------------------------------------------------
# Currently correct -- permanent regression tests
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("asset_type", v) for v in ASSET_TYPES]
    + [("criticality", v) for v in CRITICALITIES],
)
def test_every_valid_enum_value_is_accepted_on_patch(client, created, field, value):
    """F-11"""
    response = client.patch(url_for(created), json={field: value})

    assert response.status_code == 200
    assert response.json()[field] == value


@pytest.mark.parametrize(("field", "value"), INVALID_ENUM_VALUES)
def test_invalid_enum_on_patch_is_422_and_the_asset_is_unchanged(
    client, created, field, value
):
    """F-11"""
    response = client.patch(url_for(created), json={field: value})

    error = validation_error(response, field)
    assert response.status_code == 422
    assert error is not None and error["type"] == "enum"
    assert client.get(url_for(created)).json() == created


def test_ip_address_is_normalized_on_patch(client, created):
    response = client.patch(
        url_for(created), json={"ip_address": " 2001:0db8::0001 "}
    )

    assert response.status_code == 200
    assert response.json()["ip_address"] == "2001:db8::1"


@pytest.mark.parametrize("bad", ["999.1.1.1", "010.0.0.1", "not-an-ip"])
def test_invalid_ip_address_on_patch_is_422_and_the_asset_is_unchanged(
    client, created, bad
):
    response = client.patch(url_for(created), json={"ip_address": bad})

    assert response.status_code == 422
    assert validation_error(response, "ip_address") is not None
    assert client.get(url_for(created)).json() == created


def test_patch_that_omits_a_nullable_field_leaves_it_alone(client, created):
    """Omission is not null: only an explicit null may clear a field."""
    updated = client.patch(url_for(created), json={"owner": "security-team"}).json()

    assert updated["operating_system"] == "Ubuntu 22.04"


def test_patch_already_normalized_tags_are_stored_as_sent(client, created):
    """The normalized case works today; F-06 below covers the rest."""
    updated = client.patch(url_for(created), json={"tags": ["edge", "prod"]}).json()

    assert updated["tags"] == ["edge", "prod"]


def test_characterize_empty_patch_is_accepted_and_refreshes_last_seen(
    client, created
):
    """CRUD-10: no approved decision on `{}`; pins current behaviour."""
    response = client.patch(url_for(created), json={})

    assert response.status_code == 200
    body = response.json()
    unchanged = ASSET_KEYS - {"last_seen"}
    assert {k: body[k] for k in unchanged} == {k: created[k] for k in unchanged}


# --------------------------------------------------------------------------
# Confirmed defects -- strict xfails carrying their finding IDs
# --------------------------------------------------------------------------


@known_defect(
    "F-17",
    "store.update dumps the patch with exclude_none=True, so an explicit "
    "null is ignored instead of clearing operating_system",
)
def test_patch_null_clears_operating_system(client, created):
    response = client.patch(url_for(created), json={"operating_system": None})

    assert response.status_code == 200
    assert response.json()["operating_system"] is None
    assert client.get(url_for(created)).json()["operating_system"] is None


@known_defect(
    "F-17",
    "AssetUpdate declares every field Optional, so null for a required field "
    "is accepted with 200 and silently ignored",
)
@pytest.mark.parametrize(
    "field", ["ip_address", "asset_type", "criticality", "owner", "tags"]
)
def test_patch_null_for_a_non_nullable_field_is_422(client, created, field):
    response = client.patch(url_for(created), json={field: None})

    assert response.status_code == 422


@known_defect(
    "F-06",
    "AssetUpdate has no tag validator; PATCH stores tags verbatim while "
    "create lowercases, trims, de-duplicates and drops empty tags",
)
def test_patch_tags_are_normalized_like_create(client, created):
    response = client.patch(
        url_for(created), json={"tags": ["PROD", "prod", " Web ", ""]}
    )

    assert response.status_code == 200
    assert response.json()["tags"] == ["prod", "web"]


@known_defect(
    "F-06",
    "un-normalized PATCH tags are invisible to the case-insensitive tag "
    "filter, so the asset cannot be found by its own tag",
)
def test_patched_tags_are_findable_with_the_tag_filter(client, created):
    client.patch(url_for(created), json={"tags": [" Web "]})

    found = client.get(API, params={"tag": "web"}).json()

    assert [item["id"] for item in found] == [created["id"]]


@known_defect(
    "F-06",
    "AssetUpdate.owner is a plain str: create requires a non-empty owner, "
    "PATCH accepts ''",
)
def test_patch_owner_empty_string_is_422(client, created):
    assert client.patch(url_for(created), json={"owner": ""}).status_code == 422


@known_defect(
    "F-16",
    "blank owner is accepted on PATCH as on create (no strip before the "
    "non-empty check); also an F-06 parity gap",
)
def test_patch_owner_whitespace_only_is_422(client, created):
    assert client.patch(url_for(created), json={"owner": "   "}).status_code == 422


@known_defect(
    "F-12",
    "AssetUpdate ignores unknown fields, so hostname/id/environment in a "
    "PATCH return 200 and are dropped. Approved contract: reject with 422",
)
@pytest.mark.parametrize(
    "extra",
    [
        {"hostname": "renamed-01"},
        {"id": "other-id"},
        {"environment": "production"},
        {"first_seen": "2000-01-01T00:00:00Z"},
    ],
    ids=["hostname", "id", "environment", "first_seen"],
)
def test_unknown_or_immutable_fields_on_patch_are_422(client, created, extra):
    assert client.patch(url_for(created), json=extra).status_code == 422
