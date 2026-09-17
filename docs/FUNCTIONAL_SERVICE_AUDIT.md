# cyber-inventory — Functional-Service Audit & Test Matrix

> **2026-09-15 — product/API decisions approved.** These supersede every
> "decision needed" note below.
>
> | Topic | Approved contract | Effect |
> |---|---|---|
> | Unknown fields | Unknown client fields are rejected with 422, not silently discarded. `environment` is **not** an Asset property — it is the deployment environment reported by `/version` — and is not being added to Asset to accommodate the earlier payload. | **F-12** becomes a confirmed defect (strict xfail), on create and PATCH, including client-supplied `id` / `first_seen` / `last_seen`. |
> | Enum filters | List filters are strict and case-sensitive, consistent with the Asset schema. `criticality=bogus`, `criticality=HIGH` and `asset_type=bogus` return 422. The `tag` filter stays case-insensitive. | **F-05** confirmed (strict xfail). |
> | PATCH null | `null` clears a nullable field (`operating_system`). Required / non-nullable fields reject `null` with 422. | **F-17** confirmed (strict xfail), both halves. |
>
> **Implementation notes for the harness (Phase 2).** Automated coverage lives
> in `tests/contract/` (pytest) and `scripts/tests/` (HTTP, persistence,
> Cloud Run) — see `scripts/tests/README.md`. Two deviations from §5 below:
> the HTTP integration checks are a script (`scripts/tests/functional_checks.py`)
> rather than a `tests/integration/` pytest package, so a plain `pytest` run can
> never reach the network; and no pytest markers were registered, so
> `pyproject.toml` is untouched — characterization tests are named
> `test_characterize_*` instead.
>
> **Contract conflict to settle when F-12 is fixed:** the pre-existing
> `tests/test_assets_api.py::test_client_cannot_set_its_own_id` asserts **201**
> when a client sends `id`. Under the approved contract that request is a 422,
> so that test must change in the same commit that fixes F-12.

**Scope:** read-only audit of `mangohero8/cyber-inventory` @ `7c39111` (origin/main, 2026-09-15).
**Files audited:** `app/main.py`, `app/models.py`, `app/store.py`, `app/version.py`, `tests/*`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `Makefile`, `pyproject.toml`, `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`, `scripts/setup-gcp.sh`, `README.md`, `PHASES.md`, `docs/PLANNING.md`.
**Nothing in the repository was modified.** Behavior was confirmed with throwaway probes run from outside the repo: an in-process `TestClient` probe, plus a live-uvicorn concurrency probe on `127.0.0.1:8099` that cleaned up after itself.

**Environment:** Python 3.14.7 venv locally (CI pins 3.11). Docker 29.7.2 daemon running. `gcloud` authenticated to project (project id omitted). Existing suite: **29 passed, 90.20% coverage**. App code is unchanged since the last full run at `c9f27bb`.

Evidence tags used below: **[observed]** = reproduced in a probe; **[code]** = read directly from source; **[inferred]** = follows from code and platform config but not yet exercised on Cloud Run.

---

## 0. Runner legend

| Tag | Runner | Where it runs |
|---|---|---|
| **P** | pytest, in-process `TestClient` | local + CI, no network |
| **I** | integration script against a running service (uvicorn or local Docker) | local only |
| **C** | Cloud Run functional script against the private deployed service | requires deployed Cloud Run |
| **D** | destructive: restarts a process, container, or revision, or injects faults | kept separate from smoke |

"Destructive" means it restarts or replaces something, injects a failure, or can affect data it did not create. Creating and then deleting uniquely named test records is **not** destructive.

---

## 1. In-memory storage: exactly what is lost

`app/store.py` keeps all state in one module-level `AssetStore()` singleton (`_store`, line 113), inside a single Python process:

| State | Structure | Lost on process / container / instance / revision replacement | Consequence |
|---|---|---|---|
| Every asset record: `id`, `hostname`, `ip_address`, `asset_type`, `criticality`, `owner`, `operating_system`, `tags`, `first_seen`, `last_seen` | `AssetStore._assets: dict[str, Asset]` | **All of it** | Inventory is empty after any replacement |
| Hostname uniqueness constraint | `AssetStore._hostname_index: dict[str, str]` | **All of it** | A hostname that existed can be registered again with **no 409** |
| Asset identity | UUID4 generated per create (`models.py:96`) | **Not reproducible** | Re-registering a host issues a **new id**; ids held by clients return 404 |
| Discovery history | `first_seen` set at create time | **Reset** | Re-registered assets look newly discovered |
| Aggregates | `/api/v1/stats` computed from `_assets` | Reset to `{"total":0,...}` | Dashboards drop to zero |
| Update and delete history | Never recorded anywhere | n/a (never existed) | No record of who changed or removed what |

**What survives:** build provenance (image ENV), config, and **Cloud Logging** output. That output is only a partial forensic trail. `asset_created` logs `asset_id` + `hostname` only: no IP, type, criticality, owner, or tags. PATCH and DELETE emit no domain event at all, just the generic `request handled` line with the path **[observed]**. The inventory **cannot be rebuilt from logs.**

**Triggers that wipe or split state on the current Cloud Run config** (`deploy.yml:100-115`, **[inferred]**):

| Trigger | Why it happens | Effect |
|---|---|---|
| Every merge to `main` | Deploy creates a new revision and shifts 100% traffic to it | New revision starts empty. Old revision's data becomes unreachable at promotion |
| Idle scale-to-zero | `--min-instances=0` | Next request cold-starts an empty instance |
| Scale-out | `--max-instances=3` | Each instance has its **own** store: POST on A then GET on B returns 404, lists differ, and the same hostname can be registered on A and B without a 409 |
| Crash / OOM / platform maintenance | `--memory=512Mi`, request bodies unbounded (see VAL-11) | Instance replaced empty |
| Local `make run` | `uvicorn --reload` | Every saved code edit wipes local data |

**CI side effect [inferred]:** the candidate validation step (`deploy.yml:224-232`) POSTs `smoke-${GITHUB_RUN_ID}` and never deletes it. The candidate revision is the one then promoted to 100%, so production can briefly list a CI-owned record until that instance recycles.

---

## 2. The three previously observed inconsistencies

