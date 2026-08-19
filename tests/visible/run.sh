#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PYTHON:-python3}"

if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
  echo "pytest is required for the local visible tests"
  exit 2
fi

PYTHONPATH="$ROOT/environment/app" "$PY" -m pytest -q "$ROOT/tests/visible/test_core.py"
