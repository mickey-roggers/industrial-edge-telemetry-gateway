# Industrial Edge Telemetry Gateway

An **Odyssey task bundle** — a self-contained, verifier-graded software-engineering task. A frontier coding agent is given an incomplete FastAPI gateway (`environment/app`) and must finish it so that it correctly discovers and polls a simulated machine fleet, decodes raw Modbus-style registers into engineering units, maintains history and latest state, and drives a deterministic alarm state machine. The reference solution under `solution/` proves the task is solvable and drives the sealed verifier to full reward.

## What the gateway does

The gateway is a small FastAPI service that:

1. Discovers a fleet of simulated machines from a local simulator.
2. Polls each machine for raw registers.
3. Decodes registers into engineering units using device-specific maps.
4. Stores every valid, unique reading in history (composite-key dedup).
5. Maintains the correct "latest" machine state using exact ordering and restart rules.
6. Evaluates a deterministic alarm state machine (5 alarm types, 2 trigger paths).
7. Exposes a REST API for querying machines, status, history, and alarms.

## Repository structure

```
.
├── task.toml              # [metadata] [verifier] [agent] [environment]
├── instruction.md         # the problem statement (§1–§12)
├── README.md
├── .dockerignore
├── .gitignore
├── environment/           # the sandbox build → /app (the STARTING state the agent sees)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/               # incomplete FastAPI app (stubs + subtle bugs)
│   └── simulator/         # local machine simulator
├── solution/              # the reference solution (oracle)
│   ├── app/               # complete, correct implementation
│   └── solve.sh           # oracle entrypoint (runs the verifier against solution/app)
└── tests/                 # the sealed verifier
    ├── test.sh            # verifier entrypoint
    └── verifier_runner.py # 74 checks across 5 components
```

## REST API

| Method | Path | Description |
|---|---|---|
| POST | `/poll` | Poll all machines once; returns `{polled, stored, history_only, failures}` |
| GET | `/machines` | Summary per machine (`id`, `status`, `last_seen`, `active_alarms`) |
| GET | `/machines/{id}/status` | Latest state only |
| GET | `/machines/{id}/history?from=&to=&limit=` | Stage-A history (filterable) |
| GET | `/alarms?machine_id=&state=&severity=` | Filterable alarm list |
| GET | `/health` | Health check |
| POST | `/admin/reset` | Test convenience: wipe state + rediscover (not part of the §9 contract) |

## Register decoding

Four registers are read from each device:

| Register | Meaning | Scaling |
|---|---|---|
| 40001 | Temperature | raw ÷ 10 → °C |
| 40002 | Pressure | raw ÷ 100 → kPa |
| 40003 | Vibration | raw ÷ 10 → mm/s |
| 40004 | Status code | direct |

Two register maps are supported and are a property of the device (never hard-coded): `standard-v1` and `high-temp-v1`.

Status codes: `0` STOPPED, `1` RUNNING, `2` FAULT, `3` MAINTENANCE (code `3` is invalid for `standard-v1`).

Normal operating bands per map:

| Map | Temp (°C) | Pressure (kPa) | Vibration (mm/s) |
|---|---|---|---|
| standard-v1 | −40 … 120 | 0 … 1500 | 0 … 50 |
| high-temp-v1 | −40 … 180 | 0 … 2000 | 0 … 60 |

A reading is rejected *entirely* if it is missing any required register, decodes to a physically impossible value (outside a wider physical acceptance envelope), or carries an invalid status code for its map. A reading that merely exceeds a normal operating band is still valid data — it is stored in history, may become latest state, and can fire an alarm.

## Alarms (5 types, 2 trigger paths)

| Alarm | Severity | Trigger |
|---|---|---|
| HIGH_TEMPERATURE | WARNING | temperature > map max |
| HIGH_VIBRATION | WARNING | vibration ≥ 15.0 mm/s |
| CRITICAL_VIBRATION | CRITICAL | vibration ≥ 25.0 mm/s |
| PRESSURE_FAULT | CRITICAL | pressure < map min |
| COMMUNICATION_LOST | CRITICAL | 3 consecutive failures |

Alarms 1–4 are evaluated **only** when a reading becomes the new latest state; a history-only reading never touches them. COMMUNICATION_LOST is evaluated once per `POST /poll` for every machine, independent of the latest-state outcome. Each alarm is a single instance per `(machine_id, alarm_type)`, either ACTIVE or CLEARED.

## The verifier

