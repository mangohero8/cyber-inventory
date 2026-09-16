#!/usr/bin/env bash
#
# DESTRUCTIVE (local only). Restarts or replaces a throwaway cyber-inventory
# instance that this harness starts itself, and records what happens to the
# inventory data.
#
#   scripts/tests/destructive/persistence-local.sh --mode process --confirm-restart
#   scripts/tests/destructive/persistence-local.sh --mode container-replace --confirm-restart
#   scripts/tests/destructive/persistence-local.sh --mode container-restart --confirm-restart
#   scripts/tests/destructive/persistence-local.sh --all --confirm-restart
#
# Kept out of the ordinary integration tests on purpose: this is not a smoke
# test, and today it is EXPECTED to report the requirement as FAIL (F-03).
# Exit status: 0 requirement met, 1 requirement failed (expected today),
# 2 the harness could not run.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
SCRIPT="$ROOT/scripts/tests/persistence_local.py"

[ -x "$PY" ] || { echo "no python at $PY -- run 'make install' first" >&2; exit 2; }

if [ "${1:-}" = "--all" ]; then
  shift
  status=0
  for mode in process container-replace container-restart; do
    echo ""
    echo "############ mode: $mode ############"
    rc=0
    "$PY" "$SCRIPT" --mode "$mode" "$@" || rc=$?
    [ "$rc" -gt "$status" ] && status=$rc
  done
  exit "$status"
fi

exec "$PY" "$SCRIPT" "$@"
