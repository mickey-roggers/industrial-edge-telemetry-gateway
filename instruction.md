# Industrial Edge Telemetry Gateway

You are building a complete **Odyssey task bundle** called the **Industrial Edge Telemetry Gateway**. This is a systems-integration coding task: a FastAPI service that polls simulated industrial machines, decodes PLC-style registers, maintains correct historical and live state under out-of-order/duplicate/restart conditions, runs a deterministic alarm engine, and exposes a REST API — all under concurrency and simulated failure modes.

The specification below is **locked and final**. Do not reinterpret, simplify, or "improve" any rule. If you find a genuine contradiction, stop and flag it before writing code; do not silently resolve it in whichever direction seems reasonable.

Work in this exact order and do not skip ahead:

1. Build the **reference solution** (`solution/solve.sh` + full working app) directly against §4–§9 below.
2. **Hand-trace** the reference against every case in the "Hidden Test Scenarios" list (§12) before writing any verifier code.
3. Only once the reference passes the hand-trace, implement `tests/test.sh` (visible + hidden, per §12).
4. Build the **incomplete starter app** by stripping the working reference down to stubs (`NotImplementedError` and a few subtly-wrong implementations).
5. Produce `task.toml`, `Dockerfile`, `requirements.txt`, and the final bundle ZIP per the layout in §13.

## 1. Task Identity

| Field | Value |
|---|---|
| title | Implement a Robust Industrial Edge Telemetry Gateway |
| workingSlug | industrial-edge-telemetry-gateway |
| collectionFamily | Product clone |
| taskFamily | systems_integration |
| verifierFamily | programmatic |
| objective | Complete the Industrial Edge Telemetry Gateway so that it discovers simulated machines, polls them, decodes raw registers into engineering values using the supplied device-specific maps, stores every valid unique reading in history, maintains correct latest machine state using the exact ordering and restart rules, manages a deterministic alarm state machine, and exposes the required REST API. The implementation must remain correct under concurrent overlapping polls and under the defined failure modes while preserving the exact API contract. |
| motivation | Real industrial edge gateways must turn unreliable PLC register streams into consistent historical records and a trustworthy live machine state that other plant systems can query. This task stands in for that class of systems-integration work. |
| difficultyExplanation | Difficulty lives in four places: (1) separating historical storage from latest-state updates - most naive implementations collapse the two; (2) the exact restart predicate, which requires reasoning about sequence AND timestamp AND a high-water-mark threshold simultaneously, not just sorting; (3) concurrency-safety of the poll path under overlapping requests and partial failures; (4) the interaction between failure modes and the COMMUNICATION_LOST counter, where a valid duplicate and an out-of-envelope value must be treated differently even though both are terminal outcomes for that reading. Frontier models frequently default to received-order or wall-clock ordering instead of tracking a per-device sequence high-water mark with an explicit restart exception, and they often let a late historical reading incorrectly re-trigger alarm evaluation. |
| expertTimeEstimateHours | 14 |
| oracleStrategy | The reference solution under `solution/` implements the exact two-stage acceptance model (§4), the latest-state predicates (§5), the COMMUNICATION_LOST counter (§6), and the alarm state machine (§7), and drives every visible and hidden verifier scenario to full reward. |
| verificationStrategy | The sealed verifier under `tests/` exercises five independent components (§10). Mechanism-level scenarios are included in the verifier, while held-out scenarios combine mechanisms through multi-machine interleaving, restarts, duplicates, mixed failure modes, and concurrent overlapping polls. Scoring is additive. |
| binarySuccessCondition | The implementation passes every required check in all five scoring components — including the full hidden matrix — and produces correct history, latest state, and alarm transitions for every verifier scenario without modifying the test harness or simulator contract. |
| partialScoreStrategy | Each of the five components (§10) is scored independently, 0 or full weight per component. No component compensates for a failed one. Scores are additive. |
| environmentSummary | Python 3.12, FastAPI, httpx, uvicorn, SQLite, asyncio. Runtime is fully offline — the simulator and verifier are local and no runtime network access is required. Python dependencies are installed when the Docker image is built. |
| resourceEstimate | cpus: 2, memory_mb: 4096, storage_mb: 2048, gpus: 0, agentTimeoutSec: 18000 (5h), verifierTimeoutSec: 1800 |
| networkRequirements | none at task runtime; dependencies are resolved during image build |

