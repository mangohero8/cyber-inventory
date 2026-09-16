"""Create-request validation and HTTP error behaviour.

Covers F-11 (enums), F-04 (whitespace hostname 500), F-12 (unknown fields) and
F-16 (whitespace owner). Unmarked tests pin behaviour that is already correct.
Tests marked `known_defect` encode the approved contract for a confirmed
defect; they are strict xfails and must fail today.
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.support import (
    API,
    ASSET_TYPES,
    CRITICALITIES,
    INVALID_ENUM_VALUES,
    asset,
    known_defect,
    validation_error,
)

# --------------------------------------------------------------------------
# Currently correct -- permanent regression tests
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["hostname", "ip_address", "asset_type", "owner"])
def test_missing_required_field_is_422_naming_the_field(client, field):
    payload = asset()
    del payload[field]

    response = client.post(API, json=payload)

    error = validation_error(response, field)
    assert response.status_code == 422
    assert error is not None and error["type"] == "missing"
    assert error["loc"] == ["body", field]


@pytest.mark.parametrize("value", ASSET_TYPES)
def test_every_valid_asset_type_is_accepted_on_create(client, value):
    """F-11: valid values behave exactly as the schema declares."""
    response = client.post(API, json=asset(asset_type=value))

    assert response.status_code == 201
    assert response.json()["asset_type"] == value


@pytest.mark.parametrize("value", CRITICALITIES)
def test_every_valid_criticality_is_accepted_on_create(client, value):
    """F-11: valid values behave exactly as the schema declares."""
    response = client.post(API, json=asset(criticality=value))

    assert response.status_code == 201
    assert response.json()["criticality"] == value


@pytest.mark.parametrize(("field", "value"), INVALID_ENUM_VALUES)
def test_invalid_enum_on_create_is_422_and_nothing_is_stored(client, field, value):
    """F-11: strict, case-sensitive enums; the error names the field."""
    response = client.post(API, json=asset(**{field: value}))

    error = validation_error(response, field)
    assert response.status_code == 422
    assert error is not None and error["type"] == "enum"
    assert client.get(API).json() == []


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("10.0.0.5", "10.0.0.5"),
        ("  10.0.0.5  ", "10.0.0.5"),
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
        ("::1", "::1"),
    ],
)
def test_ip_address_is_normalized_on_create(client, given, stored):
    response = client.post(API, json=asset(ip_address=given))

    assert response.status_code == 201
    assert response.json()["ip_address"] == stored


@pytest.mark.parametrize(
    "bad", ["999.1.1.1", "010.0.0.1", "10.0.0", "not-an-ip", ""]
)
def test_invalid_ip_address_is_422_on_create(client, bad):
    """Leading-zero IPv4 is rejected (models.py docstring says 'normalized')."""
    response = client.post(API, json=asset(ip_address=bad))

    assert response.status_code == 422
    assert validation_error(response, "ip_address") is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hostname", 12345),
        ("owner", ["team"]),
        ("tags", "prod"),
        ("tags", [1, 2]),
        ("ip_address", 167772161),
    ],
    ids=["int-hostname", "list-owner", "str-tags", "int-tags", "int-ip"],
)
def test_wrong_json_types_are_422(client, field, value):
    response = client.post(API, json=asset(**{field: value}))

    assert response.status_code == 422
    assert validation_error(response, field) is not None


def test_hostname_of_253_characters_is_accepted(client):
    assert client.post(API, json=asset(hostname="a" * 253)).status_code == 201


def test_hostname_longer_than_253_characters_is_422(client):
    response = client.post(API, json=asset(hostname="a" * 254))

    assert response.status_code == 422
    assert validation_error(response, "hostname")["type"] == "string_too_long"


@pytest.mark.parametrize(
    ("content", "content_type", "error_type"),
    [
        (b"{not json", "application/json", "json_invalid"),
        (b"[]", "application/json", "model_attributes_type"),
        (json.dumps(asset()).encode(), "text/plain", "model_attributes_type"),
        (b"", "application/json", "missing"),
    ],
    ids=["malformed-json", "json-array", "text-plain", "empty-body"],
)
def test_malformed_or_non_object_bodies_are_422(
    client, content, content_type, error_type
):
    """VAL-09/10: FastAPI answers 422 (not 400) for unparseable bodies."""
    response = client.post(
        API, content=content, headers={"Content-Type": content_type}
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == error_type


def test_unknown_route_is_a_json_404(client):
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    ("method", "path"),
    [("put", API), ("delete", API), ("post", f"{API}/{uuid.uuid4()}")],
    ids=["put-collection", "delete-collection", "post-item"],
)
def test_unsupported_method_is_a_json_405(client, method, path):
    response = getattr(client, method)(path)

    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}


HOSTILE_BUT_HANDLED = {
    "nul-in-hostname": {"hostname": "web\x00-01"},
    "unicode-hostname": {"hostname": "wéb-ünïcode-01"},
    "emoji-owner": {"owner": "team-🔥"},
    "null-operating-system": {"operating_system": None},
    "huge-operating-system": {"operating_system": "x" * 100_000},
    "null-owner": {"owner": None},
    "null-tags": {"tags": None},
    "object-hostname": {"hostname": {"nested": True}},
    "number-criticality": {"criticality": -1},
    "boolean-asset-type": {"asset_type": True},
    "too-long-hostname": {"hostname": "h" * 10_000},
}


@pytest.mark.parametrize(
    "override", HOSTILE_BUT_HANDLED.values(), ids=HOSTILE_BUT_HANDLED.keys()
)
def test_hostile_create_input_never_produces_a_server_error(lenient_client, override):
    """RUN-02: client input yields 2xx/4xx. Whitespace hostname is F-04 below."""
    response = lenient_client.post(API, json=asset(**override))

    assert response.status_code < 500


def test_characterize_hostname_format_is_not_validated(client):
    """VAL-06: no approved decision yet; pins current behaviour.

    Change this test deliberately if a hostname format rule is adopted.
    """
    assert client.post(API, json=asset(hostname="not a host!!")).status_code == 201


# --------------------------------------------------------------------------
# Confirmed defects -- strict xfails carrying their finding IDs
# --------------------------------------------------------------------------


@known_defect(
    "F-04",
    "whitespace-only hostname passes min_length before it is stripped to '', "
    "then Asset re-validation inside store.create raises -> HTTP 500",
)
@pytest.mark.parametrize(
    "hostname", ["   ", "\t", " \n "], ids=["spaces", "tab", "newline"]
)
def test_whitespace_only_hostname_is_422_not_500(lenient_client, hostname):
    response = lenient_client.post(API, json=asset(hostname=hostname))

    assert response.status_code == 422


@known_defect(
    "F-16",
    "owner is NonEmptyStr without whitespace stripping, so a blank owner "
    "is stored",
)
@pytest.mark.parametrize("owner", ["   ", "\t"], ids=["spaces", "tab"])
def test_whitespace_only_owner_is_422_on_create(client, owner):
    assert client.post(API, json=asset(owner=owner)).status_code == 422


@known_defect(
    "F-12",
    "AssetCreate uses Pydantic's default extra='ignore'; unknown fields are "
    "silently dropped. Approved contract: reject with 422",
)
@pytest.mark.parametrize(
    "extra",
    [
        {"environment": "production"},
        {"foo": 1},
        {"id": "client-chosen-id"},
        {"first_seen": "2000-01-01T00:00:00Z"},
        {"last_seen": "2000-01-01T00:00:00Z"},
    ],
    ids=["environment", "arbitrary", "id", "first_seen", "last_seen"],
)
def test_unknown_fields_on_create_are_422(client, extra):
    """`environment` is deployment metadata (/version), not an Asset property.

    CONTRACT CONFLICT TO RESOLVE WITH THE F-12 FIX: the pre-existing
    tests/test_assets_api.py::test_client_cannot_set_its_own_id expects 201
    when a client sends `id`. Under the approved contract that request is a
    422, so that test must be updated in the same change that fixes F-12.
    """
    response = client.post(API, json={**asset(), **extra})

    assert response.status_code == 422
