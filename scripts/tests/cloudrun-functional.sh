#!/usr/bin/env bash
#
# Functional validation of the PRIVATE cyber-inventory Cloud Run service.
#
#   scripts/tests/cloudrun-functional.sh [--project P] [--region R]
#                                        [--service S] [--expect-commit SHA]
#                                        [--plan]
#
# WHAT IT DOES
#   1. read-only discovery of the service URL and serving revision
#   2. an anonymous request must be rejected with 403 (IAM still enforced)
#   3. authenticated /health, /ready and /version
#   4. a test-only CRUD lifecycle: unique ids, cleaned up and verified
#   5. reproduction of the known filter behaviour (F-05)
#   6. a report of existing smoke-* / owner=ci records -- never deleted
#
# WHAT IT MUST NEVER DO -- and cannot, because every gcloud call goes through
# gcloud_readonly(), which allowlists read-only verbs only:
#   deploy, change traffic or tags, change IAM, restart or update the service,
#   change branch protection, or delete a record it did not create.
#
# The caller needs roles/run.invoker on the service. This script does not
# grant it, and will not.
#
# --plan prints what would run and exits, without contacting anything.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-$ROOT/.venv/bin/python}"
CHECKS="$ROOT/scripts/tests/functional_checks.py"
SERVICE="${SERVICE:-cyber-inventory}"
REGION="${REGION:-us-central1}"
PROJECT="${PROJECT:-}"
EXPECT_COMMIT=""
PLAN="no"

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?--project needs a value}"; shift ;;
    --region) REGION="${2:?--region needs a value}"; shift ;;
    --service) SERVICE="${2:?--service needs a value}"; shift ;;
    --expect-commit) EXPECT_COMMIT="${2:?--expect-commit needs a value}"; shift ;;
    --plan) PLAN="yes" ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# Every gcloud invocation in this script goes through here. Anything that is
# not an explicitly allowlisted read-only command aborts the run.
gcloud_readonly() {
  case "$*" in
    "run services describe "*) ;;
    "auth print-identity-token"|"auth print-identity-token "*) ;;
    "config get-value "*) ;;
    *)
      echo "BLOCKED: 'gcloud $*' is not on the read-only allowlist" >&2
      exit 3
      ;;
  esac
  gcloud "$@"
}

if [ "$PLAN" = "yes" ]; then
  cat <<PLANNED
PLAN (nothing was contacted)

  read-only discovery:
    gcloud run services describe $SERVICE --region $REGION ${PROJECT:+--project $PROJECT} \\
      --format='value(status.url)'
    gcloud run services describe $SERVICE --region $REGION ${PROJECT:+--project $PROJECT} \\
      --format='value(status.latestReadyRevisionName)'

  IAM check (expects HTTP 403):
    curl -sS -o /dev/null -w '%{http_code}' "<service-url>/health"
    curl -sS -o /dev/null -w '%{http_code}' -X POST "<service-url>/api/v1/assets" \\
      -H 'Content-Type: application/json' -d '{}'

  authenticated checks:
    CYBER_INVENTORY_ID_TOKEN=\$(gcloud auth print-identity-token) \\
    $PY scripts/tests/functional_checks.py http \\
      --base-url "<service-url>" --auth-env CYBER_INVENTORY_ID_TOKEN \\
      --suites ops,lifecycle,filters,smoke-report --run-prefix crfst \\
      ${EXPECT_COMMIT:+--expect-commit $EXPECT_COMMIT}

  never run: deploy, update-traffic, update-tags, add-iam-policy-binding,
             services update, services delete, branch protection changes
PLANNED
  exit 0
fi

[ -x "$PY" ] || { echo "no python at $PY -- run 'make install' first" >&2; exit 2; }
command -v gcloud >/dev/null || { echo "gcloud is not installed" >&2; exit 2; }

if [ -z "$PROJECT" ]; then
  PROJECT="$(gcloud_readonly config get-value project 2>/dev/null || true)"
fi
[ -n "$PROJECT" ] && [ "$PROJECT" != "(unset)" ] || {
  echo "no project: pass --project or run 'gcloud config set project'" >&2
  exit 2
}

describe() {
  gcloud_readonly run services describe "$SERVICE" \
    --project "$PROJECT" --region "$REGION" --format="$1"
}

echo "======================================================================"
echo "cyber-inventory Cloud Run functional validation"
echo "  project:  $PROJECT"
echo "  region:   $REGION"
echo "  service:  $SERVICE"
echo "======================================================================"

URL="$(describe 'value(status.url)')"
[ -n "$URL" ] || { echo "could not resolve the service URL" >&2; exit 2; }
REVISION="$(describe 'value(status.latestReadyRevisionName)')"
echo "  url:      $URL"
echo "  revision: $REVISION"

RC=0

echo ""
echo "TEST IAM-01  an anonymous GET /health is rejected"
ANON_GET="$(curl -sS -o /dev/null -w '%{http_code}' "$URL/health" || echo 000)"
echo "  expected: 403"
echo "  actual:   $ANON_GET"
if [ "$ANON_GET" = "403" ]; then echo "  result:   PASS"; else echo "  result:   FAIL"; RC=1; fi

echo ""
echo "TEST IAM-02  an anonymous POST is rejected before it can write"
# Deliberately an empty body: if IAM were broken this is a 422, never a record.
ANON_POST="$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -d '{}' \
  "$URL/api/v1/assets" || echo 000)"
echo "  expected: 403"
echo "  actual:   $ANON_POST"
if [ "$ANON_POST" = "403" ]; then echo "  result:   PASS"; else echo "  result:   FAIL"; RC=1; fi

# The token is exported, never passed as an argument, so it cannot show up in
# `ps` output or a shell history file.
CYBER_INVENTORY_ID_TOKEN="$(gcloud_readonly auth print-identity-token)"
export CYBER_INVENTORY_ID_TOKEN
[ -n "$CYBER_INVENTORY_ID_TOKEN" ] || { echo "could not mint an ID token" >&2; exit 2; }

echo ""
"$PY" "$CHECKS" http \
  --base-url "$URL" \
  --auth-env CYBER_INVENTORY_ID_TOKEN \
  --suites ops,lifecycle,filters,smoke-report \
  --run-prefix crfst \
  ${EXPECT_COMMIT:+--expect-commit "$EXPECT_COMMIT"} || RC=1

unset CYBER_INVENTORY_ID_TOKEN

echo ""
echo "revision serving during this run: $REVISION"
if [ "$RC" -eq 0 ]; then
  echo "CLOUD RUN VALIDATION: PASS"
else
  echo "CLOUD RUN VALIDATION: FAIL"
fi
exit "$RC"
