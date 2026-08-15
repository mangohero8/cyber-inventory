"""Asset storage.

Deliberately an in-memory dict behind a narrow interface.

Why no database: adding Postgres would add a migration story, a connection
pool, a test fixture, and a Cloud SQL instance -- none of which teach you
anything about the pipeline, which is what this project is for. The
interface below is small enough that swapping in a real backend later is a
contained change, and that is the actual lesson: put a seam where you
expect to change.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.models import Asset, AssetCreate, AssetUpdate


class DuplicateAssetError(Exception):
    """Raised when registering a hostname that already exists."""


class AssetStore:
    """Thread-safe in-memory asset store.

    The lock is not decoration. Uvicorn serves requests concurrently, and
    read-modify-write sequences like `update` are genuinely racy without it.
    """

    def __init__(self) -> None:
        self._assets: dict[str, Asset] = {}
        self._hostname_index: dict[str, str] = {}
        self._lock = threading.RLock()

    def create(self, payload: AssetCreate) -> Asset:
        with self._lock:
            if payload.hostname in self._hostname_index:
                raise DuplicateAssetError(
                    f"asset with hostname '{payload.hostname}' already exists"
                )
            asset = Asset(**payload.model_dump())
            self._assets[asset.id] = asset
            self._hostname_index[asset.hostname] = asset.id
            return asset

    def get(self, asset_id: str) -> Asset | None:
        with self._lock:
            return self._assets.get(asset_id)

    def get_by_hostname(self, hostname: str) -> Asset | None:
        with self._lock:
            asset_id = self._hostname_index.get(hostname.strip().lower())
            return self._assets.get(asset_id) if asset_id else None

    def list(
        self,
        *,
        criticality: str | None = None,
        asset_type: str | None = None,
        tag: str | None = None,
    ) -> list[Asset]:
        with self._lock:
            results = list(self._assets.values())

        if criticality:
            results = [a for a in results if a.criticality == criticality]
        if asset_type:
            results = [a for a in results if a.asset_type == asset_type]
        if tag:
            needle = tag.strip().lower()
            results = [a for a in results if needle in a.tags]

        # Stable ordering. Unordered list endpoints produce flaky tests and
        # confusing diffs in demos.
        return sorted(results, key=lambda a: a.hostname)

    def update(self, asset_id: str, payload: AssetUpdate) -> Asset | None:
        with self._lock:
            existing = self._assets.get(asset_id)
            if existing is None:
                return None

            changes = payload.model_dump(exclude_unset=True, exclude_none=True)
            updated = existing.model_copy(
                update={**changes, "last_seen": datetime.now(timezone.utc)}
            )
            self._assets[asset_id] = updated
            return updated

    def delete(self, asset_id: str) -> bool:
        with self._lock:
            asset = self._assets.pop(asset_id, None)
            if asset is None:
                return False
            self._hostname_index.pop(asset.hostname, None)
            return True

    def count(self) -> int:
        with self._lock:
            return len(self._assets)

    def clear(self) -> None:
        """Reset state. Used by test fixtures."""
        with self._lock:
            self._assets.clear()
            self._hostname_index.clear()


# Module-level singleton, injected via FastAPI dependency so tests can
# substitute a clean instance.
_store = AssetStore()


def get_store() -> AssetStore:
    return _store
