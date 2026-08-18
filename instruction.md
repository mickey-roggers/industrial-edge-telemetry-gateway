# Industrial Edge Telemetry Gateway

You are building a complete **Odyssey task bundle** called the **Industrial Edge Telemetry Gateway**. This is a systems-integration coding task: a FastAPI service that polls simulated industrial machines, decodes PLC-style registers, maintains correct historical and live state under out-of-order/duplicate/restart conditions, runs a deterministic alarm engine, and exposes a REST API — all under concurrency and simulated failure modes.

The specification below is **locked and final**. Do not reinterpret, simplify, or "improve" any rule. If you find a genuine contradiction, stop and flag it before writing code; do not silently resolve it in whichever direction seems reasonable.

Work in this exact order and do not skip ahead:

1. Build the **reference solution** (`solution/solve.sh` + full working app) directly against §4–§9 below.
2. **Hand-trace** the reference against every case in the "Hidden Test Scenarios" list (§12) before writing any verifier code.
3. Only once the reference passes the hand-trace, implement `tests/test.sh` (visible + hidden, per §12).
4. Build the **incomplete starter app** by stripping the working reference down to stubs (`NotImplementedError` and a few subtly-wrong implementations).
5. Produce `task.toml`, `Dockerfile`, `requirements.txt`, and the final bundle ZIP per the layout in §13.

---

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
| difficultyExplanation | Difficulty lives in four places: (1) separating historical storage from latest-state updates — most naive implementations collapse the two; (2) the exact restart predicate, which requires reasoning about sequence AND timestamp AND a high-water-mark threshold simultaneously, not just sorting; (3) concurrency-safety of the poll path under overlapping requests and partial failures; (4) the interaction between failure modes and the COMMUNICATION_LOST counter, where a "valid duplicate" and an "out-of-range value" must be treated differently even though both are terminal outcomes for that reading. |
| expertTimeEstimateHours | 14 |
| oracleStrategy | The reference solution under `solution/` implements the exact two-stage acceptance model (§4), the latest-state predicates (§5), the COMMUNICATION_LOST counter (§6), and the alarm state machine (§7), and drives every visible and hidden verifier scenario to full reward. |
| verificationStrategy | The sealed verifier under `tests/` exercises five independent components (§10). Visible tests cover each mechanism in isolation; hidden tests inject multi-machine interleaved sequences, restarts, duplicates, mixed failure modes, and concurrent overlapping polls. Scoring is additive. |
| binarySuccessCondition | The implementation passes every required check in all five scoring components — including the full hidden matrix — and produces correct history, latest state, and alarm transitions for every verifier scenario without modifying the test harness or simulator contract. |
| partialScoreStrategy | Each of the five components (§10) is scored independently, 0 or full weight per component. No component compensates for a failed one. Scores are additive. |
| environmentSummary | Python 3.12, FastAPI, httpx, uvicorn, SQLite, asyncio. Fully offline — no runtime network. |
| resourceEstimate | cpus: 2, memory_mb: 4096, storage_mb: 2048, gpus: 0, agentTimeoutSec: 18000 (5h), verifierTimeoutSec: 1800 |
| networkRequirements | none — `network_mode: none` for both `[environment]` (build) and `[agent]` (rollout) |

---

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

**Core design principle — this is the single rule that drives the entire task's difficulty:**
A reading being **stored in history** and a reading becoming the machine's **current live state** are two separate decisions. A valid, uniquely-sequenced reading is always stored. It only becomes the new "latest state" — and therefore only feeds the alarm engine — if it also satisfies the ordering/restart predicate in §5.

---

## 3. Register Maps (fully specified — implement exactly, do not invent additional maps)

| Register | Meaning | Conversion | `standard-v1` range | `high-temp-v1` range |
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

**Rejection rule:** any missing required register, any decoded value outside its map's range, or an unrecognized status code for that map causes the **entire reading** to be rejected — no history entry, no state update, counts as a failure toward COMMUNICATION_LOST (§6). Which map applies is a property of the device (`register_map` field from `GET /devices`) — do not hard-code a single map; the hidden verifier tests both.

---

## 4. Historical Acceptance (Stage A)

A reading is stored in history **if and only if all four hold**:

1. The poll response was syntactically valid (not a timeout, 5xx, 404, or malformed JSON).
2. `(device_id, sequence)` has never been stored before.
3. All four required registers are present.
4. Every decoded value is inside that device's map-specific range, and the status code is valid for that map.

If stored, the reading is written to history **unconditionally** — independent of whether it will become the new latest state (§5). History is always returned ordered by `(timestamp ASC, sequence ASC)`.

---

## 5. Latest-State Update (Stage B)

After a reading passes Stage A, it becomes the new **latest state** only if one of these two predicates holds. Implement exactly this — do not substitute "highest sequence wins" or any other simplification:

**Normal progression**
```
sequence  > current_latest_sequence[device]
timestamp > current_latest_timestamp[device]
```

