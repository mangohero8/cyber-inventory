"""List, filter, sort and pagination behaviour.

Covers LST-01..10 and F-05. Approved decision: `criticality` and `asset_type`
filters are strict and case-sensitive, consistent with the Asset schema, and
invalid values return 422. The `tag` filter stays case-insensitive.
"""

from __future__ import annotations

import pytest

from tests.support import API, asset, known_defect, validation_error

INVENTORY = [
    # hostname       asset_type        criticality  tags
    ("app-01", "server", "critical", ["prod", "pci"]),
    ("db-01", "server", "high", ["prod"]),
    ("laptop-01", "workstation", "low", ["corp"]),
    ("edge-01", "network_device", "high", ["prod", "edge"]),
    ("job-01", "container", "medium", ["batch"]),
    ("bucket-01", "cloud_resource", "medium", ["prod"]),
]
ALL_HOSTS = sorted(host for host, *_ in INVENTORY)
PROD_HOSTS = ["app-01", "bucket-01", "db-01", "edge-01"]


@pytest.fixture
def inventory(client) -> dict[str, dict]:
    records = {}
    for hostname, asset_type, criticality, tags in INVENTORY:
        response = client.post(
            API,
            json=asset(
                hostname=hostname,
                asset_type=asset_type,
                criticality=criticality,
                tags=tags,
            ),
        )
        if response.status_code != 201:
            pytest.fail(f"precondition: seeding {hostname} -> {response.status_code}")
        records[hostname] = response.json()
    return records


def hostnames(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["hostname"] for item in response.json()]


# --------------------------------------------------------------------------
# Currently correct -- permanent regression tests
# --------------------------------------------------------------------------


def test_list_on_an_empty_inventory_is_an_empty_array(client):
    response = client.get(API)

    assert response.status_code == 200
    assert response.json() == []


def test_unfiltered_list_returns_everything_sorted_by_hostname(client, inventory):
    assert hostnames(client.get(API)) == ALL_HOSTS


@pytest.mark.parametrize(
    ("criticality", "expected"),
    [
        ("critical", ["app-01"]),
        ("high", ["db-01", "edge-01"]),
        ("medium", ["bucket-01", "job-01"]),
        ("low", ["laptop-01"]),
    ],
)
def test_criticality_filter_for_each_valid_value(
    client, inventory, criticality, expected
):
    response = client.get(API, params={"criticality": criticality})

    assert hostnames(response) == expected


@pytest.mark.parametrize(
    ("asset_type", "expected"),
    [
        ("server", ["app-01", "db-01"]),
        ("workstation", ["laptop-01"]),
        ("network_device", ["edge-01"]),
        ("container", ["job-01"]),
        ("cloud_resource", ["bucket-01"]),
    ],
)
def test_asset_type_filter_for_each_valid_value(
    client, inventory, asset_type, expected
):
    response = client.get(API, params={"asset_type": asset_type})

    assert hostnames(response) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"criticality": "high", "asset_type": "server"}, ["db-01"]),
        ({"asset_type": "server", "tag": "pci"}, ["app-01"]),
        ({"criticality": "high", "tag": "edge"}, ["edge-01"]),
        (
            {"criticality": "high", "asset_type": "server", "tag": "prod"},
            ["db-01"],
        ),
        ({"criticality": "medium", "tag": "prod"}, ["bucket-01"]),
        ({"criticality": "low", "asset_type": "server"}, []),
    ],
    ids=[
        "crit+type",
        "type+tag",
        "crit+tag",
        "crit+type+tag",
        "crit+tag-2",
        "no-intersection",
    ],
)
def test_combined_filters_are_an_intersection(client, inventory, params, expected):
    """LST-07: every supplied filter must match (AND, not OR)."""
    assert hostnames(client.get(API, params=params)) == expected


@pytest.mark.parametrize("tag", ["prod", "PROD", "  Prod  "])
def test_tag_filter_is_case_and_whitespace_insensitive(client, inventory, tag):
    assert hostnames(client.get(API, params={"tag": tag})) == PROD_HOSTS


def test_valid_filter_with_no_matches_is_an_empty_array(client, inventory):
    assert hostnames(client.get(API, params={"tag": "no-such-tag"})) == []


def test_filtered_items_are_full_representations(client, inventory):
    listed = client.get(API, params={"asset_type": "workstation"}).json()

    assert listed == [inventory["laptop-01"]]


def test_characterize_empty_filter_value_means_no_filter(client, inventory):
    """LST-08: pins current behaviour.

    Likely to change with the F-05 fix: a strict enum query parameter rejects
    "" with 422. Update this test deliberately in that change.
    """
    assert hostnames(client.get(API, params={"criticality": ""})) == ALL_HOSTS


def test_characterize_sort_and_pagination_parameters_are_ignored(client, inventory):
    """LST-09/10: not implemented; unknown query parameters are ignored.

    Pinned so no client can believe sort or paging works when it does not.
    """
    params = {"sort": "-hostname", "limit": 1, "offset": 2, "page": 3}

    assert hostnames(client.get(API, params=params)) == ALL_HOSTS


# --------------------------------------------------------------------------
# Confirmed defect -- strict xfail carrying its finding ID
# --------------------------------------------------------------------------


@known_defect(
    "F-05",
    "list filters are typed `str | None` (main.py:215-216), not the enums, "
    "so invalid values return 200 [] -- a silent false negative",
)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("criticality", "bogus"),
        ("criticality", "HIGH"),
        ("criticality", "Critical"),
        ("asset_type", "bogus"),
        ("asset_type", "SERVER"),
        ("asset_type", "database"),
    ],
)
def test_invalid_enum_filter_is_422(client, inventory, field, value):
    response = client.get(API, params={field: value})

    assert response.status_code == 422
    error = validation_error(response, field)
    assert error is not None and error["type"] == "enum"
    assert error["loc"] == ["query", field]