### 2a. `environment` accepted on create but not returned — **CONFIRMED, root cause: silent extra-field drop**
- **[observed]** `POST` with `"environment":"production"` returns **201**. The response has no `environment` key.
- **[code]** `AssetCreate` (`models.py:39`) uses Pydantic's default `extra="ignore"`. No model has an `environment` field. The only "environment" in the codebase is the **deployment** environment in `BuildInfo` (`version.py:36`), surfaced by `/version`. It is not an asset attribute.
- The same mechanism drops `id`, `first_seen`, and `last_seen` when a client sends them. The existing `test_client_cannot_set_its_own_id` **depends on** that silent drop, since it expects 201.
- **Classification: contract ambiguity (MEDIUM), not a storage bug.** Needs a product decision:
  - (a) add `environment` as a real asset field,
  - (b) `extra="forbid"`, so 422 on unknown fields, which would require changing that existing test, or
  - (c) keep ignoring and document it.
- Tests should pin current behavior until you decide.

### 2b. Invalid enum values in POST return 422 — **CONFIRMED, correct behavior, untested**
- **[observed]** `asset_type:"database"` → 422 `type: enum`. `criticality:"HIGH"` → 422 (enums are case-sensitive). PATCH with `criticality:"bogus"` → 422.
- **No existing test covers invalid enums on POST or PATCH.** This is a regression gap, not a defect.

### 2c. List filters accept invalid values — **CONFIRMED DEFECT**
- **[observed]** `?criticality=bogus` → **200 `[]`**. `?asset_type=bogus` → **200 `[]`**. `?criticality=HIGH` → **200 `[]`**, even though `high` assets exist.
- **[code]** `main.py:215-216` types the filters as `str | None`, not `Criticality | None` / `AssetType | None`. `store.list` does plain equality.
- **Why it matters:** a typo'd query reads as "no critical assets." For a security inventory that is a false negative, not a cosmetic issue. It is also inconsistent with POST, which rejects the same values.
- `?tag=` is case-insensitive **[observed]**, so filter case rules differ across parameters. **HIGH.**

---

## 3. Newly found defects (not previously known)

| ID | Defect | Evidence |
|---|---|---|
| **VAL-04** | Whitespace-only `hostname` → **HTTP 500** | **[observed]** `"   "` passes `min_length=1` *before* the after-validator strips it to `""`. `store.create` then re-validates via `Asset(**payload.model_dump())`, which raises `pydantic_core.ValidationError` inside the handler. Result: unhandled 500 with a plain-text body. |
| **CRUD-08** | PATCH does not normalize tags | **[observed]** PATCH `["PROD","prod"," Web "]` is stored verbatim. `AssetUpdate` (`models.py:105`) has no tag validator. `?tag=web` will not match `" Web "`, and duplicates are stored. |
| **VAL-12** | PATCH `owner:""` accepted | **[observed]** 200. Create requires non-empty (`NonEmptyStr`), but `AssetUpdate.owner` is a plain `str`, so an invariant enforced on create is breakable on update. |
| **HLT-04** | Readiness failure returns **HTTP 200** | **[observed]** When the store check fails, `/ready` returns `200 {"status":"degraded"}`. Any status-code consumer treats it as ready, including `curl -sf` in `deploy.yml:200`. Latent today because a dict `count()` cannot fail; becomes critical the moment a DB exists. |
| **RUN-01** | 500 responses lose correlation | **[observed]** Body is `text/plain` `Internal Server Error`, with **no** `X-Request-ID` and **no** `X-Build-Commit`. The middleware re-raises before setting headers (`main.py:109-115`). The ERROR log line does carry `request_id`. |
| **OBS-05** | No audit events for update or delete | **[observed]** Create emits `asset_created`; PATCH and DELETE emit only generic request lines. |
| **VAL-05** | Whitespace-only `owner` accepted | **[observed]** `"   "` → 201, stored as `"   "`. |
| **CRUD-07** | PATCH cannot clear a nullable field | **[observed]** `{"operating_system":null}` → 200, value unchanged (`exclude_none=True`, `store.py:85`). |
| **CRUD-09** | PATCH of non-patchable `hostname` silently ignored | **[observed]** 200, no change, no error. |
| **VAL-11** | No size limits | **[observed]** 10,000-char `operating_system` → 201; 5,000 tags → 201. Unbounded memory growth on a 512Mi instance whose loss is total. |

Lower-severity items (docs drift, dead code, timestamp skew) are in the matrix and the prioritized table.

---

## 4. Functional-service test matrix

Each test lists: **Req** requirement · **Impl** existing implementation · **Cov** existing automated coverage · **Gap** missing coverage · **Proc** procedure · **Exp** expected result · **P/F** pass/fail criteria · then a flags line: `Destructive · Local · Cloud Run · Runner`.

"Exp" is the **target contract**. Where current behavior differs, it is marked **CURRENT:** so the test can be written as a strict-xfail that exposes it accurately.

### 4.1 CRUD correctness

**CRUD-01 — Create returns 201 with the complete stored representation**
- **Req:** POST returns every submitted field (normalized) plus server-owned `id`, `first_seen`, `last_seen`.
- **Impl:** `create_asset` → `store.create` → `Asset(**payload.model_dump())`.
- **Cov:** `test_create_returns_201_and_the_stored_asset` checks `hostname`, and that `id` and `first_seen` are truthy.
- **Gap:** full-body equality; `id` is a valid UUID; timestamps are ISO-8601 UTC (`Z`); `criticality` defaults to `medium` when omitted; `operating_system` defaults to null; `tags` defaults to `[]`.
- **Proc:** POST a minimal payload and a full payload with a unique hostname. Compare each response key by key.
- **Exp:** 201. Exactly the keys `hostname, ip_address, asset_type, criticality, owner, operating_system, tags, id, first_seen, last_seen`.
- **P/F:** any missing or extra key, wrong default, non-UUID id, or non-UTC timestamp = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (+ **C** variant)

**CRUD-02 — Server-owned fields cannot be set by clients**
- **Req:** client-supplied `id`, `first_seen`, `last_seen` are not stored.
- **Impl:** `AssetCreate` has no such fields; the extra-ignore default drops them.
- **Cov:** `test_client_cannot_set_its_own_id` (id only).
- **Gap:** `first_seen` / `last_seen` supplied as `2000-01-01T00:00:00Z`.
- **Proc:** POST with all three fields set.
- **Exp:** 201; `id` ≠ supplied; both timestamps ≈ now.
- **P/F:** any supplied value echoed back = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-03 — GET by id returns the same representation as create**
- **Req:** read-after-write consistency on a single instance.
- **Impl:** `get_asset` → `store.get`.
- **Cov:** **none.** No test does a 200 GET of a created asset.
- **Gap:** the whole requirement.
- **Proc:** POST, then GET `/api/v1/assets/{id}`.
- **Exp:** 200; body == POST body.
- **P/F:** any field differs = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ (single instance) · **P**

