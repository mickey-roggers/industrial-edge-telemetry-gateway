#!/usr/bin/env bash
# solution/solve.sh - canonical reference solution entrypoint.
#
# Runs the full reference implementation (solution/app) against the sealed
# verifier (tests/test.sh). The verifier starts the bundled simulator, launches
# this gateway, drives every visible + hidden scenario, and exits 0 iff all
# five scoring components pass. This script pins the gateway-under-test to the
# reference app and delegates to the canonical verifier entrypoint.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export GATEWAY_DIR="$ROOT/solution/app"
exec bash "$ROOT/tests/test.sh"
