#!/usr/bin/env bash
#
# Local HTTP integration tests against a REAL cyber-inventory process.
#
#   scripts/tests/local-integration.sh              # uvicorn from .venv, free port
#   scripts/tests/local-integration.sh --docker     # the production container image
#   scripts/tests/local-integration.sh --base-url URL   # a service you already run
#
# Non-destructive: it stops only what it started, and deletes only records it
# created (every record carries a unique run id in its hostname and tags).
# Exits non-zero if any requirement fails.
#
# The in-process pytest suite proves the application logic. This proves the
# thing you actually ship: a real server, real sockets, real JSON on the wire,
# and the stdout a log aggregator would receive.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-$ROOT/.venv/bin/python}"
CHECKS="$ROOT/scripts/tests/functional_checks.py"
MODE="process"
TARGET_URL=""

usage() {
  sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --docker) MODE="docker" ;;
    --base-url)
      MODE="external"
      TARGET_URL="${2:-}"
      [ -n "$TARGET_URL" ] || { echo "--base-url needs a URL" >&2; exit 2; }
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ -x "$PY" ] || { echo "no python at $PY -- run 'make install' first" >&2; exit 2; }

COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUN_TAG="itest-$(date -u +%Y%m%d%H%M%S)-$$"
LOG="$(mktemp -t cyber-inventory-itest)"
SERVER_PID=""
SERVER_EXIT=""
CONTAINER=""
IMAGE=""
STOPPED="no"

free_port() {
  "$PY" - <<'PYTHON'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PYTHON
}

teardown() {
  local status=$?
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [ -n "$CONTAINER" ]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
  if [ -n "$IMAGE" ]; then
    docker image rm "$IMAGE" >/dev/null 2>&1 || true
  fi
  if [ "$status" -ne 0 ] && [ -s "$LOG" ]; then
    echo ""
    echo "--- last 40 lines of the service log ($LOG) ---"
    tail -n 40 "$LOG"
  fi
  exit "$status"
}
trap teardown EXIT

wait_until_healthy() {
  local url="$1" attempt code
  for attempt in $(seq 1 60); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' "$url/health" 2>/dev/null || echo 000)"
    if [ "$code" = "200" ]; then
      echo "service healthy after ${attempt} attempt(s)"
      return 0
    fi
    sleep 0.5
  done
  echo "ERROR: service never became healthy (last HTTP status ${code:-none})" >&2
  return 1
}

case "$MODE" in
  process)
    PORT="$(free_port)"
    BASE_URL="http://127.0.0.1:$PORT"
    echo "starting uvicorn on $BASE_URL (commit ${COMMIT:0:7})"
    GIT_COMMIT="$COMMIT" BUILD_TIME="$BUILD_TIME" ENVIRONMENT=integration \
      "$ROOT/.venv/bin/uvicorn" app.main:app \
      --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
    SERVER_PID=$!
    ;;
  docker)
    command -v docker >/dev/null || { echo "docker not installed" >&2; exit 2; }
    IMAGE="cyber-inventory:$RUN_TAG"
    CONTAINER="cyber-inventory-$RUN_TAG"
    echo "building $IMAGE"
    docker build \
      --build-arg GIT_COMMIT="$COMMIT" \
      --build-arg BUILD_TIME="$BUILD_TIME" \
      -t "$IMAGE" "$ROOT"
    PORT="$(free_port)"
    # Deliberately not 8080 inside the container: this also proves the image
    # honours $PORT the way Cloud Run requires (RUN-05).
    docker run -d --name "$CONTAINER" \
      -e PORT=9090 -e ENVIRONMENT=integration \
      -p "127.0.0.1:$PORT:9090" "$IMAGE" >/dev/null
    BASE_URL="http://127.0.0.1:$PORT"
    echo "started container $CONTAINER on $BASE_URL"
    ;;
  external)
    BASE_URL="$TARGET_URL"
    echo "targeting $BASE_URL (not started by this script)"
    ;;
esac

wait_until_healthy "$BASE_URL"

RC=0
if [ "$MODE" = "external" ]; then
  "$PY" "$CHECKS" http \
    --base-url "$BASE_URL" \
    --suites ops,lifecycle,validation,filters \
    --run-prefix itest || RC=1
else
  "$PY" "$CHECKS" http \
    --base-url "$BASE_URL" \
    --suites ops,lifecycle,validation,filters,concurrency \
    --exclusive \
    --expect-commit "$COMMIT" \
    --run-prefix itest || RC=1
fi

# Stop the service we started, then check what it logged on the way out.
if [ "$MODE" = "process" ]; then
  echo ""
  echo "stopping uvicorn (SIGTERM) to check graceful shutdown"
  kill -TERM "$SERVER_PID"
  set +e
  wait "$SERVER_PID"
  SERVER_EXIT=$?
  set -e
  SERVER_PID=""
  STOPPED="yes"
elif [ "$MODE" = "docker" ]; then
  echo ""
  echo "stopping container (SIGTERM, 10s grace) to check graceful shutdown"
  docker stop -t 10 "$CONTAINER" >/dev/null
  SERVER_EXIT="$(docker inspect -f '{{.State.ExitCode}}' "$CONTAINER")"
  docker logs "$CONTAINER" >"$LOG" 2>&1 || true
  STOPPED="yes"
fi

if [ "$STOPPED" = "yes" ]; then
  echo ""
  echo "--- log checks ---"
  "$PY" "$CHECKS" logs \
    --log-file "$LOG" \
    --expect-commit "$COMMIT" \
    --expect-shutdown \
    --expect-mutations \
    --exit-code "$SERVER_EXIT" || RC=1
fi

echo ""
if [ "$RC" -eq 0 ]; then
  echo "LOCAL INTEGRATION: PASS"
else
  echo "LOCAL INTEGRATION: FAIL"
fi
exit "$RC"
