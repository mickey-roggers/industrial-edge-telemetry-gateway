# Industrial Edge Telemetry Gateway

You are building the **gateway** for an Industrial Edge Telemetry system. A fleet
of machines is exposed through a simulated device API. Your gateway polls those
devices, decodes their register readings into engineering values, stores a
tamper-evident history, maintains each machine's current "live" state, and raises
operational alarms. A separate, already-built **simulator** stands in for the
machine fleet and lets the verifier inject failures and restart events.

> You implement **only the gateway** (a FastAPI app). The simulator, the verifier,
> and the failure-injection control API already exist and must not be modified.

---

## 1. Scope

Implement a FastAPI service that exposes exactly these five endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/poll` | Poll **every known machine once**, decode, store, evaluate alarms. |
| `GET`  | `/machines` | Summary per known machine (id, status, last_seen, active_alarms). |
| `GET`  | `/machines/{id}/status` | Latest live state for one machine. |
| `GET`  | `/machines/{id}/history?from=&to=&limit=` | Stored history (§4), filtered + paginated. |
| `GET`  | `/alarms?machine_id=&state=&severity=` | Alarm list (§7), filterable. |

Your gateway must discover the fleet from the simulator at startup and also
re-sync the fleet on each poll (the verifier registers devices *after* startup).

---

## 2. Devices & register maps

Each device has a `register_map` (its decoding scheme). Two maps are supported:

| Register | Meaning | `standard-v1` | `high-temp-v1` |
|----------|---------|---------------|----------------|
| `40001`  | temperature | units of 0.1 → `/10`, range −40…120 °C | units of 0.1 → `/10`, range −40…180 °C |
| `40002`  | pressure    | units of 0.01 → `/100`, range 0…1500 kPa | units of 0.01 → `/100`, range 0…2000 kPa |
| `40003`  | vibration   | units of 0.1 → `/10`, range 0…50 mm/s | units of 0.1 → `/10`, range 0…60 mm/s |
| `40004`  | status code | `0=STOPPED, 1=RUNNING, 2=FAULT, 3=MAINTENANCE` | same, **but code 3 is invalid for `standard-v1`** |

Engineering value = `raw / scaling`. So `40001 = 253` → `25.3 °C`, `40001 = 1500` → `150.0 °C`.

---

## 3. Decoding & Stage A (reject, don't crash)

For each polled device, fetch `GET /devices/{id}/registers`. A reading is:

```
{ "sequence": int, "timestamp": ISO-8601 str, "registers": {40001:int, 40002:int, 40003:int, 40004:int} }
```

Decode it. **Reject the entire reading** (do not store, count as a failure) if any of:
- a required register is missing,
- a decoded value is outside the **physical acceptance envelope**
  (temperature −273.15…1000 °C, pressure −100…50000 kPa, vibration −100…1000 mm/s),
- the status code is not valid for that map.

> Note the deliberate tension with §7: a value that merely *exceeds an operational
> limit* (e.g. temperature above the map's max) is **still stored and still drives
> an alarm** — only physically impossible values are rejected at Stage A.

Transport/syntax failures (timeout, HTTP 5xx, 404, malformed JSON, missing
register) count toward the `COMMUNICATION_LOST` counter (§6) but are **not** Stage-A
rejections.

---

## 4. History (Stage A write)

Store **every** newly-decoded, uniquely-sequenced, valid reading in history. The
history key is the **composite `(device_id, sequence)`** — never overwrite, never
duplicate. History rows must be returned ordered by `(timestamp ASC, sequence ASC)`.
Support `from`/`to` (inclusive ISO-8601) and `limit` (default 100, max 1000).

---

## 5. Latest-state predicate (Stage B)

A reading becomes the machine's **latest state** only if:

- it is the **first** reading for the device, **or**
- **normal progression**: `sequence` strictly greater AND `timestamp` strictly greater than current, **or**
- **restart**: `sequence` went *backwards* AND `timestamp` strictly greater AND the previous latest `sequence ≥ 900` AND the new `sequence ≤ 100`.

Tied timestamps (`ts == current_ts`) satisfy **neither** predicate → history-only.
A reading that is accepted into history but fails Stage B is **history-only**: it
must **never** change the latest state and must **never** re-trigger an alarm.

---

## 6. COMMUNICATION_LOST (Path B)

A single per-machine counter of *consecutive* poll failures (timeout, 5xx, 404,
malformed, Stage-A rejection). Reset to 0 on any successful valid or duplicate
reading. When the counter reaches **3**, raise `COMMUNICATION_LOST` (CRITICAL).
This is evaluated **once per `POST /poll`**, after all machines are attempted —
not per-machine inline — so a single poll cycle with three failures on one machine
raises it, and a later valid reading clears it.

---

## 7. Telemetry alarms (Path A)

Four alarms evaluated **only when a reading becomes the latest state** (never on
history-only readings):

| Alarm | Condition | Severity |
|-------|-----------|----------|
| `HIGH_TEMPERATURE` | `temperature_c > map max` | WARNING |
| `HIGH_VIBRATION` | `vibration_mm_s ≥ 15.0` | WARNING |
| `CRITICAL_VIBRATION` | `vibration_mm_s ≥ 25.0` | CRITICAL |
| `PRESSURE_FAULT` | `pressure_kpa < map min` | CRITICAL |

Raise on first true evaluation; clear only on a later false evaluation **on the
same path**. The five alarm types (`HIGH_TEMPERATURE`, `HIGH_VIBRATION`,
`CRITICAL_VIBRATION`, `PRESSURE_FAULT`, `COMMUNICATION_LOST`) are independent
instances; do not merge paths or states.

---

## 8. Poller (concurrency)

`POST /poll` polls **all** known machines **concurrently** (async). Each machine's
outcome is recorded atomically (per-machine lock) so overlapping polls cannot
corrupt a row or lose a counter update. Return a small JSON summary
(`{stored, history_only, duplicates, failures, machines:[...]}`).

---

## 9. API contract (exact)

- `GET /machines` → list of `{id, status, last_seen, active_alarms}` (status may be
  `null` if no reading yet; `active_alarms` = count of ACTIVE alarms for that machine).
- `GET /machines/{id}/status` → `{machine_id, status, temperature_c, pressure_kpa,
  vibration_mm_s, telemetry_timestamp}`; `404` if unknown; `null` fields if no state yet.
- `GET /machines/{id}/history?from=&to=&limit=` → `{machine_id, readings:[...]}`.
- `GET /alarms?machine_id=&state=&severity=` → list of `{machine_id, alarm_type,
  state, severity}`.

Do not add, remove, or rename response **fields**. Adding extra endpoints (e.g.
`/health`) is allowed and does not affect scoring.

---

## 10. Failure-injection control API (verifier-only — do NOT implement)

The verifier drives the simulator via:
`POST /control/reset`, `POST /control/device`, `POST /control/device/{id}/script`,
`POST /control/device/{id}/enqueue`, `GET /control/device/{id}/queue`.
Modes include `normal`, `timeout`, `5xx`, `notfound`, `malformed`, `missing`,
`outofrange`, `invalidstatus`. Your gateway only consumes `GET /devices` and
`GET /devices/{id}/registers`.

---

## 11. What you are given

- `environment/app/` — a scaffold (you may use it or start fresh).
- `environment/simulator/` — the simulator (read-only).
- `tests/` — the sealed verifier; treat it as the grading oracle.
- `solution/app/` — a reference implementation (do not copy; it is the answer key).

## 12. Acceptance

`bash tests/test.sh` must exit `0`. It starts the simulator + your gateway, drives
all five components (decode, history/dedup, latest-state/restart, alarm lifecycle,
API/concurrency/failures) including hidden scenarios, and scores each component
0/full (20% each). The verifier is sealed; make your gateway pass it.