**Restart** (device reset its sequence counter)
```
sequence  < current_latest_sequence[device]
timestamp > current_latest_timestamp[device]
current_latest_sequence[device] >= 900
sequence <= 100
```

Both branches require `timestamp >` strictly — this alone resolves timestamp ties (a tied timestamp fails both predicates, so the reading is stored in history but never becomes latest state).

If neither predicate holds, the reading is **history-only**: it stays in `GET /machines/{id}/history` but does not change `GET /machines/{id}/status`, and it **does not trigger alarm recalculation** (§7).

**Worked example — why "highest sequence" alone is wrong:**

| Event | seq | ts | Becomes latest state? | Why |
|---|---|---|---|---|
| 1 | 100 | 10:00 | yes | first reading |
| 2 | 102 | 10:02 | yes | seq↑, ts↑ |
| 3 | 101 | 10:01 (late arrival) | no | ts 10:01 not > 10:02 — history only |
| 4 | 997 | 10:05 | yes | seq↑, ts↑ |
| 5 | 0 | 10:06 | yes (restart) | seq<current, ts↑, prev seq ≥900, new seq ≤100 |
| 6 | 0 | 10:06 (dup) | no | rejected at Stage A — (device, 0) already stored |
| 7 | 50 | 10:07 | yes | normal progression after restart |

---

## 6. COMMUNICATION_LOST Counter

Each machine keeps one consecutive-unsuccessful-poll counter, evaluated once, after every `POST /poll`.

| Poll outcome for this machine | Counter |
|---|---|
| New reading stored in history (whether or not it becomes latest state) | reset to 0 |
| Valid duplicate — `(device_id, sequence)` already stored, response otherwise well-formed | reset to 0 |
| Timeout | +1 |
| HTTP 5xx | +1 |
| HTTP 404 | +1 |
| Malformed JSON | +1 |
| Missing required register | +1 |
| Value outside map range / invalid status code | +1 |

When the counter reaches **3**, `COMMUNICATION_LOST` becomes `ACTIVE` for that machine. A device that keeps responding with valid (even duplicate or stale) data is NOT considered lost — only one that stops responding usefully.

---

## 7. Alarm State Machine

One alarm instance exists per `(machine_id, alarm_type)`, with **two independent trigger paths** — do not merge them:

| Alarm type | Raise condition | Severity | Recalculated when |
|---|---|---|---|
| HIGH_TEMPERATURE | `temperature_c > map max` | WARNING | reading becomes latest state |
| HIGH_VIBRATION | `vibration_mm_s >= 15.0` | WARNING | reading becomes latest state |
| CRITICAL_VIBRATION | `vibration_mm_s >= 25.0` | CRITICAL | reading becomes latest state |
| PRESSURE_FAULT | `pressure_kpa < map min` | CRITICAL | reading becomes latest state |
| COMMUNICATION_LOST | consecutive-failure counter ≥ 3 | CRITICAL | every `POST /poll` |

Rules:
- Raise to `ACTIVE` the first time a condition becomes true (via its trigger path above).
- Clear to `CLEARED` only when a subsequent evaluation on that same trigger path makes the condition false.
- No debounce/hysteresis window in v1 — a single qualifying evaluation is enough to flip state.
- **Critical:** a history-only reading (§5) never triggers alarms 1–4, even if its decoded values would otherwise satisfy a raise or clear condition. Alarms 1–4 recalculate ONLY on a latest-state update. COMMUNICATION_LOST is evaluated separately, once per poll, regardless of latest-state outcome.
- `active_alarms` in `GET /machines` counts alarms currently `ACTIVE` for that machine, across all five types.

---

## 8. Two-Stage Poll Processing — Full Logic (implement exactly this control flow)

For each machine, on each `POST /poll`:

```
1. Call the simulated device API for this machine.
2. If response is not syntactically valid (timeout / 5xx / 404 / malformed JSON):
   -> increment consecutive-failure counter for this machine. STOP (this machine done).
3. If any required register is missing, OR any value is outside the map range,
   OR the status code is invalid for that map:
   -> reject the reading entirely (no history row).
   -> increment consecutive-failure counter. STOP.
4. If (device_id, sequence) already stored:
   -> duplicate. Reset consecutive-failure counter to 0. STOP (no new history row).
5. Otherwise (new, valid reading):
   -> store in history (Stage A / §4, unconditional).
   -> reset consecutive-failure counter to 0.
   -> evaluate Stage B predicate (§5).
       - if satisfied: update latest_state, then recalculate alarms 1-4 (§7).
       - if not satisfied: history-only. No further writes.
6. After ALL machines have been attempted in this POST /poll:
   -> evaluate COMMUNICATION_LOST per machine based on its current counter (§6-7).
7. Return the response.
```