**CRUD-04 — List includes created asset with identical representation**
- **Req:** list and GET agree.
- **Impl:** `store.list`.
- **Cov:** counts only (`test_list_is_filterable`).
- **Gap:** element equality with GET.
- **Proc:** POST 2 assets; GET list; match by id.
- **Exp:** each listed object == its GET body.
- **P/F:** mismatch or missing = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-05 — PATCH updates only supplied fields, and the change is stored**
- **Req:** partial update leaves unsent fields intact; result is visible on subsequent GET.
- **Impl:** `store.update` with `model_dump(exclude_unset=True, exclude_none=True)`.
- **Cov:** `test_patch_updates_only_supplied_fields` (criticality; checks owner and OS untouched in the PATCH response).
- **Gap:** each patchable field individually (`ip_address`, `asset_type`, `criticality`, `owner`, `operating_system`, `tags`); GET after PATCH equals the PATCH response.
- **Proc:** parametrize over the six fields: POST, PATCH one field, GET.
- **Exp:** 200; only that field (plus `last_seen`) differs; GET == PATCH response.
- **P/F:** any other field changed, or GET ≠ PATCH response = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-06 — PATCH refreshes `last_seen` and preserves identity**
- **Req:** `last_seen` advances; `id`, `hostname`, `first_seen` never change.
- **Impl:** `model_copy(update={..., "last_seen": now})`.
- **Cov:** `test_patch_refreshes_last_seen` (string `>=` comparison; `first_seen` equal).
- **Gap:** parse datetimes and assert strictly greater; `id` and `hostname` unchanged.
- **Proc:** POST, then PATCH `owner` and compare.
- **Exp:** `last_seen` > previous; others equal.
- **P/F:** identity drift = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-07 — PATCH can clear a nullable field**
- **Req:** `{"operating_system": null}` sets it to null.
- **Impl:** `exclude_none=True` discards explicit nulls.
- **Cov:** none.
- **Gap:** all of it.
- **Proc:** POST with OS set; PATCH `{"operating_system":null}`; GET.
- **Exp:** `operating_system` is null. **CURRENT: 200, value unchanged [observed].**
- **P/F:** value unchanged = FAIL (decision needed: support clearing, or return 422 on explicit null).
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (xfail strict)

**CRUD-08 — PATCH normalizes tags exactly like create**
- **Req:** lowercase, trimmed, de-duplicated, empty tags removed.
- **Impl:** create-only validator (`models.py:80`); `AssetUpdate` has none.
- **Cov:** create only (`test_tags_are_deduplicated_and_lowercased`).
- **Gap:** PATCH path, plus filter consequence.
- **Proc:** POST; PATCH `tags:["PROD","prod"," Web ",""]`; GET; GET `?tag=web`.
- **Exp:** `["prod","web"]`; `?tag=web` returns the asset. **CURRENT: stored verbatim, `?tag=web` misses [observed].**
- **P/F:** unnormalized storage or filter miss = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (xfail strict)

**CRUD-09 — PATCH rejects or explicitly handles non-patchable fields**
- **Req:** client learns `hostname` / `id` cannot be changed.
- **Impl:** extra-ignore on `AssetUpdate`.
- **Cov:** none.
- **Gap:** all of it.
- **Proc:** PATCH `{"hostname":"renamed"}`.
- **Exp:** 422 (or documented ignore). **CURRENT: 200, silently ignored [observed].**
- **P/F:** characterization test pins current; decision needed.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-10 — PATCH with empty body**
- **Req:** defined semantics for `{}`.
- **Impl:** no-op except `last_seen` bump.
- **Cov:** none.
- **Proc:** PATCH `{}`.
- **Exp:** documented. **CURRENT: 200, `last_seen` refreshed [observed].**
- **P/F:** characterization.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-11 — DELETE semantics are complete**
- **Req:** 204 with empty body; later GET, PATCH, DELETE → 404; list and stats exclude it.
- **Impl:** `delete_asset` → `store.delete`.
- **Cov:** `test_delete_then_get_is_404`, `test_delete_unknown_asset_is_404`.
- **Gap:** empty body; second DELETE 404; PATCH after delete 404; list/stats updated.
- **Proc:** POST, DELETE, then GET, PATCH, DELETE, list, stats.
- **Exp:** 204 `b""`, then 404/404/404, absent from list, stats decremented.
- **P/F:** any deviation = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-12 — Hostname is reusable after delete**
- **Req:** uniqueness index is cleaned up on delete.
- **Impl:** `_hostname_index.pop`.
- **Cov:** `test_hostname_is_reusable_after_delete` ✔.
- **Gap:** new id ≠ old id.
- **Proc:** POST, DELETE, POST again.
- **Exp:** 201 with a different id.
- **P/F:** 409, or same id = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-13 — Duplicate hostname → 409, without side effects**
- **Req:** case- and whitespace-variant duplicates are rejected; the original is untouched.
- **Impl:** normalize-then-index lookup under lock.
- **Cov:** exact duplicate only.
- **Gap:** `" WEB-01.CORP.LOCAL "` variant; original record unchanged after the 409; response detail text.
- **Proc:** POST; POST the variant with a different IP; GET the original.
- **Exp:** 409 `asset with hostname '…' already exists`; original IP unchanged.
- **P/F:** 201, or original mutated = FAIL. *(Variant [observed] PASS.)*
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-14 — Stats stay consistent with list across the full lifecycle**
- **Req:** `total`, `by_criticality`, `by_type` always derive from the current records.
- **Impl:** `stats` iterates `store.list()`.
- **Cov:** two creates only.
- **Gap:** empty-store shape; PATCH moving an asset between buckets; DELETE decrements; buckets with 0 are omitted.
- **Proc:** stats on empty store → create 2 → PATCH criticality → DELETE 1, asserting stats at each step.
- **Exp:** empty = `{"total":0,"by_criticality":{},"by_type":{}}`; buckets track changes exactly.
- **P/F:** any count mismatch = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**CRUD-15 — End-to-end lifecycle over real HTTP**
- **Req:** CRUD works through the real server stack (uvicorn, middleware, serialization), not only in-process.
- **Impl:** same routes.
- **Cov:** none automated. Candidate smoke in `deploy.yml` does POST + stats 200 only, and never deletes.
- **Gap:** full lifecycle with cleanup.
- **Proc:** with `RUN_ID=$(uuidgen)`, hostname `fst-$RUN_ID`: POST → GET → list `?tag=$RUN_ID` → PATCH → GET → DELETE → GET 404. A trap deletes on exit.
- **Exp:** 201 → 200 → contains → 200 → reflects patch → 204 → 404.
- **P/F:** any status or body mismatch = FAIL; cleanup failure reported separately.
- Non-destructive · Local ✔ · Cloud Run ✔ (see note) · **I + C**
- **Cloud Run note:** with more than one warm instance, GET or DELETE may hit a different instance than the POST. The C variant must report `INCONSISTENT (multi-instance)` rather than a plain FAIL, and must flag cleanup as unverifiable. See PER-05.

