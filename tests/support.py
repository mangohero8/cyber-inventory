"""Shared helpers for the functional-service contract tests.

Finding IDs (F-xx) and matrix IDs (CRUD-01, LST-05, ...) refer to
docs/FUNCTIONAL_SERVICE_AUDIT.md.
"""

from __future__ import annotations

from typing import Any

import pytest

API = "/api/v1/assets"
STATS = "/api/v1/stats"

ASSET_KEYS = frozenset(
    {
        "hostname",
        "ip_address",
        "asset_type",
        "criticality",
        "owner",
        "operating_system",
        "tags",
        "id",
        "first_seen",
        "last_seen",
    }
)

ASSET_TYPES = (
    "server",
    "workstation",
    "network_device",
    "container",
    "cloud_resource",
)
CRITICALITIES = ("low", "medium", "high", "critical")

# Not members of the schema enums. Enums are strict and case-sensitive
# (approved decision), so a wrong-case spelling is as invalid as a typo.
INVALID_ENUM_VALUES = [
    ("asset_type", "database"),
    ("asset_type", "SERVER"),
    ("asset_type", ""),
    ("criticality", "bogus"),
    ("criticality", "HIGH"),
    ("criticality", ""),
]


def known_defect(finding: str, reason: str) -> pytest.MarkDecorator:
    """Mark a test that encodes the approved contract for a confirmed defect.

    strict=True: once the defect is fixed the test XPASSes and the run FAILS,
    so the marker must be removed deliberately. A fixed defect cannot hide,
    and neither can a regression back into it.

    raises=AssertionError: only a failed contract assertion counts as the
    expected failure. If the test itself crashes (a typo, a changed fixture)
    that is reported as a real failure instead of being absorbed as "expected".
    """
    return pytest.mark.xfail(
        strict=True, raises=AssertionError, reason=f"{finding}: {reason}"
    )


def asset(hostname: str = "web-01.corp.local", **overrides: Any) -> dict[str, Any]:
    """A valid create payload. Override any field by keyword."""
    body: dict[str, Any] = {
        "hostname": hostname,
        "ip_address": "10.0.0.5",
        "asset_type": "server",
        "criticality": "high",
        "owner": "platform-team",
        "operating_system": "Ubuntu 22.04",
        "tags": ["prod", "dmz"],
    }
    body.update(overrides)
    return body


def validation_error(response, field: str) -> dict[str, Any] | None:
    """The 422 error entry naming `field`, or None.

    `field` is matched anywhere in `loc`, not just at the end: an error inside
    a list reports ["body", "tags", 0], where the index is last.
    """
    if response.status_code != 422:
        return None
    for error in response.json().get("detail", []):
        if field in error.get("loc", []):
            return error
    return None