## 2. System Overview

```
Simulated Machines (in-image)         Edge Gateway                          Gateway REST API
-----------------------------         ------------------------------        ----------------------
GET /devices                 ------>  Poller (asyncio, concurrent)          GET /machines
GET /devices/{id}/registers  ------>  Register Decoder                      GET /machines/{id}/status
(failure injection: timeout,          Validator (range + status)            GET /machines/{id}/history
 5xx, 404, malformed JSON)            Two-Stage Processor                   GET /alarms
                                      (History vs Latest State)             POST /poll
                                      Alarm Engine    --> SQLite Storage
```

**Core design principle:** a reading being stored in history and a reading becoming the machine's current live state are two separate decisions. A valid, uniquely-sequenced reading is always stored. It only becomes the new latest state — and therefore only feeds the alarm engine — if it also satisfies the ordering/restart predicate in §5.

## 3. Register Maps

| Register | Meaning | Conversion | `standard-v1` normal band | `high-temp-v1` normal band |
|---|---|---|---|---|
| 40001 | Temperature | `raw / 10.0` → °C | −40.0 … 120.0 | −40.0 … 180.0 |
| 40002 | Pressure | `raw / 100.0` → kPa | 0.0 … 1500.0 | 0.0 … 2000.0 |
| 40003 | Vibration | `raw / 10.0` → mm/s | 0.0 … 50.0 | 0.0 … 60.0 |
| 40004 | Status (enum) | direct | 0/1/2 | 0/1/2/3 |

Status enum:

| Code | `standard-v1` | `high-temp-v1` |
|---|---|---|
| 0 | STOPPED | STOPPED |
| 1 | RUNNING | RUNNING |
| 2 | FAULT | FAULT |
| 3 | *(invalid)* | MAINTENANCE |

The numeric bands above are the map-specific **normal operating bands** used by the alarm engine (§7). A value just above or below a normal band is still valid machine data if it is physically plausible; it is stored, can become latest state, and may raise an alarm.

**Physical validity envelope for Stage-A rejection:** reject temperature outside −273.15 … 1000.0 °C, pressure outside −100.0 … 50000.0 kPa, or vibration outside −100.0 … 1000.0 mm/s. These values represent impossible/corrupt sensor data rather than abnormal-but-real operation.

**Rejection rule:** any missing required register, any decoded value outside the physical validity envelope, or an unrecognized status code for that map causes the entire reading to be rejected — no history entry, no state update, and one failure toward COMMUNICATION_LOST (§6). The map comes from the device's `register_map` field returned by `GET /devices`; do not hard-code a single map.

## 4. Historical Acceptance (Stage A)

A reading is stored in history **if and only if all four hold**:

1. The poll response was syntactically valid (not a timeout, 5xx, 404, or malformed JSON).
2. `(device_id, sequence)` has never been stored before.
3. All four required registers are present.
4. Every decoded numeric value is inside the physical validity envelope, and the status code is valid for that device's map.

If stored, the reading is written to history unconditionally — independent of whether it will become the new latest state. History is always returned ordered by `(timestamp ASC, sequence ASC)`.

## 5. Latest-State Update (Stage B)

After a reading passes Stage A, it becomes the new latest state only if one of these two predicates holds. Do not substitute highest-sequence-wins or another simplification.

**Normal progression**
```
sequence  > current_latest_sequence[device]
timestamp > current_latest_timestamp[device]
```