### 4.2 Persistence across restart / replacement

All of these currently **fail by architecture**. That is **expected behavior for an in-memory store**, not a code defect. The tests exist to prove and record the loss, and to become the acceptance gate when persistence is implemented. Each test should assert the *current* behavior (data lost) and report the requirement as `FAIL (architectural)`.

**PER-01 — Survives application process restart**
- **Req:** records survive a process restart.
- **Impl:** module-level dict.
- **Cov:** none.
- **Gap:** all of it.
- **Proc:** start uvicorn on a free port; create 3 assets `per-$RUN_ID-{1..3}` and record their ids; capture stats. SIGTERM, wait for exit, restart with the same env. GET each id; stats; list `?tag=$RUN_ID`; re-POST the same hostname.
- **Exp (target):** all 3 present, stats unchanged, re-POST 409.
- **Exp (current):** all 404, total 0, re-POST **201 with a new id**.
- **P/F:** requirement PASS only if records survive. Test also asserts "shutdown log emitted" and "startup log emitted".
- **Destructive (local process)** · Local ✔ · Cloud Run ✘ · **D**

**PER-02 — Survives dev auto-reload**
- **Req:** awareness only.
- **Impl:** `make run` uses `--reload`.
- **Cov:** none.
- **Proc:** `make run`; create asset; `touch app/version.py`; wait for reload; GET.
- **Exp (current):** 404.
- **P/F:** documentation-only characterization; no gating.
- **Destructive (local)** · Local ✔ · Cloud Run ✘ · **D** (manual, LOW)

**PER-03 — Survives Docker container replacement**
- **Req:** records survive `docker rm -f` + `docker run` of the same image.
- **Impl:** no volume; memory only.
- **Cov:** none (CI runs one container once).
- **Proc:** `make build`; `docker run -d --name ci-per-$RUN_ID -p $PORT:8080`; create assets; `docker rm -f`; run a new container from the same image; GET ids.
- **Exp (current):** all 404, total 0.
- **P/F:** as PER-01. Also capture `docker logs` from both containers.
- **Destructive (local container)** · Local ✔ (Docker now running) · Cloud Run ✘ · **D**

**PER-04 — Survives in-place container restart**
- **Req:** records survive `docker restart`.
- **Proc:** as PER-03, but `docker restart ci-per-$RUN_ID`.
- **Exp (current):** lost (process memory is not preserved across restart).
- **P/F:** as PER-01.
- **Destructive (local container)** · Local ✔ · Cloud Run ✘ · **D**

**PER-05 — Consistent across Cloud Run instances (cold start / scale-out)**
- **Req:** any instance returns the same data.
- **Impl:** per-instance memory; `--min-instances=0`, `--max-instances=3`.
- **Cov:** none.
- **Gap:** all of it. The app exposes **no instance or revision identifier** (see OBS-09), so attribution must come from Cloud Logging `labels.instanceId` on the request log, joined on `X-Request-ID` / trace.
- **Proc (observation only, no traffic changes):** create asset `per-cr-$RUN_ID`; issue N sequential authenticated GETs of its id over time; pull request logs for those requests and group results by `instanceId`.
- **Exp (current):** any request served by an instance other than the one that handled the POST returns 404.
- **P/F:** any 404 for an existing id = FAIL (architectural).
- **Cannot be forced** without load or traffic manipulation. It is recorded only if multiple instances occur naturally. Detailed design is deferred to the persistence prompt.
- Non-destructive by design · Local ✘ · Cloud Run ✔ · **C** (observational)

**PER-06 — Survives new Cloud Run revision deployment**
- **Req:** records survive a normal staged deploy.
- **Impl:** new revision = new empty instances; traffic moves 100% at promotion.
- **Cov:** none.
- **Proc (no bypass):**
  1. **PREPARE:** create `per-rev-$RUN_ID`; record id, current `/version` commit, and the serving revision (`gcloud run services describe … --format='value(status.traffic)'`, read-only).
  2. Wait for the **next normal merge to main** to run the deploy workflow. Do not trigger it for the test.
  3. **VERIFY:** confirm `/version` shows the new commit; GET the id; list `?tag=$RUN_ID`; stats.
- **Exp (current):** 404 and absent. The CI smoke asset `smoke-<run_id>` may be present.
- **P/F:** record survives = PASS; lost = FAIL (architectural).
- **Destructive in effect** (all production data lost), though that is caused by the normal pipeline, not the test · Local ✘ · Cloud Run ✔ · **C/D** (two-phase, human-gated)

**PER-07 — Uniqueness holds across instances**
- **Req:** one hostname = one asset service-wide.
- **Impl:** per-instance index.
- **Cov:** none.
- **Proc:** same observational constraints as PER-05. If two instances are observed, POST the same unique hostname repeatedly and look for more than one 201 across instances.
- **Exp (current):** multiple 201s are possible.
- **P/F:** more than one 201 = FAIL (architectural).
- Non-destructive, but creates duplicate test records that cleanup may not reach · Local ✘ · Cloud Run ✔ · **C** (observational)

### 4.3 Concurrency and data integrity

Single-process results below are all **[observed]** against live uvicorn with parallel httpx clients. **None are automated.**

**CON-01 — Concurrent create of the same hostname**
- **Req:** exactly one succeeds.
- **Impl:** `RLock` around check-and-insert.
- **Cov:** none.
- **Proc:** 50 parallel POSTs with one unique hostname.
- **Exp:** exactly 1×201 and 49×409. **Observed: 1 / 49 ✔**
- **P/F:** ≠1 success = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (threads against `AssetStore`) + **I** (HTTP)

**CON-02 — Concurrent distinct creates**
- **Req:** no lost writes; ids unique.
- **Proc:** 300 parallel POSTs with distinct hostnames tagged `$RUN_ID`; list by tag.
- **Exp:** 300 × 201, 300 listed, 300 unique ids. **Observed ✔**
- **P/F:** any shortfall = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P + I**

**CON-03 — Concurrent PATCHes of different fields**
- **Req:** no field reverted by a stale copy.
- **Impl:** read-copy-write under lock.
- **Proc:** 100 parallel PATCHes alternating `owner` / `operating_system`; GET.
- **Exp:** both fields hold one of the submitted values; all 200. **Observed ✔**
- **P/F:** a field reverted to its original value = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P + I**

**CON-04 — Concurrent DELETE of one id**
- **Req:** exactly one 204.
- **Proc:** 20 parallel DELETEs.
- **Exp:** 1×204, 19×404. **Observed ✔**
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P + I**

