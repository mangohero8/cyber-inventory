# Failure Diagnosis

How to trace a change from commit to production, and what to do when the
trace breaks. Learning goal #2, made practical.

> **Extending this document.** Part 1 is the method. Part 2 is a symptom
> index. Part 3 is the drill workbook. Add new symptoms to the Part 2 table
> and new drills to `scripts/drill.sh`, then record them here.

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-15 | Initial. Method, symptom index, 7 drills. |

---

# Part 1 — The trace

Every change passes through six checkpoints. Diagnosis is finding the last
one that's correct; the break is always in the gap after it.

```
1  COMMIT       git log -1 --format='%H %s'
      │
2  CI RUN       gh run list --workflow=ci.yml --limit 3
      │
3  IMAGE        gcloud artifacts docker images list \
      │            REGION-docker.pkg.dev/PROJECT/REPO/IMAGE --include-tags
4  DEPLOY       gh run list --workflow=deploy.yml --limit 3
      │
5  REVISION     gcloud run revisions list --service=SVC --region=REGION
      │
6  PRODUCTION   curl -s "$URL/version"
```

## Walk it backwards

**Start at 6, not at 1.** Ask production what it is. That single answer
eliminates most of the search space:

```bash
URL=$(gcloud run services describe cyber-inventory --region=us-central1 \
      --format='value(status.url)')
curl -s "$URL/version"
```

| What `/version` says | What it means | Where to look next |
|---|---|---|
| The commit you expect | Production is current. The bug is in the code, not the pipeline. | Application logs |
| An **older** commit | The deploy didn't take effect. | Checkpoints 4–5 |
| `"unknown"` | The image wasn't built by CI, or provenance injection is broken. | Checkpoints 2–3 |
| Nothing — connection fails | Routing, auth, or the container isn't serving. | Part 2, by HTTP code |

That table is the entire reason `/version` exists. Without it, "is my change
live?" is a guess, and every subsequent step is guessing on top of a guess.

## The one-command status check

```bash
echo "HEAD:  $(git rev-parse HEAD)"
echo "PROD:  $(curl -s "$URL/version" | python3 -c 'import json,sys; print(json.load(sys.stdin)["commit"])')"
gh run list --limit 3
```

## Read the failure, not the summary

Three habits that save the most time:

**Read whose error page it is.** A 404 that doesn't look like your framework's
404 means something upstream answered instead of your app. That's how the
Cloud Run reserved-path problem was found.

**A pending check and a failing check are different diagnoses.** Failing means
your code ran and something was wrong. Pending forever means the check never
started — usually a name mismatch in branch protection, or a workflow that
doesn't trigger on this event.

**"Does not exist" often means something else.** Real examples from this
project: a service account that existed but hadn't propagated through IAM; a
repository that existed but was private; a REST endpoint reached with the
wrong HTTP method. Check whether the thing exists *and* whether you're
allowed to see it *and* whether you're asking correctly.

---

# Part 2 — Symptom index

| Symptom | Most likely cause | First command |
|---|---|---|
| `/version` says `unknown` | Build arg not injected, or renamed | `git diff main -- Dockerfile` |
| `/version` shows an older SHA | Deployed `:latest`, or deploy silently failed | `gcloud run revisions list` |
| Revision never becomes ready | Container not listening on `$PORT`, or crash at startup | `gcloud run services logs read` |
| HTTP 000 in smoke test | DNS not resolvable, or wrong URL | `dig $URL`, check the URL variable |
| HTTP 403 from a live service | Not publicly invokable | `gcloud run services get-iam-policy` |
| HTTP 404, Google-branded page | Reserved path (ends in `z`), or wrong route | Try `/version`; check `app/main.py` |
| HTTP 404, FastAPI JSON | Route genuinely not registered | `curl $URL/openapi.json` |
| Check pending forever | Protection requires a check name that never reports | Compare protection contexts to check-run names |
| Passes locally, fails in CI | Reproduce locally first — usually not environmental | `make test` |
| `Cache export is not supported` | Missing `setup-buildx-action` | `grep buildx .github/workflows/` |
| `Service account does not exist` right after creating it | IAM eventual consistency | Wait 15s, retry |
| `bash\r: command not found` | CRLF line endings from a Windows checkout | `file scripts/*.sh`, check `.gitattributes` |
| `No module named 'app'` | Project root not on the import path | `grep pythonpath pyproject.toml` |
| `refusing to allow a PAT` on push | Token lacks `workflow` scope | Use SSH, or add the scope |
| Push rejected, `fetch first` | Remote moved ahead | `git log HEAD..origin/main` |
| Scanner flags a package you don't import | Build tooling shipped in the image | Check both the venv **and** system site-packages |

---

# Part 3 — Drills

Seven deliberate failures, each taken from this project's real history or a
well-known class of incident.

```bash
bash scripts/drill.sh list
bash scripts/drill.sh run 3      # inject on a new branch
bash scripts/drill.sh reveal 3   # answer -- read it LAST
bash scripts/drill.sh clean
```

## How to run one properly

1. `bash scripts/drill.sh run N`
2. `git push -u origin drill/N && gh pr create --fill`
3. Watch it fail. **Diagnose from the symptom alone.**
4. Write down your hypothesis *before* checking — being wrong on purpose is
   most of the learning.
5. Confirm with `git diff main`.
6. Read the reveal.

Give each one fifteen minutes before revealing. The reveal is worth much less
if you read it first; the point is the search, not the answer.

The injected commit messages are deliberately innocuous — `"build: simplify
container start command"` for the one that breaks Cloud Run. Real breaking
changes rarely announce themselves, and a plausible commit message is part of
the exercise.

## The seven

| # | Failure | Stage it breaks | Core lesson |
|---|---|---|---|
| 1 | Provenance chain broken | Build | Docker substitutes empty for undefined build args — silently |
| 2 | Passes locally, fails in CI | Test | Reproduce locally first; usually it isn't environmental |
| 3 | Runs locally, dies on Cloud Run | Deploy | Enumerate what the platform supplies that you hardcoded |
| 4 | Vulnerable dependency | Scan | Well-intentioned changes have unintended effects; that's why scans gate |
| 5 | Production serves the wrong commit | Deploy | Exit code 0 ≠ new version serving |
| 6 | Every PR hangs forever | Protection | Pending ≠ failing; protection matches job names as strings |
| 7 | Secret committed | Scan | The scan is a backstop, not a control — rotate, don't just delete |

## Running them as a cohort exercise

Worth doing deliberately, since it serves the community goal as well as the
diagnosis one:

- **Pair up.** One person injects a drill without telling the other which.
  The second diagnoses out loud while the first stays quiet.
- **Time-box to fifteen minutes**, then swap regardless of outcome.
- **Debrief on the search, not the answer** — what did you check first, what
  did you rule out, what would you check first next time?
- **Keep a shared symptom index.** Every failure the cohort hits for real gets
  a row in a table like Part 2. By week two you have a group reference nobody
  had to write from scratch.

The debrief is where the value is. Two people who each solved three drills
know six failures if they talk, and three if they don't.

---

# Part 4 — *(reserved)*

*Add new drills, symptoms, and post-incident notes here.*
