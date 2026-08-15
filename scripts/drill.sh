#!/usr/bin/env bash
#
# Failure drills -- break the pipeline on purpose, then practise finding it.
#
#   bash scripts/drill.sh list
#   bash scripts/drill.sh run 3      # inject drill 3 on a new branch
#   bash scripts/drill.sh reveal 3   # show the answer
#   bash scripts/drill.sh clean      # return to main, delete drill branches
#
# WHY DO THIS DELIBERATELY
#
# Debugging is a skill, and skills need reps. In real work, failures arrive
# unpredictably, at bad times, with stakes -- which is the worst possible
# environment for learning. A drill gives you the same failure with none of
# the pressure, and lets you practise the trace enough times that it becomes
# automatic.
#
# Every drill below is a real failure taken from this project's actual history
# or from a well-known class of production incident. None are invented.
#
# HOW TO USE IT
#
# Run a drill, push the branch, open a PR, and diagnose it from the symptom
# ALONE. Do not read the reveal until you have found it or spent 15 minutes.
# The reveal is worth much less if you read it first -- the point is the
# search, not the answer.
#
# Edits are applied with Python rather than sed, because BSD sed (macOS) and
# GNU sed (Linux) disagree about `-i` and this repository is cross-platform.

set -euo pipefail

DRILL_DIR=".drill"

usage() {
  cat <<'EOF'
usage: bash scripts/drill.sh <command> [n]

  list        show available drills
  run <n>     create branch drill/<n> and inject the failure
  reveal <n>  show the root cause and fix
  clean       return to main and delete all drill branches
EOF
}

list_drills() {
  cat <<'EOF'

  1  Provenance chain broken
     Symptom: CI fails at "Verify build provenance". /version says "unknown".

  2  Passes locally, fails in CI
     Symptom: `make test` is green on your machine; CI is red.

  3  Container runs locally, dies on Cloud Run
     Symptom: builds fine, deploys, then the revision never becomes ready.

  4  Vulnerable dependency introduced
     Symptom: CI red at the Trivy step with HIGH findings.

  5  Production serves the wrong commit
     Symptom: deploy succeeds; /version reports an older SHA than HEAD.

  6  Every PR hangs forever
     Symptom: checks never complete. "Expected -- waiting for status."

  7  Secret committed
     Symptom: CI red at the filesystem scan.

EOF
}

require_clean() {
  if [ -n "$(git status --porcelain)" ]; then
    echo "working tree is dirty -- commit or stash first" >&2
    exit 1
  fi
}

start_branch() {
  local n="$1"
  require_clean
  git checkout main --quiet
  git pull --quiet origin main || true
  git checkout -b "drill/${n}" --quiet
  mkdir -p "${DRILL_DIR}"
  echo "==> on branch drill/${n}"
}

py() { python3 - "$@"; }

run_drill() {
  local n="$1"
  start_branch "$n"

  case "$n" in
    1)
      # Rename the build arg the Dockerfile reads. The image builds fine and
      # the app starts fine -- it simply has no idea what commit it is.
      py <<'PY'
import pathlib
p = pathlib.Path("Dockerfile"); s = p.read_text()
s = s.replace("ARG GIT_COMMIT=unknown", "ARG GIT_SHA=unknown", 1)
s = s.replace("ENV GIT_COMMIT=${GIT_COMMIT} \\", "ENV GIT_COMMIT=${GIT_SHA_TYPO} \\", 1)
p.write_text(s)
PY
      msg="build: update provenance build arg naming"
      ;;

    2)
      # A test that depends on dict ordering of tags. Passes when the input
      # happens to be ordered; the CI environment produces a different order.
      py <<'PY'
import pathlib
p = pathlib.Path("tests/test_assets_api.py"); s = p.read_text()
s = s.replace(
    'assert response.json()["tags"] == ["prod", "dmz"]',
    'assert response.json()["tags"] == sorted(["prod", "dmz"])', 1)
p.write_text(s)
PY
      msg="test: sort tags assertion for stability"
      ;;

    3)
      # Hardcode a port that does NOT match what the platform routes to.
      #
      # NOTE: an earlier version of this drill hardcoded 8080 -- which is
      # exactly what deploy.yml passes via --port, so nothing broke. The bug
      # was real (a hardcoded port, and no `exec` so SIGTERM is swallowed)
      # but it was LATENT: correct by coincidence. Using 8000 makes the
      # mismatch actual, which is what happens the day someone changes the
      # service port in one place and not the other.
      py <<'PY'
import pathlib
p = pathlib.Path("Dockerfile"); s = p.read_text()
old = 'CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]'
new = 'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]'
assert old in s, "drill 3 anchor missing -- Dockerfile already modified?"
p.write_text(s.replace(old, new, 1))
PY
      msg="build: simplify container start command"
      ;;

    4)
      py <<'PY'