**CON-05 — Create/delete churn keeps the index consistent**
- **Req:** no orphaned hostname index entry.
- **Proc:** 200 parallel create-then-delete cycles on one hostname; then list and re-create.
- **Exp:** 0 remaining; re-create 201. **Observed ✔**
- **P/F:** leftover entry, or 409 on re-create = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P + I**

**CON-06 — Reads during writes never error; stats == list**
- **Req:** no `dictionary changed size during iteration`; aggregates consistent.
- **Impl:** `list()` snapshots under lock.
- **Proc:** writer threads plus reader threads on list and stats for about 5 s; then compare stats total to list length.
- **Exp:** zero 5xx; `stats.total == len(list)` at quiesce. **Observed match ✔**
- **P/F:** any 5xx or mismatch = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P + I**

**CON-07 — Integrity across Cloud Run instances**
- Same as PER-05 / PER-07. **FAIL (architectural)** whenever more than one instance serves traffic.
- **C** (observational)

### 4.4 Request validation and HTTP error behavior

**VAL-01 — Missing required fields → 422 naming each field**
- **Impl:** Pydantic.
- **Cov:** `test_missing_required_field_is_rejected` (status only).
- **Gap:** `detail[].loc` includes `ip_address`, `asset_type`, `owner`; one parametrized case per missing field.
- **Proc:** POST the payload minus each required field.
- **Exp:** 422; `["body",<field>]` present with `type:"missing"`.
- **P/F:** wrong status or loc = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**VAL-02 — Invalid enum on POST and PATCH → 422**
- **Impl:** `AssetType` / `Criticality` StrEnum.
- **Cov:** **none.**
- **Proc:** POST `asset_type:"database"`, `criticality:"HIGH"`, `criticality:""`; PATCH `criticality:"bogus"`, `asset_type:"database"`.
- **Exp:** 422 `type:"enum"` with the expected values listed. **Observed ✔**
- **P/F:** non-422 = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✔ (C smoke of one case) · **P**

**VAL-03 — IP address validation and normalization**
- **Impl:** `ipaddress.ip_address()`.
- **Cov:** invalid `999.1.1.1` → 422 ✔; IPv6 accepted ✔.
- **Gap:** IPv6 normalization (`2001:0db8:0000::0001` → `2001:db8::1`); whitespace trimming; leading-zero IPv4 → 422 **[observed]** (the `models.py:62` docstring wrongly says it normalizes); PATCH invalid IP (`models.py:118-123` uncovered).
- **Proc:** parametrized POST and PATCH.
- **Exp:** normalized string, or 422 per case.
- **P/F:** mismatch = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**VAL-04 — Whitespace-only hostname → 422, never 500** · **DEFECT**
- **Impl:** `min_length` checked before strip; store re-validation raises.
- **Cov:** none.
- **Proc:** POST `hostname:"   "`, and also `"\t"`.
- **Exp:** 422. **CURRENT: 500 text/plain [observed].**
- **P/F:** any 5xx = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✔ (C: proves the 500 reaches prod clients) · **P** (xfail strict)

**VAL-05 — Whitespace-only owner rejected** · **DEFECT**
- **Impl:** `NonEmptyStr` without strip.
- **Cov:** none.
- **Proc:** POST `owner:"   "`.
- **Exp:** 422. **CURRENT: 201, stored `"   "` [observed].**
- **P/F:** 201 = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (xfail strict)

**VAL-06 — Hostname format**
- **Impl:** none beyond length.
- **Cov:** none.
- **Proc:** POST `"not a host!!"`.
- **Exp:** decision needed (RFC 1123 check → 422). **CURRENT: 201 [observed].**
- **P/F:** characterization until decided.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**VAL-07 — Unknown fields on create (`environment`)**
- **Impl:** extra-ignore.
- **Cov:** indirect (id test relies on it).
- **Proc:** POST with `environment:"production"` and `foo:1`.
- **Exp:** decision needed (§2a). **CURRENT: 201, fields dropped, no signal [observed].**
- **P/F:** characterization test pins current; flips when decided.
- Non-destructive · Local ✔ · Cloud Run ✔ (C: reproduce the originally observed case) · **P**

**VAL-08 — Wrong JSON types → 422**
- **Cov:** none.
- **Proc:** `hostname:12345`, `tags:"prod"`, `tags:[1,2]`.
- **Exp:** 422. `hostname:12345` **observed 422 ✔**.
- **P/F:** non-422 = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**VAL-09 — Malformed JSON**
- **Impl:** FastAPI default.
- **Cov:** none.
- **Proc:** POST and PATCH body `{not json` with `application/json`.
- **Exp:** documented contract. **CURRENT: 422 `json_invalid` (not 400) [observed].**
- **P/F:** 5xx = FAIL; otherwise characterization.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**VAL-10 — Non-object body / wrong content type**
- **Cov:** none.
- **Proc:** JSON array body; JSON with `Content-Type: text/plain`; empty body.
- **Exp:** 4xx, never 5xx. **CURRENT: 422 for all three [observed].**
- **P/F:** 5xx = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**VAL-11 — Field and body size bounds**
- **Impl:** hostname/owner ≤ 253; nothing else bounded; no body limit.
- **Cov:** none.
- **Proc:** 254-char hostname; 10k-char OS; 5,000 tags; 1 MB body.
- **Exp:** hostname 422; others decision needed. **CURRENT: OS and tags accepted [observed].**
- **P/F:** hostname 254 must be 422; the rest is characterization.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**VAL-12 — Update validation parity with create** · **DEFECT (owner)**
- **Impl:** `AssetUpdate` lacks `NonEmptyStr` and the tag validator.
- **Cov:** none (update validator uncovered).
- **Proc:** PATCH `owner:""`, `owner:"   "`, `ip_address:"999.1.1.1"`.
- **Exp:** 422 for all. **CURRENT: owner `""` → 200 [observed]; invalid IP → 422 ✔.**
- **P/F:** 200 = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (xfail strict for owner)

**VAL-13 — Missing resource → 404 with a stable body**
- **Cov:** status only for GET, PATCH, DELETE.
- **Gap:** body `{"detail":"asset not found"}`; JSON content type.
- **Proc:** GET, PATCH, DELETE of `uuid4()`.
- **Exp:** 404 with that body.
- **P/F:** mismatch = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✔ · **P + C**

**VAL-14 — Unknown route 404; unsupported method 405**
- **Cov:** none.
- **Proc:** GET `/api/v1/nope`; PUT `/api/v1/assets`; POST `/api/v1/assets/{id}`.
- **Exp:** 404 / 405 / 405 with JSON `detail` **[observed 404, 405 ✔]**.
- **P/F:** mismatch = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✔ (C: confirm 404 is FastAPI's JSON, not Google's HTML frontend) · **P + C**

