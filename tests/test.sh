#!/usr/bin/env bash
# Canonical sealed verifier entrypoint: tests/test.sh
#
# Starts the bundled simulator and the gateway-under-test, drives every
# visible + hidden scenario through the REST API, and scores five independent
# components. Each component is 0 or full weight (20% each); scoring is
# additive. Exit code 0 = pass, non-zero = fail.
#
# The gateway-under-test is, in order of precedence:
#   1. $GATEWAY_DIR        (explicit override, used by the grader)
#   2. /app                (where the candidate's code is mounted in Docker)
#   3. $ROOT/solution/app  (the bundled reference, for self-validation)
# The simulator is ALWAYS this bundle's simulator so the verifier fully controls
# the device fleet and failure injection.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# On Windows/MSYS the python interpreter is a native binary that does not
# understand /c/... style paths, so convert ROOT to a native Windows path.
if command -v cygpath >/dev/null 2>&1; then
  ROOT_W="$(cygpath -w "$ROOT")"
else
  ROOT_W="$ROOT"
fi

PY="${PYTHON:-python3}"
# Prefer a project venv python if present (Linux layout, or Windows Scripts layout).
if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; fi
if [ -x "$ROOT/.venv-dev/Scripts/python.exe" ]; then PY="$ROOT/.venv-dev/Scripts/python.exe"; fi
if [ -x "$ROOT/.venv-dev/bin/python" ]; then PY="$ROOT/.venv-dev/bin/python"; fi

SIM_PORT=8899
GW_PORT=8111
SIM_URL="http://127.0.0.1:${SIM_PORT}"
GW_URL="http://127.0.0.1:${GW_PORT}"

# ---- locate gateway-under-test ----
if [ -n "${GATEWAY_DIR:-}" ] && [ -f "$GATEWAY_DIR/main.py" ]; then
  APP_DIR="$GATEWAY_DIR"
elif [ -d "/app" ] && [ -f "/app/main.py" ]; then
  APP_DIR="/app"
elif [ -f "$ROOT/solution/app/main.py" ]; then
  APP_DIR="$ROOT/solution/app"
else
  echo "ERROR: cannot locate gateway app (set GATEWAY_DIR, or mount at /app)"
  exit 2
fi
SIM_DIR="$ROOT/environment/simulator"

# Convert python-facing paths to native form when running under MSYS/Windows.
if [ "$ROOT_W" != "$ROOT" ]; then
  SIM_DIR="$(cygpath -w "$SIM_DIR")"
  if [ -n "${GATEWAY_DIR:-}" ]; then APP_DIR="$(cygpath -w "$GATEWAY_DIR")"; fi
fi

DB="$ROOT_W/.verifier_gateway.db"
rm -f "$DB"

export SIMULATOR_PORT="$SIM_PORT"
export POLL_TIMEOUT_SECONDS="3.0"
export GATEWAY_DB_PATH="$DB"
export SIMULATOR_BASE_URL="$SIM_URL"

wait_url() {
  local url="$1"; local n="${2:-80}"; local i=0
  while [ "$i" -lt "$n" ]; do
    if curl -s -o /dev/null -m 1 "$url" 2>/dev/null; then return 0; fi
    sleep 0.2; i=$((i+1))
  done
  return 1
}

# Kill any process (orphan from a previous interrupted run) holding TCP <port>.
free_port() {
  local port="$1"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" 2>/dev/null
  fi
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "port $port" 2>/dev/null
  fi
  # Fallback for environments without fuser/pkill (e.g. some MSYS shells): parse
  # netstat output and taskkill the holder directly.
  if command -v netstat >/dev/null 2>&1 && command -v taskkill >/dev/null 2>&1; then
    for pid in $(netstat -ano 2>/dev/null | grep ":$port " | grep LISTENING | awk '{print $5}'); do
      [ "$pid" != "0" ] && taskkill /F /PID "$pid" 2>/dev/null
    done
  fi
  sleep 0.5
}

# Kill a service by direct PID (background job) and, if pkill exists, by name so
# grandchild processes (e.g. the python interpreter spawned under MSYS) are also
# reaped instead of orphaning the port. Falls back to taskkill by port.
kill_svc() {
  local pid="$1"; local name="$2"
  kill "$pid" 2>/dev/null
  if command -v pkill >/dev/null 2>&1 && [ -n "$name" ]; then
    pkill -f "$name" 2>/dev/null
  fi
  if command -v taskkill >/dev/null 2>&1 && [ -n "$name" ]; then
    taskkill /F /PID "$pid" 2>/dev/null
  fi
  sleep 0.3
}

free_port "$SIM_PORT"
free_port "$GW_PORT"

# Launch a service detached so it survives the launching shell (important when
# this script is PID 1's child in a container). Use setsid/nohup where available.
detach() {
  if command -v setsid >/dev/null 2>&1; then
    setsid "$@" &
  else
    nohup "$@" &
  fi
}

# ---- start simulator ----
echo "starting simulator on $SIM_PORT"
detach env PYTHONPATH="$SIM_DIR" "$PY" "$SIM_DIR/simulator.py" > "$ROOT_W/.sim.log" 2>&1
SIM_PID=$!
if ! wait_url "$SIM_URL/health"; then
  echo "FAIL: simulator did not start"; cat "$ROOT_W/.sim.log"; kill_svc "$SIM_PID" "simulator.py"; exit 2
fi
echo "simulator up"

# ---- start gateway ----
echo "starting gateway (under test) on $GW_PORT from $APP_DIR"
detach env PYTHONPATH="$APP_DIR" "$PY" -m uvicorn main:app --host 127.0.0.1 --port "$GW_PORT" --log-level warning > "$ROOT_W/.gw.log" 2>&1
GW_PID=$!
if ! wait_url "$GW_URL/health"; then
  echo "FAIL: gateway did not start"; cat "$ROOT_W/.gw.log"; kill_svc "$SIM_PID" "simulator.py"; exit 2
fi
echo "gateway up"

# ---- run the verifier scenarios ----
export VERIFIER_SIM_URL="$SIM_URL"
export VERIFIER_GW_URL="$GW_URL"
export VERIFIER_DB="$DB"

"$PY" "$ROOT_W/tests/verifier_runner.py"
RC=$?
echo "verifier_runner exit: $RC"

kill_svc "$GW_PID" "uvicorn main:app"
kill_svc "$SIM_PID" "simulator.py"
exit $RC
