"""F-13 -- data integrity under concurrent access, within one process.

Real threads against a fresh AssetStore. The GIL makes races rare by default,
so `sys.setswitchinterval` is lowered to force frequent thread switches inside
the store's read-modify-write sections, and a Barrier releases every worker at
the same moment for maximum contention.

Scope is one process. Integrity ACROSS Cloud Run instances is a separate,
architectural failure (F-02) that no in-process test can cover: each instance
owns a private dict.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import Any

import pytest

from app.models import AssetCreate, AssetUpdate
from app.store import AssetStore, DuplicateAssetError
from tests.support import asset

BARRIER_TIMEOUT = 20
JOIN_TIMEOUT = 60


@pytest.fixture(autouse=True)
def aggressive_thread_switching():
    """Switch threads far more often than usual, to surface races."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(previous)


@pytest.fixture
def store() -> AssetStore:
    """A dedicated store, so these tests never touch the app singleton."""
    return AssetStore()


def hammer(work: Callable[[int], Any], count: int) -> list[Any]:
    """Run work(0..count-1) on `count` threads released simultaneously.

    Exceptions are returned in place of results so a test can count outcomes
    instead of losing them in a worker thread.
    """
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def worker(index: int) -> None:
        barrier.wait(timeout=BARRIER_TIMEOUT)
        try:
            results[index] = work(index)
        except Exception as exc:  # returned, so the test can assert on it
            results[index] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), "worker threads hung"
    return results


def unexpected(results: list[Any], *allowed: type[Exception]) -> list[Any]:
    return [
        item
        for item in results
        if isinstance(item, Exception) and not isinstance(item, allowed)
    ]


def test_concurrent_creates_of_one_hostname_admit_exactly_one(store):
    """CON-01: the uniqueness check and the insert are one atomic step."""
    payload = AssetCreate(**asset(hostname="race-01"))

    results = hammer(lambda _: store.create(payload), 200)

    assert unexpected(results, DuplicateAssetError) == []
    created = [item for item in results if not isinstance(item, Exception)]
    conflicts = [item for item in results if isinstance(item, DuplicateAssetError)]
    assert len(created) == 1
    assert len(conflicts) == 199
    assert store.count() == 1
    assert store.get_by_hostname("race-01").id == created[0].id


def test_concurrent_distinct_creates_lose_nothing(store):
    """CON-02: no write is dropped and no id collides."""

    def create(index: int):
        return store.create(AssetCreate(**asset(hostname=f"bulk-{index:04d}")))

    results = hammer(create, 300)

    assert unexpected(results) == []
    assert store.count() == 300
    assert len({item.id for item in results}) == 300
    assert len(store.list()) == 300
    assert all(store.get_by_hostname(f"bulk-{i:04d}") is not None for i in range(300))


def test_concurrent_updates_of_different_fields_do_not_revert_each_other(store):
    """CON-03: no update is lost to a stale read-modify-write copy."""
    original = store.create(
        AssetCreate(**asset(owner="original-owner", operating_system="original-os"))
    )
    owners = {f"owner-{i}" for i in range(0, 200, 2)}
    systems = {f"os-{i}" for i in range(1, 200, 2)}

    def update(index: int):
        change = (
            AssetUpdate(owner=f"owner-{index}")
            if index % 2 == 0
            else AssetUpdate(operating_system=f"os-{index}")
        )
        return store.update(original.id, change)

    results = hammer(update, 200)

    assert unexpected(results) == []
    assert None not in results
    final = store.get(original.id)
    assert final.owner in owners, "an owner update was reverted by a stale copy"
    assert final.operating_system in systems, "an OS update was reverted"
    assert final.id == original.id
    assert final.first_seen == original.first_seen
    assert store.count() == 1


def test_concurrent_deletes_of_one_asset_succeed_exactly_once(store):
    """CON-04: delete is not idempotent by accident -- exactly one winner."""
    target = store.create(AssetCreate(**asset(hostname="doomed-01")))

    results = hammer(lambda _: store.delete(target.id), 100)

    assert results.count(True) == 1
    assert results.count(False) == 99
    assert store.count() == 0
    assert store.get_by_hostname("doomed-01") is None


def test_create_delete_churn_leaves_the_hostname_index_consistent(store):
    """CON-05: no orphaned index entry, so the hostname stays registrable."""
    payload = AssetCreate(**asset(hostname="churn-01"))

    def churn(_: int) -> str:
        try:
            created = store.create(payload)
        except DuplicateAssetError:
            return "conflict"
        return "deleted" if store.delete(created.id) else "vanished"

    results = hammer(churn, 200)

    assert unexpected(results) == []
    assert "vanished" not in results
    assert store.count() == 0
    assert store.get_by_hostname("churn-01") is None
    assert store.create(payload).hostname == "churn-01"


def test_reads_during_writes_never_fail_and_aggregates_agree(store):
    """CON-06: listing while writing neither raises nor miscounts."""

    def mixed(index: int):
        if index % 3 == 0:
            return store.create(AssetCreate(**asset(hostname=f"mix-{index:04d}")))
        if index % 3 == 1:
            return len(store.list(criticality="high"))
        return store.count()

    results = hammer(mixed, 300)

    assert unexpected(results) == []
    assert store.count() == 100
    assert len(store.list()) == 100