**VAL-15 — Error body shape inventory**
- **Req:** documented and consistent.
- **Cov:** none.
- **Proc:** collect bodies for 404, 405, 409, 422, 500.
- **Exp:** documented. **CURRENT:** 404/405/409 `{"detail": str}`; 422 `{"detail": [..]}`; 500 `text/plain` **[observed]**.
- **P/F:** characterization. The 500 case feeds RUN-01.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

### 4.5 List / filter / sort / pagination

**LST-01 — Unfiltered list returns all records, sorted by hostname**
- **Cov:** `test_list_ordering_is_stable` ✔; count ✔.
- **Gap:** empty store → `[]`.
- **Proc:** empty list; 3 inserts in non-sorted order.
- **Exp:** `[]`; then sorted.
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**LST-02 — `criticality` filter (valid values)**
- **Cov:** `high` ✔.
- **Gap:** all 4 values parametrized; the default `medium` is filterable.
- **Proc:** seed one asset per value; filter each.
- **Exp:** exactly the matching asset.
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**LST-03 — `asset_type` filter (valid values)**
- **Cov:** **none** (`store.py:70` uncovered).
- **Proc:** seed one per type; filter each of the 5.
- **Exp:** exactly the match. **Observed `server` ✔**
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**LST-04 — `tag` filter is case- and whitespace-insensitive**
- **Cov:** lowercase exact only.
- **Gap:** `?tag=PROD`, `?tag=%20prod%20`.
- **Exp:** matches. **Observed `PROD` ✔**
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**LST-05 — Invalid `criticality` / `asset_type` → 422** · **DEFECT**
- **Impl:** `str | None` params.
- **Cov:** none.
- **Proc:** `?criticality=bogus`, `?asset_type=bogus`, `?asset_type=database`.
- **Exp:** 422 `type:"enum"`, matching POST. **CURRENT: 200 `[]` [observed].**
- **P/F:** 200 = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✔ (C: reproduce as originally observed) · **P** (xfail strict)

**LST-06 — Case handling for enum filters is defined** · **DEFECT (same root cause)**
- **Proc:** `?criticality=HIGH` with a `high` asset present.
- **Exp:** 422 (strict), or a match if case-insensitive is chosen. **CURRENT: 200 `[]` [observed]** — the worst option, a silent false negative.
- **P/F:** 200 `[]` = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (xfail strict)

**LST-07 — Combined filters use AND semantics**
- **Cov:** none.
- **Proc:** seed a 2×2 of criticality × type plus tags; query combinations.
- **Exp:** intersection only.
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**LST-08 — Empty filter value**
- **Cov:** none.
- **Proc:** `?criticality=`.
- **Exp:** documented. **CURRENT: treated as no filter [observed].**
- **P/F:** characterization.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**LST-09 — Sort parameter**
- **Impl:** **not implemented**; unknown params silently ignored.
- **Cov:** none.
- **Proc:** `?sort=-hostname`.
- **Exp:** decision needed. **CURRENT: ignored, hostname-ascending [observed].**
- **P/F:** characterization (prevents a client silently believing sort works).
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**LST-10 — Pagination**
- **Impl:** **not implemented**; unbounded response.
- **Cov:** none.
- **Proc:** `?limit=1&offset=0` with 3 assets; also time a list of 5,000 assets.
- **Exp:** decision needed. **CURRENT: all returned [observed].**
- **P/F:** characterization, plus a performance guard (list of 5,000 under a chosen threshold, e.g. 1 s locally).
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

### 4.6 Application / runtime failure behavior

**RUN-01 — Unhandled exception → 500 that is logged and correlatable**
- **Impl:** middleware logs `unhandled exception` then re-raises (`main.py:109-115`).
- **Cov:** none (uncovered lines).
- **Proc:** monkeypatch `AssetStore.list` to raise; GET list with `raise_server_exceptions=False`; capture logs.
- **Exp:** 500; ERROR JSON log with `request_id`, `path`, `exception`; response has `X-Request-ID` and `X-Build-Commit`. **CURRENT: log ✔; headers missing; body `text/plain` [observed].**
- **P/F:** no ERROR log = FAIL; missing headers = FAIL (xfail strict).
- Non-destructive (fault injected in-process only) · Local ✔ · Cloud Run ✘ · **P**

