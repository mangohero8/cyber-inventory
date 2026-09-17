#!/usr/bin/env python3
"""Functional checks against a RUNNING cyber-inventory service.

Shared by:
  scripts/tests/local-integration.sh    -- a service this harness started
  scripts/tests/cloudrun-functional.sh  -- the private Cloud Run service
  scripts/tests/persistence_local.py    -- imports Reporter and Service

Every check prints its id, name, expected and actual result. The process exits
non-zero if a requirement fails, if a known defect unexpectedly stops
reproducing (the script-side equivalent of a strict xfail), or if cleanup
cannot be verified.

Safety rules enforced in code, not only in comments:
  * only records created by this run are ever deleted -- `owns()` requires
    both the run id hostname prefix and the run id tag
  * every record carries a unique run id, so parallel runs cannot collide
  * nothing here shells out; it speaks HTTP and nothing else
  * a bearer token is read from an environment variable, never from argv, so
    it cannot appear in `ps` output or a shell history file

Finding IDs (F-xx) and matrix IDs refer to docs/FUNCTIONAL_SERVICE_AUDIT.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

API = "/api/v1/assets"
STATS = "/api/v1/stats"
OWNER = "functional-test"

ASSET_KEYS = {
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
PROVENANCE_KEYS = {
    "service",
    "version",
    "commit",
    "commit_short",
    "build_time",
    "environment",
}

PASS = "PASS"  # noqa: S105 - a check-result label, not a credential
FAIL = "FAIL"
XFAIL = "XFAIL"
XPASS = "XPASS"
INFO = "INFO"

MULTI_INSTANCE = (
    "on Cloud Run this can also mean the request reached a different "
    "instance than the write did -- see PER-05/F-02"
)


def text_of(value: Any, limit: int = 300) -> str:
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    return rendered if len(rendered) <= limit else rendered[:limit] + " ...(truncated)"


def body_of(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def field_of(response: httpx.Response, key: str) -> Any:
    body = body_of(response)
    return body.get(key) if isinstance(body, dict) else None


def seen(response: httpx.Response) -> str:
    return f"{response.status_code} {text_of(body_of(response))}"


def new_run_id(prefix: str = "fst") -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class Result:
    test_id: str
    name: str
    status: str
    expected: str
    actual: str
    note: str = ""


class Reporter:
    """Prints each check as it happens, then a summary."""

    def __init__(self) -> None:
        self.results: list[Result] = []
        self._lock = threading.Lock()

    def _emit(self, result: Result) -> None:
        with self._lock:
            self.results.append(result)
            print(f"\nTEST {result.test_id}  {result.name}")
            print(f"  expected: {result.expected}")
            print(f"  actual:   {result.actual}")
            if result.note:
                print(f"  note:     {result.note}")
            print(f"  result:   {result.status}", flush=True)

    def check(
        self,
        test_id: str,
        name: str,
        expected: Any,
        actual: Any,
        ok: bool,
        note: str = "",
    ) -> bool:
        self._emit(
            Result(
                test_id,
                name,
                PASS if ok else FAIL,
                text_of(expected),
                text_of(actual),
                note,
            )
        )
        return ok

    def known_defect(
        self,
        test_id: str,
        finding: str,
        name: str,
        expected: Any,
        actual: Any,
        contract_holds: bool,
        reason: str,
    ) -> bool:
        """The script-side equivalent of pytest's strict xfail.

        `contract_holds` means the approved behaviour was observed, i.e. the
        defect did NOT reproduce. That is reported as XPASS and fails the run,
        so a fixed (or environment-specific) defect is never silently absorbed.
        """
        if contract_holds:
            note = (
                f"{finding} did not reproduce here. Report it -- do not edit "
                "this check to preserve the expected failure."
            )
        else:
            note = f"{finding}: {reason}"
        self._emit(
            Result(
                test_id,
                f"[{finding}] {name}",
                XPASS if contract_holds else XFAIL,
                text_of(expected),
                text_of(actual),
                note,
            )
        )
        return not contract_holds

    def info(self, test_id: str, name: str, detail: Any) -> None:
        self._emit(Result(test_id, name, INFO, "(informational)", text_of(detail)))

    def failed(self) -> bool:
        return any(result.status in (FAIL, XPASS) for result in self.results)

    def summary(self) -> int:
        counts = dict.fromkeys((PASS, FAIL, XFAIL, XPASS, INFO), 0)
        for result in self.results:
            counts[result.status] += 1
        print("\n" + "=" * 74)
        print("SUMMARY")
        for result in self.results:
            print(f"  {result.status:<6} {result.test_id:<16} {result.name}")
        print("  " + "  ".join(f"{key}={value}" for key, value in counts.items()))
        print(f"  overall:  {'FAILED' if self.failed() else 'OK'}")
        return 1 if self.failed() else 0


class Service:
    """An HTTP client that can only clean up what it created."""

    def __init__(
        self,
        base_url: str,
        run_id: str,
        reporter: Reporter,
        token: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )
        self.base_url = base_url
        self.run_id = run_id
        self.report = reporter
        self.created: dict[str, str] = {}
        self._lock = threading.Lock()

    def suite_tag(self, suite: str) -> str:
        return f"{self.run_id}.{suite}"

    def hostname(self, suffix: str) -> str:
        return f"{self.run_id}-{suffix}"

    def payload(self, suite: str, suffix: str, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "hostname": self.hostname(suffix),
            "ip_address": "192.0.2.10",
            "asset_type": "server",
            "criticality": "low",
            "owner": OWNER,
            "operating_system": "test-os",
            "tags": [OWNER, self.run_id, self.suite_tag(suite)],
        }
        body.update(overrides)
        return body

    def post_asset(self, payload: dict[str, Any]) -> httpx.Response:
        response = self.http.post(API, json=payload)
        self.register(response)
        return response

    def register(self, response: httpx.Response) -> None:
        if response.status_code == 201:
            body = body_of(response)
            if isinstance(body, dict) and "id" in body:
                with self._lock:
                    self.created[body["id"]] = body.get("hostname", "?")

    def forget(self, asset_id: str) -> None:
        with self._lock:
            self.created.pop(asset_id, None)

    def owns(self, record: dict[str, Any]) -> bool:
        """True only for records this run created: prefix AND tag must match."""
        return record.get("hostname", "").startswith(self.run_id) and self.run_id in (
            record.get("tags") or []
        )

    def stats(self) -> dict[str, Any]:
        body = body_of(self.http.get(STATS))
        return body if isinstance(body, dict) else {}

    def cleanup(self) -> bool:
        ok = True
        for asset_id, hostname in list(self.created.items()):
            try:
                deleted = self.http.delete(f"{API}/{asset_id}").status_code
                after = self.http.get(f"{API}/{asset_id}").status_code
            except httpx.HTTPError as exc:
                deleted, after = type(exc).__name__, "unknown"
            gone = deleted in (204, 404) and after == 404
            self.report.check(
                "CLEANUP",
                f"remove {hostname}",
                "DELETE 204|404 then GET 404",
                f"DELETE {deleted}, GET {after}",
                gone,
                note="" if gone else MULTI_INSTANCE,
            )
            if gone:
                self.forget(asset_id)
            ok = ok and gone

        # Sweep: a create whose response was lost still left a record behind.
        try:
            listed = self.http.get(API, params={"tag": self.run_id})
            leftovers = (
                [r for r in body_of(listed) if self.owns(r)]
                if listed.status_code == 200
                else []
            )
            for record in leftovers:
                self.http.delete(f"{API}/{record['id']}")
            recheck = self.http.get(API, params={"tag": self.run_id})
            remaining = (
                [r["hostname"] for r in body_of(recheck) if self.owns(r)]
                if recheck.status_code == 200
                else ["<list unavailable>"]
            )
        except httpx.HTTPError as exc:
            remaining = [f"<{type(exc).__name__}>"]
        self.report.check(
            "CLEANUP-SWEEP",
            "no records from this run remain",
            f"0 records tagged {self.run_id}",
            remaining or 0,
            not remaining,
            note="" if not remaining else MULTI_INSTANCE,
        )
        return ok and not remaining

    def close(self) -> None:
        self.http.close()


# --------------------------------------------------------------------------
# Suites
# --------------------------------------------------------------------------


def suite_ops(svc: Service, opts: argparse.Namespace) -> None:
    """Health, readiness and build provenance (HLT-01/03, OBS-02/03)."""
    report = svc.report

    health = svc.http.get("/health")
    report.check(
        "HLT-01",
        "GET /health is 200 and reports ok",
        '200 {"status": "ok", "checks": {"process": "ok"}}',
        seen(health),
        health.status_code == 200 and field_of(health, "status") == "ok",
    )

    ready = svc.http.get("/ready")
    checks = field_of(ready, "checks") or {}
    report.check(
        "HLT-03",
        "GET /ready is 200 and reports the store dependency",
        '200 status ok, checks.store ok',
        seen(ready),
        ready.status_code == 200
        and field_of(ready, "status") == "ok"
        and checks.get("store") == "ok",
    )

    version = svc.http.get("/version")
    body = body_of(version)
    keys_ok = (
        version.status_code == 200
        and isinstance(body, dict)
        and set(body) == PROVENANCE_KEYS
    )
    report.check(
        "OBS-VERSION",
        "GET /version returns the provenance fields",
        f"200 with keys {sorted(PROVENANCE_KEYS)}",
        seen(version),
        keys_ok,
    )
    if not keys_ok:
        return

    report.check(
        "OBS-PROVENANCE",
        "the running build is traceable to a commit",
        'commit != "unknown"',
        body["commit"],
        body["commit"] != "unknown",
        note="" if body["commit"] != "unknown" else "this image was not built by CI",
    )
    if opts.expect_commit:
        report.check(
            "OBS-COMMIT",
            "the running commit is the expected one",
            opts.expect_commit,
            body["commit"],
            body["commit"] == opts.expect_commit,
        )
    report.check(
        "OBS-03",
        "X-Build-Commit header matches /version",
        body["commit_short"],
        version.headers.get("X-Build-Commit"),
        version.headers.get("X-Build-Commit") == body["commit_short"],
    )
    report.check(
        "OBS-02",
        "X-Request-ID is present on responses",
        "a non-empty request id",
        health.headers.get("X-Request-ID"),
        bool(health.headers.get("X-Request-ID")),
    )


def suite_lifecycle(svc: Service, opts: argparse.Namespace) -> None:
    """CRUD-15: the complete lifecycle over real HTTP."""
    report = svc.report
    suite = "lifecycle"
    tag = svc.suite_tag(suite)
    hostname = svc.hostname(suite)
    before = svc.stats() if opts.exclusive else {}

    sent = svc.payload(
        suite,
        suite,
        hostname=f"  {hostname.upper()}  ",
        criticality="medium",
        tags=[OWNER.title(), svc.run_id.upper(), tag],
    )
    created = svc.post_asset(sent)
    body = body_of(created)
    shape_ok = (
        created.status_code == 201
        and isinstance(body, dict)
        and set(body) == ASSET_KEYS
    )
    report.check(
        "CRUD-01",
        "POST creates the asset and returns the full representation",
        f"201 with keys {sorted(ASSET_KEYS)}",
        seen(created),
        shape_ok,
    )
    if not shape_ok:
        return

    asset_id = body["id"]
    url = f"{API}/{asset_id}"
    expected_fields = {
        "hostname": hostname,
        "ip_address": "192.0.2.10",
        "asset_type": "server",
        "criticality": "medium",
        "owner": OWNER,
        "operating_system": "test-os",
        "tags": [OWNER, svc.run_id, tag],
    }
    actual_fields = {key: body.get(key) for key in expected_fields}
    report.check(
        "CRUD-01b",
        "the stored representation is normalized (hostname, tags)",
        expected_fields,
        actual_fields,
        actual_fields == expected_fields,
    )

    got = svc.http.get(url)
    report.check(
        "CRUD-03",
        "GET after create returns the identical representation",
        f"200 {text_of(body)}",
        seen(got),
        got.status_code == 200 and body_of(got) == body,
        note="" if got.status_code == 200 else MULTI_INSTANCE,
    )

    listed = svc.http.get(API, params={"tag": tag})
    listed_ids = (
        [item["id"] for item in body_of(listed)] if listed.status_code == 200 else None
    )
    report.check(
        "CRUD-04",
        "the run-tagged list contains exactly this asset",
        f"200 [{asset_id}]",
        f"{listed.status_code} {text_of(listed_ids)}",
        listed_ids == [asset_id],
        note="" if listed_ids == [asset_id] else MULTI_INSTANCE,
    )

    duplicate = svc.post_asset(
        svc.payload(
            suite,
            suite,
            hostname=f" {hostname.upper()} ",
            ip_address="192.0.2.99",
            owner="duplicate-attempt",
        )
    )
    expected_detail = {"detail": f"asset with hostname '{hostname}' already exists"}
    report.check(
        "CRUD-13",
        "a case/whitespace variant of the hostname is rejected with 409",
        f"409 {text_of(expected_detail)}",
        seen(duplicate),
        duplicate.status_code == 409 and body_of(duplicate) == expected_detail,
    )
    unchanged = svc.http.get(url)
    report.check(
        "CRUD-13b",
        "the rejected duplicate did not modify the original",
        text_of(body),
        seen(unchanged),
        body_of(unchanged) == body,
    )

    patch = {"criticality": "critical", "operating_system": "patched-os"}
    patched = svc.http.patch(url, json=patch)
    patched_body = body_of(patched)
    untouched = ASSET_KEYS - set(patch) - {"last_seen"}
    patch_ok = (
        patched.status_code == 200
        and isinstance(patched_body, dict)
        and all(patched_body.get(k) == v for k, v in patch.items())
        and all(patched_body.get(k) == body[k] for k in untouched)
    )
    report.check(
        "CRUD-05",
        "PATCH changes only the supplied fields",
        f"200 with {text_of(patch)}, all other fields unchanged",
        seen(patched),
        patch_ok,
    )
    report.check(
        "CRUD-05b",
        "GET after PATCH returns the patched representation",
        text_of(patched_body),
        seen(svc.http.get(url)),
        body_of(svc.http.get(url)) == patched_body,
    )

    by_criticality = svc.http.get(API, params={"criticality": "critical", "tag": tag})
    report.check(
        "LST-02",
        "the criticality filter finds the patched asset",
        f"200 [{asset_id}]",
        seen(by_criticality),
        by_criticality.status_code == 200
        and [item["id"] for item in body_of(by_criticality)] == [asset_id],
    )
    by_type = svc.http.get(API, params={"asset_type": "server", "tag": tag})
    report.check(
        "LST-03",
        "the asset_type filter finds the asset",
        f"200 [{asset_id}]",
        seen(by_type),
        by_type.status_code == 200
        and [item["id"] for item in body_of(by_type)] == [asset_id],
    )

    if opts.exclusive and before:
        mid = svc.stats()
        expected_total = before.get("total", 0) + 1
        report.check(
            "CRUD-14",
            "stats count the new asset under its patched criticality",
            {"total": expected_total, "critical": 1},
            {
                "total": mid.get("total"),
                "critical": mid.get("by_criticality", {}).get("critical"),
            },
            mid.get("total") == expected_total
            and mid.get("by_criticality", {}).get("critical") == 1,
        )

    deleted = svc.http.delete(url)
    report.check(
        "CRUD-11",
        "DELETE returns 204 with an empty body",
        "204 with an empty body",
        f"{deleted.status_code} {deleted.content!r}",
        deleted.status_code == 204 and deleted.content == b"",
    )
    gone = svc.http.get(url)
    report.check(
        "CRUD-11b",
        "GET after DELETE returns the 404 contract",
        '404 {"detail": "asset not found"}',
        seen(gone),
        gone.status_code == 404 and body_of(gone) == {"detail": "asset not found"},
    )
    report.check(
        "CRUD-11c",
        "a second DELETE returns 404",
        "404",
        seen(svc.http.delete(url)),
        svc.http.delete(url).status_code == 404,
    )
    relisted = svc.http.get(API, params={"tag": tag})
    remaining = (
        [item["hostname"] for item in body_of(relisted)]
        if relisted.status_code == 200
        else None
    )
    duplicate_left = duplicate.status_code == 201
    report.check(
        "CRUD-11d",
        "the run-tagged list no longer contains the asset",
        "200 []" if not duplicate_left else "200 (only the duplicate, if created)",
        f"{relisted.status_code} {text_of(remaining)}",
        remaining is not None and hostname not in remaining,
    )
    if deleted.status_code == 204 and gone.status_code == 404:
        svc.forget(asset_id)

    if opts.exclusive and before:
        after = svc.stats()
        report.check(
            "CRUD-14b",
            "stats return to their pre-test values after DELETE",
            before,
            after,
            after == before,
        )


def suite_validation(svc: Service, opts: argparse.Namespace) -> None:
    """Validation and error contracts over HTTP, including known defects."""
    report = svc.report
    suite = "validation"

    bad_enum = svc.post_asset(svc.payload(suite, "bad-type", asset_type="database"))
    report.check(
        "VAL-02",
        "POST with an invalid asset_type is rejected with 422",
        "422 with an enum error on asset_type",
        seen(bad_enum),
        bad_enum.status_code == 422,
    )

    missing = svc.payload(suite, "no-owner")
    del missing["owner"]
    report.check(
        "VAL-01",
        "POST without a required field is rejected with 422",
        "422 with a missing-field error",
        seen(svc.post_asset(missing)),
        svc.http.post(API, json=missing).status_code == 422,
    )

    unknown_route = svc.http.get("/api/v1/does-not-exist")
    report.check(
        "VAL-14",
        "an unknown route returns the JSON 404 of the application",
        '404 {"detail": "Not Found"}',
        seen(unknown_route),
        unknown_route.status_code == 404
        and body_of(unknown_route) == {"detail": "Not Found"},
        note="a differently shaped 404 means something upstream answered",
    )

    blank_host = svc.post_asset(svc.payload(suite, "blank", hostname="   "))
    report.known_defect(
        "VAL-04",
        "F-04",
        "a whitespace-only hostname is rejected with 422, not 500",
        "422",
        seen(blank_host),
        blank_host.status_code == 422,
        "min_length is checked before the value is stripped; the store's "
        "re-validation then raises inside the handler",
    )

    unknown_field = svc.post_asset(
        svc.payload(suite, "unknown-field", environment="production")
    )
    report.known_defect(
        "VAL-07",
        "F-12",
        "an unknown field (environment) is rejected with 422",
        "422",
        seen(unknown_field),
        unknown_field.status_code == 422,
        "AssetCreate uses extra='ignore', so unknown fields are dropped and "
        "the asset is created anyway",
    )

    blank_owner = svc.post_asset(svc.payload(suite, "blank-owner", owner="   "))
    report.known_defect(
        "VAL-05",
        "F-16",
        "a whitespace-only owner is rejected with 422",
        "422",
        seen(blank_owner),
        blank_owner.status_code == 422,
        "owner is NonEmptyStr without stripping, so a blank owner is stored",
    )


def suite_filters(svc: Service, opts: argparse.Namespace) -> None:
    """LST-02/03/07 and F-05, isolated from other data by the run tag."""
    report = svc.report
    suite = "filters"
    tag = svc.suite_tag(suite)
    seeds = {
        "high-server": {"criticality": "high", "asset_type": "server"},
        "low-workstation": {"criticality": "low", "asset_type": "workstation"},
        "critical-container": {"criticality": "critical", "asset_type": "container"},
    }
    hosts = {}
    for suffix, fields in seeds.items():
        response = svc.post_asset(svc.payload(suite, suffix, **fields))
        if response.status_code != 201:
            report.check(
                "FILTERS-SEED",
                f"seed {suffix}",
                "201",
                seen(response),
                False,
            )
            return
        hosts[suffix] = body_of(response)["hostname"]

    def filtered(**params: Any) -> list[str]:
        response = svc.http.get(API, params={"tag": tag, **params})
        if response.status_code != 200:
            return [f"HTTP {response.status_code}"]
        return sorted(item["hostname"] for item in body_of(response))

    report.check(
        "LST-01",
        "the run tag lists exactly the three seeded assets",
        sorted(hosts.values()),
        filtered(),
        filtered() == sorted(hosts.values()),
        note=MULTI_INSTANCE,
    )
    report.check(
        "LST-02",
        "criticality=high selects one asset",
        [hosts["high-server"]],
        filtered(criticality="high"),
        filtered(criticality="high") == [hosts["high-server"]],
    )
    report.check(
        "LST-03",
        "asset_type=workstation selects one asset",
        [hosts["low-workstation"]],
        filtered(asset_type="workstation"),
        filtered(asset_type="workstation") == [hosts["low-workstation"]],
    )
    combined = filtered(criticality="critical", asset_type="container")
    report.check(
        "LST-07",
        "combined filters intersect",
        [hosts["critical-container"]],
        combined,
        combined == [hosts["critical-container"]],
    )
    empty = filtered(criticality="high", asset_type="workstation")
    report.check(
        "LST-07b",
        "a combination with no match is an empty list",
        [],
        empty,
        empty == [],
    )
    upper_tag = svc.http.get(API, params={"tag": tag.upper()})
    report.check(
        "LST-04",
        "the tag filter is case-insensitive",
        "the same three assets",
        f"{upper_tag.status_code} {len(body_of(upper_tag) or [])} records",
        upper_tag.status_code == 200 and len(body_of(upper_tag)) == 3,
    )

    for field, value in (
        ("criticality", "bogus"),
        ("criticality", "HIGH"),
        ("asset_type", "bogus"),
    ):
        response = svc.http.get(API, params={"tag": tag, field: value})
        report.known_defect(
            f"LST-05/{field}={value}",
            "F-05",
            f"an invalid filter value ({field}={value}) is rejected with 422",
            "422",
            seen(response),
            response.status_code == 422,
            "filters are typed `str | None` instead of the schema enums, so an "
            "invalid or wrong-case value silently returns an empty list",
        )


def suite_concurrency(svc: Service, opts: argparse.Namespace) -> None:
    """CON-01..04 over real HTTP. Requires --exclusive; never run on prod."""
    report = svc.report
    if not opts.exclusive:
        report.info(
            "CON",
            "concurrency suite skipped",
            "requires --exclusive: it would put load on a shared service",
        )
        return

    suite = "concurrency"
    tag = svc.suite_tag(suite)
    race = svc.payload(suite, "race")
    with ThreadPoolExecutor(max_workers=25) as pool:
        codes = list(pool.map(lambda _: svc.post_asset(race).status_code, range(50)))
    report.check(
        "CON-01",
        "50 concurrent creates of one hostname admit exactly one",
        "201 x1, 409 x49",
        f"201 x{codes.count(201)}, 409 x{codes.count(409)}, "
        f"other {sorted(c for c in codes if c not in (201, 409))}",
        codes.count(201) == 1 and codes.count(409) == 49,
    )

    payloads = [svc.payload(suite, f"bulk-{i:03d}") for i in range(60)]
    with ThreadPoolExecutor(max_workers=25) as pool:
        codes = list(pool.map(lambda p: svc.post_asset(p).status_code, payloads))
    listed = svc.http.get(API, params={"tag": tag})
    stored = (
        {item["hostname"] for item in body_of(listed)}
        if listed.status_code == 200
        else set()
    )
    expected_hosts = {p["hostname"] for p in payloads}
    report.check(
        "CON-02",
        "60 concurrent distinct creates are all stored",
        "201 x60 and all 60 listed",
        f"201 x{codes.count(201)}, listed {len(expected_hosts & stored)}/60",
        codes.count(201) == 60 and expected_hosts <= stored,
    )

    target = svc.post_asset(
        svc.payload(
            suite,
            "patch-target",
            owner="original-owner",
            operating_system="original-os",
        )
    )
    if target.status_code != 201:
        report.check("CON-03", "seed the patch target", "201", seen(target), False)
        return
    target_id = body_of(target)["id"]
    bodies = [
        {"owner": f"owner-{i}"} if i % 2 == 0 else {"operating_system": f"os-{i}"}
        for i in range(40)
    ]
    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(
            pool.map(
                lambda b: svc.http.patch(f"{API}/{target_id}", json=b).status_code,
                bodies,
            )
        )
    final = body_of(svc.http.get(f"{API}/{target_id}"))
    no_revert = (
        isinstance(final, dict)
        and final.get("owner") != "original-owner"
        and final.get("operating_system") != "original-os"
    )
    report.check(
        "CON-03",
        "40 concurrent PATCHes do not revert each other's fields",
        "200 x40, neither field left at its original value",
        f"200 x{codes.count(200)}, owner={final.get('owner')}, "
        f"os={final.get('operating_system')}",
        codes.count(200) == 40 and no_revert,
    )

    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(
            pool.map(
                lambda _: svc.http.delete(f"{API}/{target_id}").status_code, range(20)
            )
        )
    report.check(
        "CON-04",
        "20 concurrent DELETEs of one asset succeed exactly once",
        "204 x1, 404 x19",
        f"204 x{codes.count(204)}, 404 x{codes.count(404)}",
        codes.count(204) == 1 and codes.count(404) == 19,
    )
    if codes.count(204) == 1:
        svc.forget(target_id)


def suite_smoke_report(svc: Service, opts: argparse.Namespace) -> None:
    """X-01: report CI leftovers. Reports only -- never deletes."""
    report = svc.report
    listed = svc.http.get(API)
    if listed.status_code != 200:
        report.check(
            "X-01", "list existing records", "200", seen(listed), False
        )
        return
    records = body_of(listed)

    def brief(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record.get("id"),
            "hostname": record.get("hostname"),
            "owner": record.get("owner"),
            "first_seen": record.get("first_seen"),
        }

    ci_records = [
        brief(r)
        for r in records
        if r.get("hostname", "").startswith("smoke-") or r.get("owner") == "ci"
    ]
    stale_tests = [
        brief(r) for r in records if r.get("owner") == OWNER and not svc.owns(r)
    ]
    report.info(
        "X-01",
        "CI smoke records present (smoke-* or owner=ci) -- reported, never deleted",
        ci_records or "none",
    )
    report.info(
        "X-01b",
        "records from earlier functional-test runs -- reported, never deleted",
        stale_tests or "none",
    )
    report.info(
        "PER-05",
        "total records visible from the instance that served this request",
        {"total": len(records)},
    )


SUITES = {
    "ops": suite_ops,
    "lifecycle": suite_lifecycle,
    "validation": suite_validation,
    "filters": suite_filters,
    "concurrency": suite_concurrency,
    "smoke-report": suite_smoke_report,
}


# --------------------------------------------------------------------------
# Log checks (for a service whose stdout this harness captured)
# --------------------------------------------------------------------------


def run_log_checks(opts: argparse.Namespace) -> int:
    report = Reporter()
    path = Path(opts.log_file)
    if not path.exists():
        report.check("LOGS", "log file exists", str(path), "missing", False)
        return report.summary()

    structured: list[dict[str, Any]] = []
    plain: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            plain.append(line)
            continue
        if isinstance(parsed, dict):
            structured.append(parsed)
        else:
            plain.append(line)

    def events(name: str) -> list[dict[str, Any]]:
        return [record for record in structured if record.get("event") == name]

    startups = events("startup")
    report.check(
        "OBS-STARTUP",
        "the service logged exactly one structured startup line",
        "1 startup event",
        f"{len(startups)} startup events",
        len(startups) == 1,
    )
    if startups and opts.expect_commit:
        report.check(
            "OBS-STARTUP-COMMIT",
            "the startup line carries the build commit",
            opts.expect_commit,
            startups[0].get("commit"),
            startups[0].get("commit") == opts.expect_commit,
        )
    warnings = events("provenance_missing")
    report.check(
        "OBS-PROVENANCE-LOG",
        "no provenance_missing warning was logged",
        "0 warnings",
        f"{len(warnings)} warnings",
        not warnings,
    )

    requests = [r for r in structured if r.get("message") == "request handled"]
    required = {
        "severity",
        "request_id",
        "method",
        "path",
        "status",
        "duration_ms",
        "timestamp",
    }
    incomplete = [r for r in requests if not required <= set(r)]
    report.check(
        "OBS-01",
        "request logs are structured JSON with the required fields",
        f">0 lines, every line containing {sorted(required)}",
        f"{len(requests)} lines, {len(incomplete)} incomplete",
        bool(requests) and not incomplete,
    )
    report.check(
        "OBS-CREATE",
        "asset_created audit events were emitted",
        ">0",
        len(events("asset_created")),
        bool(events("asset_created")),
    )

    if opts.expect_mutations:
        updated, deleted = len(events("asset_updated")), len(events("asset_deleted"))
        report.known_defect(
            "OBS-05",
            "F-08",
            "PATCH and DELETE emit asset_updated / asset_deleted audit events",
            "asset_updated > 0 and asset_deleted > 0",
            f"asset_updated={updated}, asset_deleted={deleted}",
            updated > 0 and deleted > 0,
            "only asset_created exists, so updates and deletions leave no "
            "domain audit trail -- the only durable record of an in-memory "
            "inventory",
        )

    if opts.expect_shutdown:
        shutdowns = events("shutdown")
        report.check(
            "OBS-SHUTDOWN",
            "a structured shutdown line was logged on SIGTERM",
            "1 shutdown event",
            f"{len(shutdowns)} shutdown events",
            len(shutdowns) == 1,
        )
    if opts.exit_code is not None:
        # 143 is 128+SIGTERM: uvicorn catches the signal, runs the shutdown
        # lifespan, then exits with the signal status, which is what POSIX
        # asks of a process that handled a fatal signal. 137 (128+SIGKILL) is
        # the failure worth catching: the grace period expired and in-flight
        # requests were dropped.
        clean = opts.exit_code in (0, 143)
        report.check(
            "RUN-04",
            "the service exited cleanly on SIGTERM",
            "exit code 0, or 143 (signal handled, then exited with 128+SIGTERM)",
            f"exit code {opts.exit_code}",
            clean,
            note=""
            if clean
            else "137 means SIGKILL after the grace period: in-flight "
            "requests were dropped",
        )
    report.info(
        "OBS-07",
        "non-JSON stdout lines (uvicorn's own loggers, unstructured in "
        "Cloud Logging)",
        {"count": len(plain), "sample": plain[:2]},
    )
    return report.summary()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run_http_checks(opts: argparse.Namespace) -> int:
    token = None
    if opts.auth_env:
        token = os.environ.get(opts.auth_env, "").strip()
        if not token:
            print(f"ERROR: {opts.auth_env} is unset or empty", file=sys.stderr)
            return 2

    names = [name.strip() for name in opts.suites.split(",") if name.strip()]
    unknown = [name for name in names if name not in SUITES]
    if unknown:
        print(f"ERROR: unknown suite(s): {unknown}", file=sys.stderr)
        return 2

    run_id = new_run_id(opts.run_prefix)
    print("=" * 74)
    print("cyber-inventory functional checks")
    print(f"  target:    {opts.base_url}")
    print(f"  run id:    {run_id}")
    print(f"  suites:    {', '.join(names)}")
    print(f"  exclusive: {opts.exclusive}")
    print(f"  auth:      {'bearer token' if token else 'none'}")
    print("=" * 74)

    reporter = Reporter()
    svc = Service(opts.base_url, run_id, reporter, token=token)
    try:
        for name in names:
            print(f"\n--- suite: {name} ---")
            SUITES[name](svc, opts)
    except httpx.HTTPError as exc:
        reporter.check(
            "HARNESS",
            "the service stayed reachable",
            "HTTP responses",
            f"{type(exc).__name__}: {exc}",
            False,
        )
    finally:
        print("\n--- cleanup ---")
        try:
            svc.cleanup()
        except httpx.HTTPError as exc:
            reporter.check(
                "CLEANUP",
                "cleanup completed",
                "no transport errors",
                f"{type(exc).__name__}: {exc}",
                False,
            )
        svc.close()
    return reporter.summary()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Functional checks against a running cyber-inventory service."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    http_parser = sub.add_parser("http", help="HTTP checks against a running service")
    http_parser.add_argument("--base-url", required=True)
    http_parser.add_argument(
        "--auth-env",
        help="name of the environment variable holding a bearer token "
        "(never pass the token itself on the command line)",
    )
    http_parser.add_argument(
        "--suites", default="ops,lifecycle,validation,filters"
    )
    http_parser.add_argument(
        "--exclusive",
        action="store_true",
        help="this run is the only writer: enables stats assertions and the "
        "concurrency suite",
    )
    http_parser.add_argument("--expect-commit")
    http_parser.add_argument("--run-prefix", default="fst")

    logs_parser = sub.add_parser("logs", help="checks on a captured stdout log")
    logs_parser.add_argument("--log-file", required=True)
    logs_parser.add_argument("--expect-commit")
    logs_parser.add_argument("--expect-shutdown", action="store_true")
    logs_parser.add_argument("--expect-mutations", action="store_true")
    logs_parser.add_argument("--exit-code", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    opts = build_parser().parse_args(argv)
    if opts.command == "http":
        return run_http_checks(opts)
    return run_log_checks(opts)


if __name__ == "__main__":
    raise SystemExit(main())