**Restart**
```
sequence  < current_latest_sequence[device]
timestamp > current_latest_timestamp[device]
current_latest_sequence[device] >= 900
sequence <= 100
```

Both branches require `timestamp >` strictly. Therefore a timestamp tie is stored in history but never becomes latest state.

If neither predicate holds, the reading is history-only: it remains in `/history`, does not change `/status`, and does not trigger alarm recalculation.

**Worked example:**

| Event | seq | ts | Latest? | Why |
|---|---:|---|---|---|
| 1 | 100 | 10:00 | yes | first reading |
| 2 | 102 | 10:02 | yes | seq↑, ts↑ |
| 3 | 101 | 10:01 | no | ts is older — history only |
| 4 | 997 | 10:05 | yes | seq↑, ts↑ |
| 5 | 0 | 10:06 | yes | restart predicate |
| 6 | 0 | 10:06 | no | composite duplicate |
| 7 | 50 | 10:07 | yes | normal progression after restart |

## 6. COMMUNICATION_LOST Counter

Each machine keeps one consecutive-failure counter. It is updated once for that machine's outcome during each `POST /poll`.

| Poll outcome | Counter |
|---|---|
| New valid reading stored in history | reset to 0 |
| Valid duplicate — `(device_id, sequence)` already stored | reset to 0 |
| Timeout | +1 |
| HTTP 5xx | +1 |
| HTTP 404 | +1 |
| Malformed JSON | +1 |
| Missing required register | +1 |
| Value outside physical validity envelope / invalid status code | +1 |

A stale/history-only valid reading resets the counter because it is still valid communication. When the counter reaches **3**, `COMMUNICATION_LOST` becomes `ACTIVE`. A subsequent valid response (new, duplicate, or history-only) resets the counter and clears the alarm on the next poll.

## 7. Alarm State Machine

One alarm instance exists per `(machine_id, alarm_type)`, with two independent trigger paths:

| Alarm type | Raise condition | Severity | Recalculated when |
|---|---|---|---|
| HIGH_TEMPERATURE | `temperature_c > map max` | WARNING | reading becomes latest state |
| HIGH_VIBRATION | `vibration_mm_s >= 15.0` | WARNING | reading becomes latest state |
| CRITICAL_VIBRATION | `vibration_mm_s >= 25.0` | CRITICAL | reading becomes latest state |
| PRESSURE_FAULT | `pressure_kpa < map min` | CRITICAL | reading becomes latest state |
| COMMUNICATION_LOST | consecutive-failure counter ≥ 3 | CRITICAL | every `POST /poll` |

Rules:
- Raise to `ACTIVE` the first time a condition becomes true on its trigger path.
- Clear to `CLEARED` only when a subsequent evaluation on that same trigger path makes the condition false.
- No debounce/hysteresis in v1.
- A history-only reading never triggers alarms 1–4, even if its values would satisfy a condition. Alarms 1–4 recalculate only on latest-state updates. COMMUNICATION_LOST is evaluated separately once per poll after all machines have been attempted.
- `active_alarms` counts currently `ACTIVE` alarms across all five types.

## 8. Two-Stage Poll Processing

For each machine, on each `POST /poll`:

```
1. Call the simulated device API.
2. Timeout / 5xx / 404 / malformed JSON:
   -> increment failure counter; stop processing this machine.
3. Missing register / physical-envelope violation / invalid status:
   -> reject; no history; increment failure counter; stop.
4. Existing (device_id, sequence):
   -> duplicate; reset failure counter; stop.
5. Otherwise:
   -> store history unconditionally.
   -> reset failure counter.
   -> evaluate Stage B.
      - latest: update state and recalculate alarms 1–4.
      - history-only: no alarm recalculation.
6. After all machines have been attempted:
   -> evaluate COMMUNICATION_LOST once for every known machine.
7. Return the response.
```