**Concurrency:** `POST /poll` must attempt every known machine and remain correct when multiple polling operations and machine responses overlap. Use `asyncio.gather()` (or equivalent) to poll concurrently. Judge correctness **only by final outcomes** (history, latest state, alarms). What IS required: no lost updates, no partial writes, no duplicate history rows, and no cross-machine interference. Each machine's writes are scoped by its own `device_id` — use per-row/transactional writes (SQLite transactions) so two overlapping polls of the *same* machine can't corrupt one row.

---

## 9. REST API Contract (exact — do not add, remove, or rename fields)

```
GET  /machines
GET  /machines/{id}/status
GET  /machines/{id}/history?from=&to=&limit=
GET  /alarms?machine_id=&state=&severity=
POST /poll
```

**`GET /machines`** — one entry per known machine:
```json
{"id": "machine-001", "status": "RUNNING", "last_seen": "2026-08-18T08:30:00Z", "active_alarms": 1}
```

**`GET /machines/{id}/status`** — reflects latest state only, never a history-only reading:
```json
{"machine_id": "machine-001", "status": "RUNNING", "temperature_c": 25.3,
 "pressure_kpa": 101.32, "vibration_mm_s": 4.2, "telemetry_timestamp": "2026-08-18T08:30:00Z"}
```

**`GET /machines/{id}/history`**
- `limit` defaults to **100** if omitted; maximum allowed is **1000**.
- `from` / `to` are inclusive ISO-8601 timestamps; omitted means unbounded on that side.
- Always ordered `(timestamp ASC, sequence ASC)`.
- Includes every reading accepted at Stage A, regardless of whether it became latest state.

**`GET /alarms`** — filterable by `machine_id`, `state` (ACTIVE/CLEARED), `severity` (WARNING/CRITICAL).

**`POST /poll`** — attempts every known machine per §8.

---

## 10. Scoring Components

| # | Component | Weight | Tests |
|---|---|---|---|
| 1 | Register decoding | 20% | correct conversion for both maps, range rejection, invalid status codes, missing registers |
| 2 | Historical storage & deduplication | 20% | every valid unique reading stored; composite key `(device_id, sequence)` respected; duplicates never double-stored |
| 3 | Latest-state ordering & restart | 20% | exact §5 predicates; history-only readings never leak into `/status`; restart boundary cases (seq ≥900→≤100) |
| 4 | Alarm lifecycle | 20% | raise/clear on all 5 types; COMMUNICATION_LOST counter behavior; history-only readings never trigger alarms 1-4 |
| 5 | API + concurrency-safety + failures | 20% | correct endpoints/filters/pagination; final-outcome correctness under overlapping polls; every failure mode handled per §6 |

Binary success: all required checks in all five components pass, including the full hidden matrix, without modifying the test harness or simulator contract.

---

## 11. Anticipated Exploits — the verifier must defeat ALL of these

| Exploit | Defense |
|---|---|
| Hard-coding visible machine IDs | Hidden tests generate additional machine IDs dynamically |
| Hard-coding one register map / its ranges | Hidden tests exercise `high-temp-v1` with values only valid there |
| Treating arrival order as sequence order | Hidden tests shuffle delivery order |
| Collapsing "history" with "latest state" | Verifier checks `/history` and `/status` independently |
| Mis-implementing the restart predicate | Hidden tests probe exact boundaries (seq 900/901, seq 100/101, tied timestamps) |
| Returning canned API responses without real state | Verifier drives state through `/poll` then reads it back through unrelated endpoints |
| Unsafe concurrent writes (lost updates, duplicate rows) | Concurrent overlapping poll scenario with assertions on final row counts and state |
| Reading held-out test data | Held-out scenarios and grading logic sealed under `tests/`, never exposed to the agent |
| Disabling validation to avoid rejection paths | Invalid values interleaved with valid ones; downstream `/status` and `/alarms` checked for correctness |
| Letting a history-only reading re-trigger alarms | Explicit hidden case: late out-of-range reading arrives after a newer in-range one — alarm must NOT flip |

---

## 12. Visible vs. Hidden Test Scenarios

**Visible (shipped in the starter bundle, agent can see and run them):**
- Single-machine happy-path decode, one machine, one map
- Basic duplicate rejection
- Simple out-of-order reading that does NOT trigger a restart
- One alarm raise + one clear, single alarm type
- Two-machine concurrent poll, no failures

**Hidden (sealed under `tests/`, combinatorial):**
- Multi-machine interleaved sequences including a legitimate restart, followed by a duplicate of the restart reading
- Mixed valid / missing-register / 503 / malformed responses within a single poll cycle
- Timestamp-tie case (reading stored, state unchanged)
- COMMUNICATION_LOST activation at exactly the 3rd consecutive failure, and recovery via a valid duplicate
- Restart boundary cases: seq 900 vs 899, seq 100 vs 101
- Late/history-only reading with out-of-range values that must NOT flip an alarm
- Dynamically generated extra machine IDs and the second register map together
- Overlapping concurrent polls with interleaved failures across machines
