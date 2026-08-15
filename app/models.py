"""Domain models for the cyber asset inventory.

The domain is deliberately small. This project exists to teach a pipeline,
not to model an enterprise CMDB -- so the model is just rich enough to
produce interesting validation failures and meaningful test cases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class Criticality(StrEnum):
    """How much it matters if this asset is compromised."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AssetType(StrEnum):
    SERVER = "server"
    WORKSTATION = "workstation"
    NETWORK_DEVICE = "network_device"
    CONTAINER = "container"
    CLOUD_RESOURCE = "cloud_resource"


NonEmptyStr = Annotated[str, Field(min_length=1, max_length=253)]


class AssetCreate(BaseModel):
    """Request body for registering an asset.

    Separating the create model from the stored model is a small habit with
    a large payoff: clients cannot set server-owned fields like `id` or
    `first_seen`, so you never have to defend against a caller inventing
    its own primary key.
    """

    hostname: NonEmptyStr
    ip_address: str
    asset_type: AssetType
    criticality: Criticality = Criticality.MEDIUM
    owner: NonEmptyStr
    operating_system: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("ip_address")
    @classmethod
    def validate_ip(cls, v: str) -> str:
        """Reject anything that is not a real IPv4/IPv6 address.

        Worth doing properly rather than with a regex: `ip_address()` also
        normalizes forms like '010.0.0.1' that a naive regex would accept
        and that would later break equality comparisons.
        """
        try:
            return str(ip_address(v.strip()))
        except ValueError as exc:
            raise ValueError(f"'{v}' is not a valid IP address") from exc

    @field_validator("hostname")
    @classmethod
    def normalize_hostname(cls, v: str) -> str:
        """Hostnames are case-insensitive; store them lowercased.

        Without this, 'WEB-01' and 'web-01' become two separate assets and
        your inventory quietly double-counts.
        """
        return v.strip().lower()

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for tag in v:
            cleaned = tag.strip().lower()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
        return out


class Asset(AssetCreate):
    """An asset as stored and returned by the service."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    first_seen: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    last_seen: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )


class AssetUpdate(BaseModel):
    """Partial update. Every field optional; unset fields are left alone."""

    ip_address: str | None = None
    asset_type: AssetType | None = None
    criticality: Criticality | None = None
    owner: str | None = None
    operating_system: str | None = None
    tags: list[str] | None = None

    @field_validator("ip_address")
    @classmethod
    def validate_ip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return str(ip_address(v.strip()))
        except ValueError as exc:
            raise ValueError(f"'{v}' is not a valid IP address") from exc


class HealthStatus(BaseModel):
    status: str
    checks: dict[str, str]