import pathlib
p = pathlib.Path("requirements.txt"); s = p.read_text()
s = s.replace("starlette==1.6.0", "starlette==0.41.3", 1)
p.write_text(s)
PY
      msg="build: pin starlette to a known-good older version"
      ;;

    5)
      # Deploy :latest, AND stop publishing :latest during the build.
      #
      # Both halves are required. An earlier version changed only the deploy
      # step -- but the build pushes both tags at the same instant, pointing
      # at the same image, so deploying :latest deployed exactly the right
      # thing. Correct by coincidence, and the drill taught nothing.
      #
      # Removing :latest from the build makes it point at whatever was pushed
      # LAST TIME. Production then serves the previous commit, which is the
      # real-world scenario: someone tidies up the tag list and misses that
      # the deploy step still references it.
      py <<'PY'
import pathlib
p = pathlib.Path(".github/workflows/deploy.yml"); s = p.read_text()

old_deploy = '--image="${{ steps.tags.outputs.sha_tag }}"'
new_deploy = '--image="${{ steps.tags.outputs.latest_tag }}"'
assert old_deploy in s, "drill 5 deploy anchor missing"
s = s.replace(old_deploy, new_deploy, 1)

old_tags = """          tags: |
            ${{ steps.tags.outputs.sha_tag }}
            ${{ steps.tags.outputs.latest_tag }}"""
new_tags = """          tags: |
            ${{ steps.tags.outputs.sha_tag }}"""
assert old_tags in s, "drill 5 tag-push anchor missing"
s = s.replace(old_tags, new_tags, 1)

p.write_text(s)
PY
      msg="ci: tidy up image tagging"
      ;;

    6)
      # Rename a CI job. Branch protection still requires the OLD name, which
      # will now never report.
      py <<'PY'
import pathlib
p = pathlib.Path(".github/workflows/ci.yml"); s = p.read_text()
s = s.replace("    name: Lint and test", "    name: Quality checks", 1)
p.write_text(s)
PY
      msg="ci: rename the quality job for clarity"
      ;;

    7)
      pathlib_target=".env.example"
      cat > "${pathlib_target}" <<'EOF'
# Example configuration
DATABASE_URL=postgresql://admin:hunter2@db.internal:5432/inventory
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
EOF
      msg="docs: add example environment configuration"
      ;;

    *)
      echo "unknown drill: $n" >&2; exit 1 ;;
  esac

  git add -A
  git commit --quiet -m "${msg}"
  cat <<EOF

==> drill ${n} injected and committed on drill/${n}

    The commit message is deliberately innocuous -- real breaking changes
    rarely announce themselves.

    Next:
      git push -u origin drill/${n}
      gh pr create --fill
      gh pr checks --watch

    Diagnose from the symptom alone. Reveal only after you have found it,
    or after 15 minutes:
      bash scripts/drill.sh reveal ${n}

EOF
}

reveal() {
  case "$1" in
    1) cat <<'EOF'

DRILL 1 -- Provenance chain broken

ROOT CAUSE
  The Dockerfile's ARG was renamed to GIT_SHA, and the ENV now references
  ${GIT_SHA_TYPO}, which is never defined. Docker substitutes an empty
  string. The app reads GIT_COMMIT, finds "", and _env() maps empty to
  "unknown".

HOW TO FIND IT
  1. CI failed at "Verify build provenance", which prints both values:
       reported: unknown        expected: <sha>
  2. "unknown" is the specific sentinel meaning provenance was never
     injected -- not that it was injected wrongly. That distinction points
     you at the injection path, not the app.
  3. Walk the chain backwards:
       /version reads GIT_COMMIT env
         <- Dockerfile ENV GIT_COMMIT=${...}
           <- Dockerfile ARG
             <- --build-arg in the workflow
  4. The ARG and the --build-arg no longer share a name.

FIX
  Restore `ARG GIT_COMMIT` and `ENV GIT_COMMIT=${GIT_COMMIT}`.

LESSON
  Docker does NOT error on an undefined build arg -- it substitutes empty.
  Silent empty-string substitution is why the "unknown" sentinel and the CI
  assertion exist. Without both, this ships and you discover it during an
  incident.

EOF
    ;;
    2) cat <<'EOF'

DRILL 2 -- Passes locally, fails in CI

ROOT CAUSE
  The assertion was changed to compare against sorted(["prod","dmz"]) ->
  ["dmz","prod"], but the API returns tags in insertion order, ["prod","dmz"].