**Concurrency:** `POST /poll` must attempt every known machine and remain correct when multiple polling operations and machine responses overlap. Poll all machines concurrently or by another non-serial strategy. The verifier judges only final outcomes: no lost updates, no duplicate history rows, no cross-machine interference, and correct latest state/alarms. No wall-clock performance threshold is imposed. The verifier avoids scheduler-dependent mixed-outcome races for the same machine.

## 9. REST API Contract

Candidate API endpoints:
```
GET  /machines
GET  /machines/{id}/status
GET  /machines/{id}/history?from=&to=&limit=
GET  /alarms?machine_id=&state=&severity=
POST /poll
```

`/health` is a liveness endpoint supplied by the starter and is not scored as part of the candidate contract. `POST /admin/reset` is a verifier-only test-isolation endpoint; it is not part of the candidate contract and candidates must not rely on it.

**`GET /machines`** — one entry per known machine:
```json
{"id":"machine-001","status":"RUNNING","last_seen":"2026-08-18T08:30:00Z","active_alarms":1}
```

**`GET /machines/{id}/status`** — latest state only:
```json
{"machine_id":"machine-001","status":"RUNNING","temperature_c":25.3,"pressure_kpa":101.32,"vibration_mm_s":4.2,"telemetry_timestamp":"2026-08-18T08:30:00Z"}
```

**`GET /machines/{id}/history`**
- `limit` defaults to 100; maximum 1000.
- `from` / `to` are inclusive UTC timestamps in the exact format `YYYY-MM-DDTHH:MM:SSZ`; omitted means unbounded.
- Always ordered `(timestamp ASC, sequence ASC)`.
- Includes every Stage-A accepted reading.

**`GET /alarms`** — filterable by `machine_id`, `state` (`ACTIVE`/`CLEARED`), and `severity` (`WARNING`/`CRITICAL`).

**`POST /poll`** — attempts every known machine per §8.

## 10. Scoring Components

| # | Component | Weight | Tests |
|---|---|---:|---|
| 1 | Register decoding | 20% | both maps, conversion, physical-envelope rejection, invalid status, missing registers |
| 2 | Historical storage & deduplication | 20% | every valid unique reading stored; composite-key dedup; strict history ordering |
| 3 | Latest-state ordering & restart | 20% | exact §5 predicates; history-only isolation; timestamp ties; restart boundaries |
| 4 | Alarm lifecycle | 20% | all 5 alarms, raise/clear, communication counter, history-only isolation |
| 5 | API + concurrency-safety + failures | 20% | endpoints, filters, pagination, overlapping polls, all defined failure modes |

## 11. Determinism and scope

- The simulator is local and controlled by the verifier.
- All simulator timestamps used for ordering are UTC `YYYY-MM-DDTHH:MM:SSZ` strings.
- "Malformed JSON" means a response body that cannot be parsed as JSON. Wrong field types are not generated by the v1 simulator.
- No runtime internet access is required or permitted.
- Do not modify the verifier or simulator contract.

## 12. Hidden Test Scenarios

The held-out matrix includes combinations of:
- multiple machines with independent sequence spaces;
- out-of-order valid readings that remain history-only;
- timestamp ties;
- restart boundaries around previous sequence 899/900 and new sequence 100/101;
- duplicate restart readings;
- normal-band violations that remain valid telemetry and raise alarms;
- physical-envelope violations that are rejected and count as communication failures;
- mixed timeout/5xx/404/malformed/missing/invalid-status failures;
- recovery after exactly three consecutive failures;
- dynamic machine registration and second-map decoding;
- overlapping polls where the verifier controls outcomes to avoid scheduler-dependent same-machine races.

## 13. Bundle Layout

```
industrial-edge-telemetry-gateway/
├── task.toml
├── instruction.md
├── environment/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   └── simulator/
├── tests/
│   ├── test.sh
│   └── verifier_runner.py
└── solution/
    ├── app/
    └── solve.sh
```

**Important:** the verifier and simulator are part of the evaluation harness. The candidate is responsible only for implementing the gateway under `environment/app` according to this specification.
