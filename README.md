# Cyber Inventory Service

An asset inventory API built to demonstrate a full commit-to-production
pipeline: build → test → containerize → deploy, automated end to end.

The domain is intentionally small. The point of this repository is the
**pipeline and the traceability**, not the CRUD.

## The traceability chain

The core idea: a running container should be able to tell you exactly which
commit produced it.

```
git commit  →  GitHub Actions  →  docker build --build-arg GIT_COMMIT=$SHA
            →  container ENV    →  GET /version
```

Three places surface the running commit:

| Where | How |
|---|---|
| `GET /version` | JSON with commit, build time, environment |
| Any response | `X-Build-Commit` header |
| Startup logs | First structured log line of every revision |

If `/version` ever reports `"commit": "unknown"` in a deployed environment,
the chain is broken — the image was not built by CI.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness — process is up. Checks nothing external. |
| GET | `/ready` | Readiness — dependencies are usable. |
| GET | `/version` | Build provenance. |
| POST | `/api/v1/assets` | Register an asset. 409 on duplicate hostname. |
| GET | `/api/v1/assets` | List, filterable by `criticality`, `asset_type`, `tag`. |
| GET | `/api/v1/assets/{id}` | Fetch one. |
| PATCH | `/api/v1/assets/{id}` | Partial update. |
| DELETE | `/api/v1/assets/{id}` | Remove. 204 on success. |
| GET | `/api/v1/stats` | Counts by criticality and type. |

`/healthz` and `/readyz` are registered as aliases for Kubernetes
compatibility, but **are unreachable on Cloud Run** — its frontend reserves
paths ending in "z". See the note in `app/main.py`.

Interactive docs at `/docs` when running.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# run it
.venv/bin/uvicorn app.main:app --reload --port 8080

# with fake provenance, to see /version populated
GIT_COMMIT=$(git rev-parse HEAD) \
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
.venv/bin/uvicorn app.main:app --port 8080
```

```bash
curl localhost:8080/version
curl -X POST localhost:8080/api/v1/assets \
  -H 'Content-Type: application/json' \
  -d '{"hostname":"web-01","ip_address":"10.0.0.5","asset_type":"server","criticality":"high","owner":"platform-team","tags":["prod"]}'
```

## Design notes

**Liveness and readiness are separate on purpose.** A liveness probe that
checks a database will restart healthy containers during a database blip,
escalating a partial outage into a total one. Dependency checks belong in
`/readyz`.

**Logs are structured JSON.** Cloud Logging parses stdout as structured data
and treats `severity` specially. Plain-text logs cost you level filtering and
field search exactly when you need them — during an incident.

**Storage is an in-memory dict behind a narrow interface.** A real database
would add migrations, pooling, fixtures, and a Cloud SQL instance, none of
which teach anything about the pipeline. The interface is small enough that
swapping backends later is a contained change.

## Status

- [x] Phase 1 — service skeleton with provenance
- [ ] Phase 2 — tests and lint
- [ ] Phase 3 — container
- [ ] Phase 4 — CI
- [ ] Phase 5 — CD to Cloud Run
- [ ] Phase 6 — branch protection and review
- [ ] Phase 7 — failure drills