HOW TO FIND IT
  1. Read the CI failure output -- pytest prints the actual vs expected list.
  2. Run the same test locally. It fails there too, which immediately tells
     you this is NOT an environment difference; it is a wrong assertion.
  3. `git diff main -- tests/` shows the changed line.

FIX
  Assert the real contract: ["prod", "dmz"], insertion order preserved.

LESSON
  "Passes locally, fails in CI" is a category, not a diagnosis. FIRST
  reproduce locally. If it fails locally too, it is a plain bug and the CI
  framing was a red herring that would have cost you an hour of looking at
  runner configuration.

  When it genuinely does pass locally, suspect: leftover state between
  tests, dependence on ordering, timezone, locale, filesystem case
  sensitivity, or a stale local cache.

EOF
    ;;
    3) cat <<'EOF'

DRILL 3 -- Container runs locally, dies on Cloud Run

ROOT CAUSE
  CMD hardcodes --port 8000 instead of reading $PORT. deploy.yml routes
  traffic to --port=8080. The container starts perfectly and listens on a
  port nothing is sending traffic to, so the revision never becomes ready.

  Secondary, and invisible: `exec` was dropped, so uvicorn runs as a child
  of the shell and never receives SIGTERM. The container is force-killed at
  the end of the grace period and in-flight requests are dropped. Nothing
  reports this. Ever.

HOW TO FIND IT
  CI catches this one, at "Verify build provenance":

      ##[error]container never became healthy
      INFO:     Started server process [1]
      INFO:     Application startup complete.
      INFO:     Uvicorn running on http://0.0.0.0:8000

  Read those four lines carefully, because they are not what a crash looks
  like. The process started. Startup completed. There is no traceback. The
  ONLY anomaly is the port number in the last line -- and you only notice it
  is anomalous if you know what it should be.

  That is the skill: a log that says everything succeeded, and one field in
  it that disagrees with the rest of the system.

  Then confirm the mismatch -- two numbers, two files, changed apart:
      grep -n "port=" .github/workflows/deploy.yml     -> 8080
      git diff main -- Dockerfile                      -> 8000

  IF IT REACHES PRODUCTION INSTEAD (weaker CI, or the CI probe published
  8000 too), the symptom moves one stage later: the deploy fails, the
  revision never becomes ready, and the same line appears in
      gcloud run services logs read cyber-inventory --region=us-central1

WHY CI CATCHES IT
  Because CI starts the REAL container and talks to it over HTTP, rather
  than only running unit tests. Unit tests never bind a port, so a
  pytest-only pipeline sails straight past this entire class of packaging
  bug. That step was added to verify provenance; catching runtime packaging
  errors is a second job it does for free.

  Worth generalising: the closer your pipeline gets to running the actual
  artifact the actual way, the more classes of failure it can catch.

FIX
  CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

  Read $PORT rather than matching the number by hand. One source of truth
  beats two that agree today.

LESSON
  "Works locally" is weakest exactly where the platform supplies something
  you supplied yourself locally: ports, credentials, DNS, filesystem layout.
  When a container is healthy locally and dead on the platform, enumerate
  what the platform provides that you hardcoded.

  Second lesson, from how this drill was originally written WRONG: the first
  version hardcoded 8080, which is what deploy.yml already routes to. The bug
  was real -- hardcoded port, no exec -- but it was correct BY COINCIDENCE,
  so nothing failed.

  That is a latent bug: wrong code that works until an unrelated change makes
  it matter. They are the most expensive kind, because they are introduced
  calmly during a refactor and surface months later during something urgent.
  The `exec` half of this drill is still latent even now -- no test catches
  it, and you would only ever notice it as mysterious dropped requests during
  deploys.

  This is also why "the test passed" is not the same as "the test tested
  something". A drill that cannot fail is worth exactly as much as a test
  that asserts nothing.

EOF
    ;;
    4) cat <<'EOF'

DRILL 4 -- Vulnerable dependency introduced

ROOT CAUSE
  starlette pinned back to 0.41.3, which carries three HIGH CVEs:
    CVE-2025-62727  DoS via Range header merging
    CVE-2026-48818  SSRF and NTLM credential theft via UNC paths
    CVE-2026-54283  request.form() limits silently ignored

HOW TO FIND IT
  1. CI fails at "Scan image for vulnerabilities" with a table naming the
     package, installed version, and fixed version. The scanner tells you
     the answer -- read the table rather than guessing.
  2. `git diff main -- requirements.txt` confirms the downgrade.

FIX
  Restore starlette==1.6.0.

LESSON
  Note the commit message: "pin starlette to a known-good older version".
  Downgrades are usually well-intentioned -- someone chasing a regression or
  matching another environment. This is exactly why the scanner runs on
  every PR rather than on a schedule: the intent was reasonable, the effect
  was three HIGH CVEs, and only automation caught it.

  Also note which category this is: a REAL dependency that needs upgrading,
  versus drill-style build tooling that should not be shipped at all.
  Different findings have different correct answers.

