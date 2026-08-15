# Phase Playbook

A running record of what each phase produces, why it exists, and what went
wrong along the way — written so you can rebuild this from an empty folder.

**Project goal:** build, test, containerize, and deploy a cyber inventory
service through an automated pipeline.

**Learning goals this serves:**

1. Source control and peer review practices across teams
2. Diagnose failures and trace a change from commit → build → deploy → production
3. Build community across the cohort

The domain (asset inventory) is a vehicle. The pipeline is the subject.

---

## The 20-minute recreation checklist

If you're rebuilding from scratch under time pressure, this is the order.
Details for each step are in the phase sections below.

```
[ ] git init, .gitignore, first commit on main
[ ] Service with /healthz, /readyz, /version   ← provenance FIRST, not last
[ ] pyproject.toml: pytest pythonpath, ruff, coverage floor
[ ] Tests, including one that asserts provenance is present
[ ] Multi-stage Dockerfile, non-root, $PORT, build args
[ ] .dockerignore (before your first build, not after)
[ ] Makefile so humans and CI run identical commands
[ ] GitHub repo (private), SSH auth
[ ] CI workflow: lint → test → build → verify provenance → scan
[ ] Pin every action to a commit SHA + Dependabot
[ ] Branch protection: no direct pushes to main
[ ] CD: Workload Identity Federation → Artifact Registry → Cloud Run
[ ] Smoke test that fails if /version says "unknown"
```

**The single highest-leverage decision:** build the traceability plumbing in
the first commit. Everything downstream has to preserve it, and retrofitting
it means touching every stage again.

---

## Phase 1 — Service skeleton with provenance

### Produces

| File | Purpose |
|---|---|
| `app/version.py` | Reads build provenance from env vars |
| `app/models.py` | Pydantic models + validation |
| `app/store.py` | Thread-safe in-memory storage behind a narrow interface |
| `app/main.py` | FastAPI app, ops endpoints, structured logging, middleware |
| `requirements.txt` / `requirements-dev.txt` | Pinned dependencies |
| `.gitignore` | Keeps caches, venvs, and `.env` out of Git |
| `README.md` | What it is and how to run it |

### Why

**Provenance first.** The chain is:

```
git commit → CI → docker build --build-arg → container ENV → GET /version
```

Every stage has to carry the commit SHA forward. Design it in at the start or
you retrofit all four stages later.

Three places surface the running commit: the `/version` endpoint, an
`X-Build-Commit` header on every response, and a structured log line at
startup. Default is the string `"unknown"` — if you ever see that in a
deployed environment, the image wasn't built by CI.

**Liveness and readiness are separate endpoints.** `/healthz` checks nothing
external; `/readyz` is where dependency checks go. Put a database check in
liveness and a brief database blip makes the platform restart every healthy
container — a partial outage becomes a total one.

**Logs are JSON, not text.** Cloud Logging parses stdout as structured data
and treats `severity` specially. Text logs cost you level filtering and field
search exactly when you need them.

**Storage is a dict behind an interface.** A real database adds migrations,
pooling, fixtures, and a Cloud SQL instance — none of which teach the
pipeline. Knowing what to leave out of a two-week build is a real skill.

### Gotcha hit

`AssertionError: Status code 204 must not have a response body`. FastAPI
refuses to build a response model for a 204 because HTTP forbids a body.
Annotating `-> None` isn't enough; pass `response_class=Response`.

Note it failed at **import time**, not request time. Loud early failures are a
gift.

### Verify

```bash
GIT_COMMIT=$(git rev-parse HEAD) uvicorn app.main:app --port 8080
curl localhost:8080/version   # commit must not be "unknown"
```

---

## Phase 2 — Tests and lint

### Produces

| File | Purpose |
|---|---|
| `tests/conftest.py` | Shared fixtures; auto-resets state between tests |
| `tests/test_version.py` | Provenance tests — the most valuable ones here |
| `tests/test_assets_api.py` | API behavior and validation |
| `tests/test_ops.py` | Health, readiness, OpenAPI schema |
| `pyproject.toml` | pytest, coverage, and ruff configuration |
| `GLOSSARY.md` | Plain-English definitions |

### Why

Tests are the peer-review gate. A reviewer sees green or red before reading a
line of code.

**Every test exists because a specific realistic bug would slip through
without it.** Worth stealing:

- Liveness stays green when the data store is broken — encodes the outage
  scenario so nobody can undo it accidentally
- Partial update doesn't wipe unsent fields (the `exclude_unset` bug)
- Hostname is reusable after delete (uniqueness index cleanup)
- Empty-string env var counts as missing, not as a valid commit
- List ordering is stable (prevents a whole class of flaky test)

**`autouse=True` on the store-reset fixture.** The store is a module-level
singleton that survives between tests. Without a reset, test A's leftovers
change test B's result and pass/fail starts depending on run order.

**Coverage floor of 85% is a floor, not a goal.** You can hit 100% with tests
that assert nothing.

### Gotchas hit

**`No module named 'app'`** — pytest doesn't put the project root on the
import path. Fix: `pythonpath = ["."]` under `[tool.pytest.ini_options]`.

**Linter flagged FastAPI's own idiom.** Rule B008 forbids function calls in
argument defaults — good advice generally, but `Depends()` in a default *is*
the FastAPI pattern. Disabled it **with a written reason**. A rule you
disable without explaining is a rule the next person re-enables.

### Verify

```bash
ruff check . && pytest --cov=app --cov-report=term-missing
```

---

## Phase 3 — Containerize

### Produces

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build, non-root, provenance via build args |
| `.dockerignore` | Keeps `.git`, `.venv`, secrets out of the build context |
| `Makefile` | Same commands for humans and CI |
| `docker-compose.yml` | Local run with a healthcheck |

### Why

**Two stages.** Stage one has compilers and package tooling; stage two starts
clean and copies only the finished virtualenv. Anything left in a shipped
image is attack surface and CVEs for a scanner to flag.

**Dependency install before code copy — the biggest build-speed lever there
is.** Docker reuses cached layers when inputs are unchanged. Copy
`requirements.txt` and install *before* copying app code, and editing a Python
file won't reinstall every package. Get it backwards and a 5-second build
becomes 2 minutes, on every commit, forever.

**Non-root user.** Containers default to root. Code execution as root inside a
container is a much better launching pad for host escape. Three lines to fix,
and every scanner flags it.

**`.dockerignore` is security, not just speed.** Files added in one layer stay
recoverable in image history even if a later layer deletes them. A leaked
`.env` in an image is permanent — you can't `rm` your way out.

**Listen on `$PORT`, never hardcoded.** Cloud Run injects it. Hardcoding 8080
works only because that's the current default.

**`exec` in the CMD.** Without it uvicorn runs as a child of a shell, the
shell doesn't forward SIGTERM, the container ignores shutdown, gets
force-killed after the grace period, and drops in-flight requests.

### Gotcha hit

Container registries were blocked from the build environment — `docker build`
couldn't even pull the base image. Exactly the corporate egress problem to
expect on a locked-down network. Lint with `hadolint` when you can't build.

### Verify

```bash
make build && make run-container
make smoke     # fails if /version reports "unknown"
```

---

## Phase 4 — CI pipeline

### Produces

| File | Purpose |
|---|---|
| `.github/workflows/ci.yml` | Lint → test → build → verify provenance → scan |
| `.github/dependabot.yml` | Update PRs for pinned actions and dependencies |

### Why

**Two jobs, `container` needs `quality`.** Don't spend a container build on
code that failed its tests.

**The provenance verification step is the centerpiece.** CI starts the image
it just built, curls `/version`, and asserts the reported commit equals
`github.sha`. That's the entire commit → production chain, checked on every
push. Without it, provenance breaks silently and you find out mid-incident.

**Poll, don't sleep.** A fixed `sleep 10` is either too short (flaky) or too
long (slow) and is never right on both the fastest and slowest runner.

**`permissions: contents: read`.** GitHub's default token is broader than the
job needs, and anything running in the job inherits it. Never interpolate
attacker-controlled text (PR titles, branch names) into a `run:` block — it
goes straight into a shell. `github.sha` is safe; a PR title isn't.

**`ignore-unfixed: true` on the scanner.** A CVE with no patch can't be fixed
by this repo. A scanner that reports unfixable findings every run trains
people to ignore it — costing you the real findings too.

### Gotcha hit — the big one

The pinned Trivy version didn't exist, and investigating why surfaced a live
supply chain attack:

> In March 2026, attackers force-pushed **76 of 77 version tags** of
> `aquasecurity/trivy-action` to malicious commits. The code harvested runner
> environment variables, read `Runner.Worker` process memory to extract masked
> secrets, and exfiltrated to a typosquatted domain. Only tag `0.35.0` is
> unaffected.

**A tag is a label, and labels move.** Whoever controls an action's repo can
re-point `v4` at new code that runs on your runner with your secrets — no
change to your files, no notification. A commit SHA cannot be moved.

So every action is pinned:

```yaml
uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
uses: aquasecurity/trivy-action@57a97c7e7821a5776cebc9bb87c984fa69cba8f1  # 0.35.0
```

Resolve SHAs yourself, don't copy them from a blog post:

```bash
git ls-remote https://github.com/actions/checkout.git 'refs/tags/v4.2.2^{}'
```

**Dependabot exists to fix what pinning breaks.** Pinning stops silent
updates — the point — but also stops security patches. Dependabot turns an
upstream change into a reviewable PR. Both halves: nothing changes without
approval, and you still find out.

Worth noticing: the compromised package *was the security scanner*. Supply
chain attacks target what everyone trusts and nobody inspects.

### Verify

Open a PR. Checks should run and go green. Confirm the "Verify build
provenance" step prints matching reported/expected SHAs.

---

## Phase 5 — Deploy to Cloud Run *(pending)*

Planned: Workload Identity Federation for keyless auth to GCP, push to
Artifact Registry, deploy to Cloud Run, smoke test the live revision.

Key idea to preview: WIF means **no service account JSON key in GitHub
secrets**. GitHub proves its identity to Google directly and receives a
short-lived token. A leaked long-lived key is the most common cloud breach
vector there is.

---

## Phase 6 — Source control and review *(pending)*

Planned: branch protection, CODEOWNERS, PR template, conventional commits,
and the branching model the cohort agrees on.

---

## Phase 7 — Failure drills *(pending)*

Planned: deliberately break each pipeline stage and practice walking the
commit → production trace to find it. Directly serves learning goal 2.

---

## Phase 8 — Consolidated runbook *(pending)*

---

## Appendix: every failure we hit, and the fix

A real record. Expect most of these again during the boot camp.

| Failure | Cause | Fix |
|---|---|---|
| `204 must not have a response body` | FastAPI builds a response model for a 204 | `response_class=Response` |
| `No module named 'app'` | Project root not on pytest's import path | `pythonpath = ["."]` |
| Linter flags `Depends()` | B008 false positive on FastAPI idiom | Ignore the rule, write down why |
| `docker build` can't pull base image | Registry egress blocked | Lint with hadolint; build where egress works |
| `unzip` overwrote a repo | Archive had a top-level folder matching an existing dir | Extract to a scratch dir with `-d` |
| Commands ran after a failure | Separate lines, not `&&` chained | Chain dependent steps with `&&` |
| `Repository not found` on push | Repo not created yet on GitHub | Create it first; `git remote add` creates nothing |
| `refusing to allow a PAT ... without workflow scope` | Tokens need explicit `workflow` scope to touch `.github/workflows/` | Add the scope, or use SSH |
| `Permission denied (publickey)` | No SSH key registered on the account | `ssh-keygen`, add the `.pub` to GitHub |
| `Unable to resolve action ... version` | Version tag doesn't exist | Verify the tag; then pin the SHA |

### Meta-lessons

- **Check whether a thing exists before assuming you lack permission.**
  "Repository not found" is also what GitHub returns for a private repo you
  can't see — deliberately, so nobody can probe for private repos.
- **Failures that fail loudly and early are cheap. Silent ones are expensive.**
- **When a command chain half-works, read the whole output, not the last line.**

---

## Appendix: commands

```bash
# development
make install          # venv + dev dependencies
make test             # pytest with coverage
make lint             # ruff
make run              # local server with provenance

# container
make build            # image stamped with current commit
make run-container
make smoke            # alive AND traceable

# resolving an action SHA for pinning
git ls-remote https://github.com/OWNER/REPO.git 'refs/tags/TAG^{}'

# the branch → PR → merge loop
git checkout -b feat/thing
git add -A && git commit -m "feat: thing"
git push -u origin feat/thing
gh pr create --fill
gh pr checks --watch
```