**RUN-02 — Client input can never produce a 5xx**
- **Req:** fuzz-style guard.
- **Cov:** none.
- **Proc:** parametrized battery of hostile inputs (whitespace, control chars, unicode, huge strings, nulls, wrong types) across POST and PATCH.
- **Exp:** only 2xx or 4xx. **CURRENT: VAL-04 produces 500.**
- **P/F:** any 5xx = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**RUN-03 — Startup emits a provenance log; missing provenance emits a warning**
- **Impl:** `lifespan` (`main.py:67-83`).
- **Cov:** **none** — `conftest.client` builds `TestClient(app)` without `with`, so lifespan never runs in the suite.
- **Proc:** `with TestClient(app)` plus log capture, with and without `GIT_COMMIT`.
- **Exp:** INFO `service starting` with `event:"startup"` and commit fields; WARNING `provenance_missing` when unset; INFO `service stopping` on exit.
- **P/F:** missing event = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✔ (C: read-only log query for the current revision's first line) · **P + C**

**RUN-04 — Graceful shutdown on SIGTERM in the container**
- **Impl:** exec-form CMD makes uvicorn PID 1 (`Dockerfile:132`).
- **Cov:** none.
- **Proc:** run the container; start a slow stream of requests; `docker stop -t 10`; inspect logs and exit code.
- **Exp:** `service stopping` logged; exit code 0 (not 137); in-flight requests complete.
- **P/F:** exit 137 or no shutdown log = FAIL.
- **Destructive (local container)** · Local ✔ · Cloud Run ✘ · **D**

**RUN-05 — Container honors `$PORT`**
- **Impl:** `sh -c exec uvicorn … --port ${PORT}`.
- **Cov:** CI runs port 8080 only.
- **Proc:** `docker run -e PORT=9090 -p 9090:9090`; GET `/health`.
- **Exp:** 200 on 9090.
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **I**

**RUN-06 — Behavior under memory pressure**
- **Impl:** unbounded store; 512Mi on Cloud Run.
- **Cov:** none.
- **Proc:** local container with `--memory=512m`; bulk-create large payloads until failure; observe OOM kill and restart.
- **Exp:** documents the ceiling; after OOM, data is lost (PER-04).
- **P/F:** characterization; records asset count at failure.
- **Destructive (local container)** · Local ✔ · Cloud Run ✘ (do **not** run on Cloud Run) · **D** (LOW, optional)

### 4.7 Health / readiness semantics

**HLT-01 — `/health` and `/ready` on the real paths**
- **Impl:** both registered with `z` aliases.
- **Cov:** tests call **only `/healthz` and `/readyz`**, the aliases that are unreachable on Cloud Run. The canonical `/health` and `/ready` have no test.
- **Proc:** GET all four locally.
- **Exp:** 200 with identical bodies per pair.
- **P/F:** any difference = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**HLT-02 — Liveness independent of dependencies**
- **Cov:** `test_healthz_does_not_depend_on_the_store` ✔ (alias path).
- **Gap:** also `/health`.
- **Proc:** monkeypatch `count` to raise; GET `/health`.
- **Exp:** 200 ok. **Observed ✔**
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**HLT-03 — Readiness reports dependency checks**
- **Cov:** `test_readyz_reports_dependencies` ✔ (alias).
- **Gap:** `/ready`.
- **Exp:** `checks.store == "ok"`.
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**HLT-04 — Not-ready → HTTP 503** · **DEFECT (latent)**
- **Impl:** degraded path returns a 200 model; marked `# pragma: no cover`.
- **Cov:** none.
- **Proc:** monkeypatch `count` to raise; GET `/ready`.
- **Exp:** 503 with `status:"degraded"`. **CURRENT: 200 [observed].**
- **P/F:** 200 = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (xfail strict)

**HLT-05 — Readiness is not tautological**
- **Req:** the check can actually detect failure.
- **Impl:** `dict.__len__` cannot fail.
- **Cov:** n/a.
- **Proc:** code inspection plus HLT-04.
- **Exp:** documented as "no real dependency yet."
- **P/F:** informational; becomes a real test with a DB.
- n/a · **doc**

**HLT-06 — Cloud Run: ops endpoints over authenticated private access**
- **Impl:** `--no-allow-unauthenticated --invoker-iam-check`.
- **Cov:** deploy workflow checks `/health`, `/ready`, `/version` for candidate and production ✔.
- **Gap:** a repeatable operator-run script outside the deploy; unauthenticated request returns 403 (read-only regression check that IAM is still enforced; changes nothing); `/healthz` returns Google-frontend 404 (documented).
- **Proc:** `TOKEN=$(gcloud auth print-identity-token)`; authenticated GETs; one GET without a token.
- **Exp:** 200/200/200 with a matching commit; unauthenticated **403**; `/healthz` 404 non-JSON.
- **P/F:** any deviation = FAIL.
- Non-destructive · Local ✘ · Cloud Run ✔ · **C**

**HLT-07 — OpenAPI schema generated and lists all routes**
- **Cov:** `test_openapi_schema_is_generated` ✔ (one path).
- **Gap:** all 5 asset operations + `/health`, `/ready`, `/version`; `/docs` returns 200.
- **Exp:** present.
- **P/F:** missing = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

### 4.8 Logging and operational observability

**OBS-01 — One structured request log per request**
- **Impl:** middleware `request handled`.
- **Cov:** **none** asserting log content.
- **Proc:** capture logs for GET, POST, 404, and 422 requests.
- **Exp:** exactly one JSON line each with `severity`, `request_id`, `method`, `path`, `status`, `duration_ms`; `request_id` equals the response `X-Request-ID`.
- **P/F:** missing or mismatched = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✔ (C: log query) · **P + C**

**OBS-02 — `X-Request-ID` on every response class**
- **Cov:** `/healthz` 200 only.
- **Gap:** 201, 204, 404, 409, 422, **500**.
- **Exp:** present on all. **CURRENT: missing on 500 [observed].**
- **P/F:** missing = FAIL (xfail strict for 500).
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**OBS-03 — `X-Build-Commit` on every response class**
- **Cov:** `/healthz` only.
- **Gap:** as OBS-02.
- **Exp:** present. **CURRENT: missing on 500.**
- **P/F:** as OBS-02.
- Non-destructive · Local ✔ · Cloud Run ✔ (C: equals `/version` commit_short) · **P + C**

**OBS-04 — Trace correlation with Cloud Run**
- **Impl:** reuses the `X-Cloud-Trace-Context` trace id as `request_id`.
- **Cov:** `test_trace_header_is_reused_when_present` ✔.
- **Gap:** W3C `traceparent` not handled; logs do not emit `logging.googleapis.com/trace`, so Cloud Logging will **not** nest app logs under the platform request log.
- **Proc:** P: send `traceparent` only. C: query Cloud Logging for an app log sharing the platform request's trace.
- **Exp:** decision needed. **CURRENT: `traceparent` ignored (new uuid) [code]; no native trace field [code].**
- **P/F:** characterization.
- Non-destructive · Local ✔ · Cloud Run ✔ · **P + C**

**OBS-05 — Domain audit events for every mutation** · **DEFECT**
- **Impl:** `asset_created` only.
- **Cov:** none.
- **Proc:** capture logs across POST → PATCH → DELETE, and a 409.
- **Exp:** `asset_created`, `asset_updated` (id + changed field names), `asset_deleted` (id + hostname), and optionally `asset_conflict`. **CURRENT: only `asset_created` [observed].**
- **P/F:** missing event = FAIL (xfail strict).
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**OBS-06 — Error logs carry severity and exception**
- **Impl:** `log.exception` + `exc_info` formatting (`main.py:50-51`).
- **Cov:** none (line 51 uncovered).
- **Proc:** as RUN-01; parse the log line.
- **Exp:** `severity:"ERROR"`, `exception` contains the traceback, `request_id` present.
- **P/F:** otherwise FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**OBS-07 — Single log format on stdout**
- **Impl:** root handler replaced, but uvicorn's own `uvicorn.access` / `uvicorn.error` loggers still print plain text.
- **Cov:** none.
- **Proc:** run uvicorn; count non-JSON stdout lines per request.
- **Exp:** decision needed. **CURRENT: one plain-text access line duplicates each JSON request line [observed].** On Cloud Run these become severity-less `textPayload` entries.
- **P/F:** characterization.
- Non-destructive · Local ✔ · Cloud Run ✔ (log query) · **I**

**OBS-08 — Request bodies and query strings are not logged**
- **Impl:** path only.
- **Cov:** none.
- **Proc:** POST with a distinctive owner value; GET with a distinctive query; grep captured logs.
- **Exp:** value absent. **Observed ✔ [code]**
- **P/F:** present = FAIL.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P**

**OBS-09 — Response is attributable to a revision or instance**
- **Impl:** none. `/version` has no `K_REVISION`; no instance id.
- **Cov:** none.
- **Proc:** C: GET `/version`; compare to `gcloud run services describe` (read-only).
- **Exp:** decision needed. **CURRENT: revision is not visible from the API [code].** Needed evidence for PER-05, PER-06, PER-07 must come from Cloud Logging labels.
- **P/F:** characterization.
- Non-destructive · Local ✘ · Cloud Run ✔ · **C**

**OBS-10 — Log timestamps are precise enough to order events**
- **Impl:** `%Y-%m-%dT%H:%M:%S%z` (whole seconds, process-local tz).
- **Cov:** none.
- **Proc:** inspect format.
- **Exp:** decision needed. **CURRENT: second resolution [observed].**
- **P/F:** characterization.
- Non-destructive · Local ✔ · Cloud Run ✘ · **P** (LOW)

### 4.9 Cross-cutting

| ID | Item | Evidence | Action |
|---|---|---|---|
| X-01 | CI candidate smoke leaves `smoke-<run_id>` in the revision promoted to production | `deploy.yml:224-232` (no DELETE) **[code]**; presence in prod **[inferred]** | C script GETs the full list (read-only), filters client-side for `owner == "ci"` / `smoke-*` hostnames, and reports them; **do not** delete records it did not create |
| X-02 | Uncovered code paths | `main.py` 51, 69-83, 109-115, 228, 279; `models.py` 118-123; `store.py` 53-55, 70 | Covered by RUN-01, RUN-03, OBS-06, VAL-03, LST-03 |
| X-03 | Dead code: app-level `DuplicateAssetError` handler (`main.py:277`) is unreachable because the route catches the error first; `store.get_by_hostname` unused | **[code]** | Note only |
| X-04 | Docs drift: `main.py` docstring says `/healthz` is "for Cloud Run"; `models.py:62` says leading zeros are normalized (they are rejected); README "Status" shows only Phase 1 done; `docs/branch-protection-validation.md` is a temporary file still on main | **[code]/[observed]** | Note only |
| X-05 | `first_seen` ≠ `last_seen` at creation (two `datetime.now()` calls, µs apart) | **[observed]** `…112824Z` vs `…112825Z` | Characterization test; LOW |
| X-06 | Local 3.14 vs CI 3.11 | venv | Results are strong evidence, not identical; CI is authoritative |

---

## 5. Proposed automated test structure

Design rules:
- **`make test` stays exactly as CI runs it today.** New in-process tests join it and keep CI fast and hermetic.
- **Known defects are encoded as `@pytest.mark.xfail(strict=True, reason="DEFECT <ID>: …")`.** The suite stays green on protected `main`, the defect stays visible in every run summary, and a fix turns the test into XPASS → **failure**, forcing someone to remove the marker. No CI control is weakened, and nothing is hidden with skips.
- **Contract decisions still pending** (VAL-06, VAL-07, CRUD-09, LST-09, LST-10, OBS-04, OBS-07) get **characterization tests** named `test_current_…` that assert today's behavior, so a change is deliberate.
- Integration, Cloud Run, and destructive tests **never** run under plain `pytest`. They are gated by markers plus an explicit env var, so CI cannot accidentally hit a network or restart anything.

```
pyproject.toml                         # register markers only (required by --strict-markers):
                                       #   integration, cloudrun, destructive, characterization
tests/
  conftest.py                          # existing; ADD fixtures:
                                       #   lifespan_client  -> `with TestClient(app)` (runs startup/shutdown)
                                       #   log_records      -> captures JSON log records per test
                                       #   unique_host      -> f"t-{uuid4().hex[:10]}"
  test_assets_api.py                   # existing, untouched
  test_ops.py                          # existing, untouched
  test_version.py                      # existing, untouched
  contract/
    test_crud_contract.py              # CRUD-01..06, 11..14
    test_update_semantics.py           # CRUD-07..10, VAL-12            (xfail: CRUD-07, CRUD-08, VAL-12 owner)
    test_create_validation.py          # VAL-01..11                     (xfail: VAL-04, VAL-05)
    test_http_errors.py                # VAL-13..15, RUN-01, RUN-02     (xfail: RUN-01 headers, RUN-02)
    test_list_filters.py               # LST-01..10                     (xfail: LST-05, LST-06)
    test_health_semantics.py           # HLT-01..04, HLT-07             (xfail: HLT-04)
    test_observability.py              # OBS-01..06, 08, 10, RUN-03     (xfail: OBS-02/03 on 500, OBS-05)
    test_store_concurrency.py          # CON-01..06 with threads against AssetStore + TestClient
  integration/                         # @pytest.mark.integration; skipped unless CI_BASE_URL is set
    conftest.py                        # httpx.Client(base_url=CI_BASE_URL), RUN_ID, auto-cleanup registry
    test_live_lifecycle.py             # CRUD-15
    test_live_concurrency.py           # CON-01..06 over real HTTP
scripts/tests/
  lib.sh                               # RUN_ID, check "<name>" <expected> <actual>, summary, EXIT trap cleanup
  local-integration.sh                 # starts uvicorn on a free port -> pytest -m integration -> RUN-05 docker
                                       #   $PORT check -> OBS-07 stdout scan -> teardown. Non-destructive.
  cloudrun-functional.sh               # C tests: HLT-06, CRUD-15, VAL-04/07/13/14, LST-05, OBS-03, X-01 report.
                                       #   Token via gcloud auth print-identity-token. Only GET/POST/PATCH/DELETE
                                       #   on records it created (hostname prefix cr-<RUN_ID>). Read-only gcloud
                                       #   describe. Never update-traffic / deploy / IAM / branch protection.
  destructive/
    persistence-local.sh               # PER-01, PER-03, PER-04, RUN-04 — refuses to run without --i-understand
    persistence-cloudrun.sh            # PER-05/06/07 as `prepare` and `verify` subcommands around a NORMAL deploy
Makefile                               # ADD targets only: test-integration, test-cloudrun, test-destructive
```

Every script prints `TEST <ID> <name>` / `expected: …` / `actual: …` / `PASS|FAIL|INCONSISTENT`, exits non-zero on any FAIL, and ends with a per-ID summary.

---

## 6. Blind spots in this audit

- Nothing was executed against Cloud Run. Every Cloud Run statement is **[inferred]** from `deploy.yml` and platform behavior until the C scripts run.
- The concurrency results are from one uvicorn worker on macOS. The container uses the same single-worker command, so they should transfer, but that is not verified in Docker.
- The "runbook" you mentioned as naming persistence the next validation is not in this repo: `PHASES.md` Phase 8 "Consolidated runbook" is an empty *pending* heading. I relied on your description for that.