`tests/test.sh` is the sealed entrypoint: it builds the environment, starts the simulator, launches the app, and runs `verifier_runner.py`. Scoring is additive across **5 components** (20% each): register decoding, history/dedup, latest state, alarm lifecycle, and API + concurrency + failures. The reference solution passes **74/74 checks**, including a hidden matrix (multi-machine interleaving, restarts, duplicates, mixed failure modes, and concurrent overlapping polls).

## Starter vs. solution

- **Starter** (`environment/app`): compiles and boots, but contains three `NotImplementedError` stubs (`decode_registers`, `should_become_latest_state`, `evaluate_telemetry_alarms`) and two subtle bugs (dedup key compares `sequence<=?` instead of `=?`; the poller's reject branch resets rather than increments the failure counter).
- **Solution** (`solution/app`): the complete, correct implementation that passes the full verifier.

## Testing

> **On Windows, run everything in Git Bash** (Start menu → "Git Bash"), not PowerShell or CMD. The scripts use bash syntax (`export`, `VAR=value`, and `\` line continuations) that other shells don't understand.

The test script auto-detects the project venv (`.venv-dev`); it only needs Python 3.12 with `fastapi`, `httpx`, `uvicorn`, and `pydantic`. No manual install is required if the venv already exists.

### 1. Full verifier against the reference (oracle)

```bash
cd <repo-root>
bash solution/solve.sh
```

Starts the simulator (port 8899) and the reference gateway (port 8111), runs all 74 checks, then tears both down. **Expected: `74/74`, `ALL CHECKS PASSED`, exit code 0.**

### 2. Starter (no-progress baseline) — expected to FAIL

```bash
cd <repo-root>
GATEWAY_DIR=environment/app bash tests/test.sh
```

The starter is intentionally incomplete, so this reports roughly `18/74` and exits 1. That is the "nop floor" proving the task is genuinely unsolved at the start — not a bug.

### 3. Manual / interactive testing

Use three Git Bash terminals. First, free port 8777 if a leftover simulator is running:

```bash
netstat -ano | grep ':8777' | grep LISTENING | awk '{print $5}' | xargs -r -n1 taskkill //F //PID
```

**Terminal 1 — simulator** (http://127.0.0.1:8777):

```bash
cd <repo-root>
.venv-dev/Scripts/python.exe environment/simulator/simulator.py
```

**Terminal 2 — gateway** (http://127.0.0.1:8111):

```bash
cd <repo-root>
export PYTHONPATH="$PWD/solution/app"
export SIMULATOR_BASE_URL="http://127.0.0.1:8777"
export GATEWAY_DB_PATH="$PWD/gateway.db"
.venv-dev/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8111
```

**Terminal 3 — drive it:**

```bash
# register a machine on the simulator
curl -X POST http://127.0.0.1:8777/control/device -H "Content-Type: application/json" -d '{"id":"d1","register_map":"standard-v1"}'

# (re)discover the fleet, then poll
curl -X POST http://127.0.0.1:8111/admin/reset
curl -X POST http://127.0.0.1:8111/poll

# query state
curl http://127.0.0.1:8111/machines
curl http://127.0.0.1:8111/machines/d1/status
curl http://127.0.0.1:8111/machines/d1/history
curl http://127.0.0.1:8111/alarms
```

The simulator's control API (`POST /control/device/{id}/script`) can inject `outofrange`, `invalidstatus`, `timeout`, `5xx`, `notfound`, `malformed`, and `missing` responses to exercise the failure modes interactively.

## Docker

The `environment/Dockerfile` packages the entire bundle into a self-contained, offline image that runs the sealed verifier — the same image the Odyssey grader builds and runs. Dependencies are baked in at build time; there is no runtime network.

**Build** (from the repo root):

```bash
docker build -f environment/Dockerfile -t edge-gateway .
```

**Run the reference** (expects `74/74`):

```bash
docker run --rm edge-gateway
```

**Run the starter** (incomplete — expects to fail):

```bash
docker run --rm -e GATEWAY_DIR=/task/environment/app edge-gateway
```

When no `/app` is mounted, `tests/test.sh` falls back to the bundled reference (`solution/app`), so the image self-validates out of the box. Docker is optional — the [Testing](#testing) commands run the same verifier without it, using the project venv.

## Metadata

`task.toml` declares the task identity and configuration: `collectionFamily = "Product clone"`, `taskFamily = "systems_integration"`, `verifierFamily = "programmatic"`, 2 CPUs / 4096 MB memory / 2048 MB storage, agent timeout 18,000 s, and fully offline (`network_mode = "none"` across build, rollout, and grading phases).

## License

[MIT](LICENSE) © 2026 Mickey Roggers.