EOF
    ;;
    5) cat <<'EOF'

DRILL 5 -- Production serves the wrong commit

ROOT CAUSE
  The deploy step was changed to use the :latest tag instead of the
  commit-specific SHA tag. :latest means "whatever was pushed most recently",
  which under concurrent or retried deploys is not necessarily this commit.

HOW TO FIND IT
  1. The deploy workflow SUCCEEDS at the gcloud step and then fails at the
     smoke test:
       production is serving <old-sha>, expected <new-sha>
       the deploy reported success but old code is still serving
  2. That is the entire reason the smoke test compares SHAs rather than just
     checking for a 200.
  3. Confirm from the platform side:
       gcloud run revisions list --service=cyber-inventory --region=...
       gcloud run services describe cyber-inventory --format='value(spec.template.spec.containers[0].image)'
     The deployed image reference ends in :latest, not a SHA.

FIX
  Deploy steps.tags.outputs.sha_tag.

LESSON
  A deploy command exiting 0 means "the platform accepted my request", not
  "the new version is serving traffic". Those are different claims and only
  one of them matters. Verify from the outside, by asking production what it
  is.

  Corollary: :latest makes rollback undefined. With SHA tags, rollback is
  deploying a specific earlier tag. With :latest, there is nothing to roll
  back TO.

EOF
    ;;
    6) cat <<'EOF'

DRILL 6 -- Every PR hangs forever

ROOT CAUSE
  The CI job was renamed from "Lint and test" to "Quality checks". Branch
  protection still requires a check named "Lint and test", which will now
  never report. The PR waits forever on a check that cannot exist.

HOW TO FIND IT
  1. The symptom is distinctive: the check is not FAILING, it is "Expected --
     waiting for status to be reported". Nothing is red. Nothing is running.
  2. Compare what protection requires against what CI produces:
       gh api repos/OWNER/REPO/branches/main/protection \
         --jq '.required_status_checks.contexts'
       gh api repos/OWNER/REPO/commits/main/check-runs --jq '.check_runs[].name'
     The two lists no longer intersect.
  3. `git diff main -- .github/workflows/ci.yml` shows the rename.

FIX
  Either restore the job name, or update the required contexts to match --
  but update protection FIRST if you are renaming deliberately, or you lock
  the branch between the two changes.

LESSON
  Branch protection references jobs by their DISPLAY NAME, as a string. There
  is no link between them; renaming a job silently breaks the requirement.
  This is one of the most common ways teams lock themselves out of their own
  repository, and the giveaway is a check that is pending rather than failing.

  A pending-forever check and a failing check are completely different
  diagnoses. Read which one you have before investigating.

EOF
    ;;
    7) cat <<'EOF'

DRILL 7 -- Secret committed

ROOT CAUSE
  .env.example contains what look like real credentials: a database URL with
  an inline password, and AWS-format keys.

HOW TO FIND IT
  1. CI fails at "Scan filesystem for secrets and dependency CVEs", naming
     the file and the rule that matched.
  2. `git log -p -1` shows exactly what was added.

FIX
  Remove the values. Use obvious placeholders that no scanner and no human
  will mistake for real:
    DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME

  AND -- if a real secret was ever committed, deleting it is NOT enough.
  It remains in git history and in every clone and fork. You must ROTATE it.
  Treat any credential that reached a remote as compromised, permanently.

LESSON
  Two separate things to internalise:

  a) Placeholders should be unmistakably fake. "hunter2" and the AWS EXAMPLE
     key are both well-known dummy values, and they still tripped the
     scanner -- which is the scanner behaving correctly. It cannot read your
     intent.

  b) The scan is a backstop, not a control. It runs after the commit exists.
     By the time it fires on a real secret, the secret is already in history.
     Rotation is the only real remedy.

EOF
    ;;
    *) echo "unknown drill: $1" >&2; exit 1 ;;
  esac
}

clean() {
  git checkout main --quiet
  git branch --list 'drill/*' --format='%(refname:short)' | while read -r b; do
    [ -n "$b" ] && git branch -D "$b" --quiet && echo "deleted $b"
  done
  rm -rf "${DRILL_DIR}"
  echo "==> back on main, drill branches removed"
  echo "    remote drill branches, if pushed:"
  echo "      git push origin --delete drill/N"
}

case "${1:-}" in
  list)   list_drills ;;
  run)    run_drill "${2:?drill number required}" ;;
  reveal) reveal "${2:?drill number required}" ;;
  clean)  clean ;;
  *)      usage; exit 1 ;;
esac
