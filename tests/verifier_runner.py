"""Sealed verifier runner for the Industrial Edge Telemetry Gateway.

Drives the gateway through HTTP using the bundled simulator's control API, and
scores five independent components. Each component is 0 or full weight;
scoring is additive. Hidden scenarios use dynamically generated machine IDs and
both register maps, shuffle delivery order, probe restart boundaries, and check
/history vs /status independently so the history/state-collapse exploit cannot
pass.

NOTE: this file is intentionally verbose and sealed. It is the grading oracle.
"""
import json
import os
import random
import sys
import time

import httpx

SIM = os.environ["VERIFIER_SIM_URL"]
GW = os.environ["VERIFIER_GW_URL"]

FAILURES = []
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond)))
    if not cond:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name}: {detail}")
    else:
        print(f"  [ok]   {name}")


def sim(method, path, **kw):
    r = httpx.request(method, SIM + path, timeout=10, **kw)
    return r


def gw(method, path, **kw):
    r = httpx.request(method, GW + path, timeout=15, **kw)
    return r


def reset_fleet():
    sim("POST", "/control/reset")
    # also wipe gateway state so each scenario is fully isolated
    try:
        gw("POST", "/admin/reset")
    except Exception:
        pass


def register(mid, rmap):
    sim("POST", "/control/device", json={"id": mid, "register_map": rmap})


def known(mid):
    return [d["id"] for d in sim("GET", "/devices").json()]


def script(mid, responses):
    sim("POST", f"/control/device/{mid}/script", json={"responses": responses})


def enqueue(mid, spec):
    sim("POST", f"/control/device/{mid}/enqueue", json=spec)


def poll():
    return gw("POST", "/poll").json()


def run_polls(n):
    """The simulator serves one queued reading per GET, and the gateway's
    POST /poll performs exactly one GET per device. Enqueuing N readings for a
    device therefore requires N polls to drain them all."""
    out = []
    for _ in range(n):
        out.append(gw("POST", "/poll").json())
    return out


# Standard normal register payload constructor.
def normals(mid, specs):
    """specs: list of (seq, ts) or (seq, ts, regs_override|None)."""
    out = []
    for spec in specs:
        if len(spec) == 3:
            seq, ts, regs = spec
        else:
            seq, ts = spec
            regs = None
        out.append({"mode": "normal", "sequence": seq, "timestamp": ts,
                    "registers": regs if regs is not None else None})
    return out


def REG(over=None):
    base = {40001: 250, 40002: 10132, 40003: 42, 40004: 1}  # 25.0C,101.32,4.2,RUNNING
    if over:
        base.update(over)
    return base


# ============================================================
# Component 1 (20%): register decoding (both maps, rejection, invalid status)
# ============================================================
def component1_decode():
    print("\n=== Component 1: register decoding ===")
    reset_fleet()
    # standard-v1 decode
    register("d-std", "standard-v1")
    script("d-std", normals("d-std", [(1, "2026-08-18T10:00:00Z",
            {40001: 253, 40002: 10132, 40003: 42, 40004: 1})]))
    poll()
    st = gw("GET", "/machines/d-std/status").json()
    check("C1.std.temperature", st["temperature_c"] == 25.3, st)
    check("C1.std.pressure", st["pressure_kpa"] == 101.32, st)
    check("C1.std.vibration", st["vibration_mm_s"] == 4.2, st)
    check("C1.std.status", st["status"] == "RUNNING", st)

    # high-temp-v1 decode (temp range wider; value valid only here)
    reset_fleet()
    register("d-ht", "high-temp-v1")
    # temperature 150.0 C -> raw 1500 ; valid in high-temp (<=180) but NOT standard (<=120)
    script("d-ht", normals("d-ht", [(1, "2026-08-18T10:00:00Z",
            {40001: 1500, 40002: 50000, 40003: 100, 40004: 3})]))
    poll()
    st = gw("GET", "/machines/d-ht/status").json()
    check("C1.ht.temperature", st["temperature_c"] == 150.0, st)
    check("C1.ht.pressure", st["pressure_kpa"] == 500.0, st)
    check("C1.ht.vibration", st["vibration_mm_s"] == 10.0, st)
    check("C1.ht.status_maintenance", st["status"] == "MAINTENANCE", st)

    # out-of-range rejection (temperature out of any range)
    reset_fleet()
    register("d-oor", "standard-v1")
    script("d-oor", [{"mode": "outofrange"}])
    r = poll()
    check("C1.oor.failure_counted", r["failures"] == 1, r)
    st = gw("GET", "/machines/d-oor/status").json()
    check("C1.oor.no_status", st["status"] is None, st)
    hist = gw("GET", "/machines/d-oor/history").json()
    check("C1.oor.no_history", len(hist["readings"]) == 0, hist)

    # invalid status code for map -> rejection
    reset_fleet()
    register("d-is", "standard-v1")
    script("d-is", [{"mode": "invalidstatus"}])  # status code 42 invalid for standard-v1
    r = poll()
    check("C1.invstat.failure_counted", r["failures"] == 1, r)
    hist = gw("GET", "/machines/d-is/history").json()
    check("C1.invstat.no_history", len(hist["readings"]) == 0, hist)

    # missing required register -> rejection
    reset_fleet()
    register("d-mr", "standard-v1")
    script("d-mr", [{"mode": "missing"}])
    r = poll()
    check("C1.missing.failure_counted", r["failures"] == 1, r)
    hist = gw("GET", "/machines/d-mr/history").json()
    check("C1.missing.no_history", len(hist["readings"]) == 0, hist)


# ============================================================
# Component 2 (20%): historical storage & dedup (composite key)
# ============================================================
def component2_history():
    print("\n=== Component 2: historical storage & dedup ===")
    reset_fleet()
    register("h1", "standard-v1")
    script("h1", normals("h1", [(1, "2026-08-18T10:00:00Z")]))
    poll()
    # duplicate (same device, same sequence) must NOT be double-stored
    script("h1", normals("h1", [(1, "2026-08-18T10:00:00Z")]))
    r = poll()
    check("C2.dup.stored_count", r["stored"] == 0, r)
    hist = gw("GET", "/machines/h1/history").json()
    check("C2.dup.unique_rows", len(hist["readings"]) == 1, hist)
    # distinct sequence stored as separate row
    script("h1", normals("h1", [(2, "2026-08-18T10:01:00Z")]))
    poll()
    hist = gw("GET", "/machines/h1/history").json()
    check("C2.distinct.two_rows", len(hist["readings"]) == 2, hist)
    # history ordering (timestamp asc, sequence asc)
    seqs = [(rd["sequence"], rd["timestamp"]) for rd in hist["readings"]]
    check("C2.order.sorted", seqs == sorted(seqs, key=lambda x: (x[1], x[0])), seqs)
    # history present even when reading is history-only (status unchanged)
    script("h1", normals("h1", [(3, "2026-08-18T09:00:00Z")]))
    poll()
    hist = gw("GET", "/machines/h1/history").json()
    check("C2.history_only.stored", len(hist["readings"]) == 3, hist)
    st = gw("GET", "/machines/h1/status").json()
    # status should still reflect seq2 (latest), not seq3 (older ts)
    check("C2.history_only.status_preserved", st["telemetry_timestamp"] == "2026-08-18T10:01:00Z", st)


# ============================================================
# Component 3 (20%): latest-state ordering & restart (§5)
# ============================================================
def component3_latest_state():
    print("\n=== Component 3: latest-state ordering & restart ===")
    reset_fleet()
    register("l1", "standard-v1")
    # worked example from §5: 100->102->101(late)->997->0(restart)->0(dup)->50
    script("l1", normals("l1", [(100, "2026-08-18T10:00:00Z")]))
    poll()
    st = gw("GET", "/machines/l1/status").json()
    check("C3.e1.first_latest", st["telemetry_timestamp"] == "2026-08-18T10:00:00Z", st)
    script("l1", normals("l1", [(102, "2026-08-18T10:02:00Z")]))
    poll()
    st = gw("GET", "/machines/l1/status").json()
    check("C3.e2.normal_progression", st["telemetry_timestamp"] == "2026-08-18T10:02:00Z", st)
    script("l1", normals("l1", [(101, "2026-08-18T10:01:00Z")]))  # late, history only
    poll()
    st = gw("GET", "/machines/l1/status").json()
    check("C3.e3.late_history_only", st["telemetry_timestamp"] == "2026-08-18T10:02:00Z", st)
    script("l1", normals("l1", [(997, "2026-08-18T10:05:00Z")]))
    poll()
    st = gw("GET", "/machines/l1/status").json()
    check("C3.e4.normal_high_seq", st["telemetry_timestamp"] == "2026-08-18T10:05:00Z", st)
    script("l1", normals("l1", [(0, "2026-08-18T10:06:00Z")]))  # restart (prev 997>=900, new 0<=100)
    poll()
    st = gw("GET", "/machines/l1/status").json()
    check("C3.e5.restart_applied", st["telemetry_timestamp"] == "2026-08-18T10:06:00Z", st)
    script("l1", normals("l1", [(0, "2026-08-18T10:06:00Z")]))  # dup at Stage A
    poll()
    hist = gw("GET", "/machines/l1/history").json()
    check("C3.e6.restart_dup_not_stored", len(hist["readings"]) == 5, hist)
    script("l1", normals("l1", [(50, "2026-08-18T10:07:00Z")]))
    poll()
    st = gw("GET", "/machines/l1/status").json()
    check("C3.e7.post_restart_progression", st["telemetry_timestamp"] == "2026-08-18T10:07:00Z", st)

    # restart boundary: prev seq 899 (NOT >=900) -> seq 0 must NOT restart
    reset_fleet()
    register("lb1", "standard-v1")
    script("lb1", normals("lb1", [(899, "2026-08-18T10:00:00Z")]))
    poll()
    script("lb1", normals("lb1", [(0, "2026-08-18T10:01:00Z")]))
    poll()
    st = gw("GET", "/machines/lb1/status").json()
    check("C3.bound.899_no_restart", st["telemetry_timestamp"] == "2026-08-18T10:00:00Z", st)

    # restart boundary: from seq 900, new seq 101 (NOT <=100) must NOT restart
    reset_fleet()
    register("lb2", "standard-v1")
    script("lb2", normals("lb2", [(900, "2026-08-18T10:00:00Z")]))
    poll()
    script("lb2", normals("lb2", [(101, "2026-08-18T10:01:00Z")]))
    poll()
    st = gw("GET", "/machines/lb2/status").json()
    check("C3.bound.101_no_restart", st["telemetry_timestamp"] == "2026-08-18T10:00:00Z", st)
    # but seq 100 (<=100) from 900 DOES restart
    script("lb2", normals("lb2", [(100, "2026-08-18T10:02:00Z")]))
    poll()
    st = gw("GET", "/machines/lb2/status").json()
    check("C3.bound.100_restart", st["telemetry_timestamp"] == "2026-08-18T10:02:00Z", st)

    # timestamp tie: tied ts fails both predicates -> history only
    reset_fleet()
    register("lt1", "standard-v1")
    script("lt1", normals("lt1", [(1, "2026-08-18T10:00:00Z")]))
    poll()
    script("lt1", normals("lt1", [(2, "2026-08-18T10:00:00Z")]))  # tied ts
    poll()
    st = gw("GET", "/machines/lt1/status").json()
    check("C3.tie.status_unchanged", st["telemetry_timestamp"] == "2026-08-18T10:00:00Z", st)
    hist = gw("GET", "/machines/lt1/history").json()
    check("C3.tie.history_has_both", len(hist["readings"]) == 2, hist)


# ============================================================
# Component 4 (20%): alarm lifecycle (5 types, COMM_LOST, history-only)
# ============================================================
def component4_alarms():
    print("\n=== Component 4: alarm lifecycle ===")
    # HIGH_TEMPERATURE (only valid above map max)
    reset_fleet()
    register("a1", "standard-v1")
    script("a1", normals("a1", [(1, "2026-08-18T10:00:00Z",
            {40001: 1210, 40002: 10132, 40003: 42, 40004: 1})]))  # temp 121.0 > 120 max
    poll()
    alarms = gw("GET", "/alarms").json()
    ht = [a for a in alarms if a["alarm_type"] == "HIGH_TEMPERATURE"]
    check("C4.hightemp.active", ht and ht[0]["state"] == "ACTIVE", alarms)
    check("C4.hightemp.warning", ht and ht[0]["severity"] == "WARNING", alarms)
    # back to normal temp -> clears
    script("a1", normals("a1", [(2, "2026-08-18T10:01:00Z")]))
    poll()
    alarms = gw("GET", "/alarms").json()
    ht = [a for a in alarms if a["alarm_type"] == "HIGH_TEMPERATURE"]
    check("C4.hightemp.cleared", ht and ht[0]["state"] == "CLEARED", alarms)

    # HIGH / CRITICAL vibration
    reset_fleet()
    register("a2", "standard-v1")
    script("a2", normals("a2", [(1, "2026-08-18T10:00:00Z",
            {40001: 250, 40002: 10132, 40003: 150, 40004: 1})]))  # vib 15.0
    poll()
    alarms = gw("GET", "/alarms").json()
    hv = [a for a in alarms if a["alarm_type"] == "HIGH_VIBRATION"]
    cv = [a for a in alarms if a["alarm_type"] == "CRITICAL_VIBRATION"]
    check("C4.highvib.active", hv and hv[0]["state"] == "ACTIVE", alarms)
    check("C4.critvib.inactive_at_15", cv and cv[0]["state"] == "CLEARED", alarms)
    script("a2", normals("a2", [(2, "2026-08-18T10:01:00Z",
            {40001: 250, 40002: 10132, 40003: 250, 40004: 1})]))  # vib 25.0
    poll()
    alarms = gw("GET", "/alarms").json()
    cv = [a for a in alarms if a["alarm_type"] == "CRITICAL_VIBRATION"]
    check("C4.critvib.active_at_25", cv and cv[0]["state"] == "ACTIVE", alarms)
    check("C4.critvib.critical", cv and cv[0]["severity"] == "CRITICAL", alarms)

    # PRESSURE_FAULT (pressure < map min)
    reset_fleet()
    register("a3", "standard-v1")
    script("a3", normals("a3", [(1, "2026-08-18T10:00:00Z",
            {40001: 250, 40002: 0, 40003: 42, 40004: 1})]))  # pressure 0.0 == min, not < min
    poll()
    alarms = gw("GET", "/alarms").json()
    pf = [a for a in alarms if a["alarm_type"] == "PRESSURE_FAULT"]
    check("C4.pressure.min_not_fault", pf and pf[0]["state"] == "CLEARED", alarms)
    script("a3", normals("a3", [(2, "2026-08-18T10:01:00Z",
            {40001: 250, 40002: -100, 40003: 42, 40004: 1})]))  # pressure -1.0 < 0 min
    poll()
    alarms = gw("GET", "/alarms").json()
    pf = [a for a in alarms if a["alarm_type"] == "PRESSURE_FAULT"]
    check("C4.pressure.fault_active", pf and pf[0]["state"] == "ACTIVE", alarms)

    # COMMUNICATION_LOST at exactly 3rd consecutive failure, recover via dup
    reset_fleet()
    register("a4", "standard-v1")
    for i in range(3):
        script("a4", [{"mode": "5xx"}])
        poll()
    alarms = gw("GET", "/alarms").json()
    cl = [a for a in alarms if a["alarm_type"] == "COMMUNICATION_LOST"]
    check("C4.commlost.active_at_3", cl and cl[0]["state"] == "ACTIVE", alarms)
    # recovery: valid reading resets counter
    script("a4", normals("a4", [(1, "2026-08-18T10:00:00Z")]))
    poll()
    alarms = gw("GET", "/alarms").json()
    cl = [a for a in alarms if a["alarm_type"] == "COMMUNICATION_LOST"]
    check("C4.commlost.cleared_on_valid", cl and cl[0]["state"] == "CLEARED", alarms)

    # history-only reading must NOT flip an alarm (explicit hidden case)
    reset_fleet()
    register("a5", "standard-v1")
    script("a5", normals("a5", [(1, "2026-08-18T10:00:00Z",
            {40001: 250, 40002: 10132, 40003: 250, 40004: 1})]))  # crit vib active
    poll()
    alarms = gw("GET", "/alarms").json()
    cv = [a for a in alarms if a["alarm_type"] == "CRITICAL_VIBRATION"]
    check("C4.flip.initial_active", cv and cv[0]["state"] == "ACTIVE", alarms)
    # newer in-range reading clears the alarm (latest state)
    script("a5", normals("a5", [(2, "2026-08-18T10:02:00Z")]))
    poll()
    alarms = gw("GET", "/alarms").json()
    cv = [a for a in alarms if a["alarm_type"] == "CRITICAL_VIBRATION"]
    check("C4.flip.cleared_by_latest", cv and cv[0]["state"] == "CLEARED", alarms)
    # late history-only out-of-range reading (seq3 ts10:01 < 10:02): must NOT re-activate
    script("a5", normals("a5", [(3, "2026-08-18T10:01:00Z",
            {40001: 250, 40002: 10132, 40003: 250, 40004: 1})]))
    poll()
    alarms = gw("GET", "/alarms").json()
    cv = [a for a in alarms if a["alarm_type"] == "CRITICAL_VIBRATION"]
    check("C4.flip.history_only_no_flip", cv and cv[0]["state"] == "CLEARED", alarms)

    # active_alarms count in GET /machines
    reset_fleet()
    register("a6", "standard-v1")
    script("a6", normals("a6", [(1, "2026-08-18T10:00:00Z",
            {40001: 1210, 40002: 10132, 40003: 200, 40004: 1})]))  # high temp + high vib (non-crit)
    poll()
    m = gw("GET", "/machines").json()
    a6 = [x for x in m if x["id"] == "a6"][0]
    check("C4.active_alarms_count", a6["active_alarms"] == 2, m)


# ============================================================
# Component 5 (20%): API + concurrency-safety + failures
# ============================================================
def component5_api_concurrency():
    print("\n=== Component 5: API + concurrency + failures ===")
    # API contract: endpoints exist & pagination/filters work
    reset_fleet()
    register("p1", "standard-v1")
    register("p2", "high-temp-v1")
    script("p1", normals("p1", [(1, "2026-08-18T10:00:00Z")]))
    poll()
    # GET /machines lists both
    m = gw("GET", "/machines").json()
    ids = sorted(x["id"] for x in m)
    check("C5.api.machines_list", "p1" in ids and "p2" in ids, m)
    # history limit + ordering
    for seq in range(2, 12):
        script("p1", normals("p1", [(seq, f"2026-08-18T10:{seq:02d}:00Z")]))
        poll()
    h = gw("GET", "/machines/p1/history", params={"limit": "5"}).json()
    check("C5.api.limit", len(h["readings"]) == 5, h)
    # from/to inclusive filters
    h = gw("GET", "/machines/p1/history",
           params={"from": "2026-08-18T10:04:00Z", "to": "2026-08-18T10:06:00Z"}).json()
    ts = [r["timestamp"] for r in h["readings"]]
    check("C5.api.from_to_inclusive",
          all("2026-08-18T10:04:00Z" <= t <= "2026-08-18T10:06:00Z" for t in ts), ts)
    # alarms filter by severity
    a = gw("GET", "/alarms", params={"severity": "WARNING"}).json()
    check("C5.api.alarms_severity_filter", all(x["severity"] == "WARNING" for x in a), a)
    a = gw("GET", "/alarms", params={"state": "ACTIVE"}).json()
    check("C5.api.alarms_state_filter", all(x["state"] == "ACTIVE" for x in a), a)

    # failure modes all increment the COMM_LOST counter (mixed cycle)
    reset_fleet()
    register("f1", "standard-v1")
    failures = [{"mode": "5xx"}, {"mode": "notfound"}, {"mode": "malformed"},
                {"mode": "timeout"}, {"mode": "missing"}, {"mode": "outofrange"},
                {"mode": "invalidstatus"}]
    for fmode in failures:
        script("f1", [fmode])
        r = poll()
        check(f"C5.fail.{fmode['mode']}_counted", r["failures"] == 1, r)
    # after 3 consecutive failures -> COMM_LOST active (we've had several)
    alarms = gw("GET", "/alarms").json()
    cl = [a for a in alarms if a["alarm_type"] == "COMMUNICATION_LOST"]
    check("C5.fail.commlost_active", cl and cl[0]["state"] == "ACTIVE", alarms)

    # concurrent overlapping polls: identical reading enqueued many times must
    # store exactly one row, no duplicates, no lost update.
    reset_fleet()
    register("cc1", "standard-v1")
    for _ in range(8):
        enqueue("cc1", {"mode": "normal", "sequence": 77,
                        "timestamp": "2026-08-18T10:00:00Z", "registers": REG()})
    # fire multiple POST /poll concurrently
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(poll) for _ in range(8)]
        results = [f.result() for f in futs]
    hist = gw("GET", "/machines/cc1/history").json()
    check("C5.concurrent.exactly_one_row", len(hist["readings"]) == 1, hist)
    st = gw("GET", "/machines/cc1/status").json()
    check("C5.concurrent.status_set", st["telemetry_timestamp"] == "2026-08-18T10:00:00Z", st)

    # two-machine concurrent poll interleaved with failures (hidden flavor)
    reset_fleet()
    register("m-a", "standard-v1")
    register("m-b", "high-temp-v1")
    enqueue("m-a", {"mode": "normal", "sequence": 1, "timestamp": "2026-08-18T10:00:00Z", "registers": REG()})
    enqueue("m-b", {"mode": "normal", "sequence": 1, "timestamp": "2026-08-18T10:00:00Z",
                    "registers": {40001: 1500, 40002: 50000, 40003: 100, 40004: 3}})
    enqueue("m-a", {"mode": "5xx"})
    enqueue("m-b", {"mode": "normal", "sequence": 2, "timestamp": "2026-08-18T10:01:00Z",
                    "registers": {40001: 1500, 40002: 50000, 40003: 100, 40004: 3}})
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(poll) for _ in range(6)]
        for f in futs:
            f.result()
    ha = gw("GET", "/machines/m-a/history").json()
    hb = gw("GET", "/machines/m-b/history").json()
    check("C5.two_machine.ma_rows", len(ha["readings"]) == 1, ha)
    check("C5.two_machine.mb_rows", len(hb["readings"]) == 2, hb)
    sb = gw("GET", "/machines/m-b/status").json()
    check("C5.two_machine.mb_status", sb["temperature_c"] == 150.0 and sb["status"] == "MAINTENANCE", sb)


# ============================================================
# Hidden combinatorial scenarios (sealed). These use dynamically generated
# machine IDs and both maps; they are folded into the components above via the
# reset_fleet() + register() calls so the scoring stays additive.
# ============================================================
def hidden_matrix():
    print("\n=== Hidden matrix (dynamic IDs + both maps + shuffled order) ===")
    # Dynamically generated machine IDs (not the visible ones).
    rng = random.Random(20260818)
    dyn_ids = [f"dyn-{i:03d}" for i in range(4)]
    maps = ["standard-v1", "high-temp-v1"]
    reset_fleet()
    for i, mid in enumerate(dyn_ids):
        register(mid, maps[i % 2])

    # H1a: shuffled delivery of 6 ordered readings (NO restart) — defeats the
    # arrival-order-as-sequence-order exploit while keeping a deterministic
    # final verdict (latest = reading with the greatest timestamp).
    base_ts = "2026-08-18T11:%02d:00Z"
    events = [(dyn_ids[0], seq, base_ts % (seq - 1)) for seq in range(1, 7)]
    rng.shuffle(events)
    for mid, sq, ts in events:
        enqueue(mid, {"mode": "normal", "sequence": sq, "timestamp": ts, "registers": REG()})
    run_polls(6)
    hist = gw("GET", "/machines/dyn-000/history").json()
    uniq = sorted(set(r["sequence"] for r in hist["readings"]))
    check("H1a.dyn.shuffled_unique", uniq == [1, 2, 3, 4, 5, 6], uniq)
    st = gw("GET", "/machines/dyn-000/status").json()
    check("H1a.dyn.latest_by_ts", st["telemetry_timestamp"] == "2026-08-18T11:05:00Z", st)

    # H1b: legitimate restart followed by a duplicate of the restart reading,
    # delivered in dependency order (restart verdict is inherently order
    # dependent per the spec). prev 950 -> seq 5 restart -> seq 5 dup -> seq 20.
    reset_fleet()
    register("hr1", "standard-v1")
    enqueue("hr1", {"mode": "normal", "sequence": 950, "timestamp": "2026-08-18T12:00:00Z", "registers": REG()})
    enqueue("hr1", {"mode": "normal", "sequence": 5, "timestamp": "2026-08-18T12:01:00Z", "registers": REG()})
    enqueue("hr1", {"mode": "normal", "sequence": 5, "timestamp": "2026-08-18T12:01:00Z", "registers": REG()})
    enqueue("hr1", {"mode": "normal", "sequence": 20, "timestamp": "2026-08-18T12:02:00Z", "registers": REG()})
    run_polls(4)
    hist = gw("GET", "/machines/hr1/history").json()
    uniq = sorted(set(r["sequence"] for r in hist["readings"]))
    check("H1b.dyn.restart_dup_unique", uniq == [5, 20, 950], uniq)
    st = gw("GET", "/machines/hr1/status").json()
    check("H1b.dyn.restart_status", st["telemetry_timestamp"] == "2026-08-18T12:02:00Z", st)

    # high-temp-v1 only-valid values on a dynamic machine
    mid = dyn_ids[1]
    register(mid, "high-temp-v1")
    enqueue(mid, {"mode": "normal", "sequence": 1, "timestamp": "2026-08-18T11:00:00Z",
                  "registers": {40001: 1800, 40002: 200000, 40003: 600, 40004: 3}})  # 180C,2000kPa,60,MAINT
    poll()
    st = gw("GET", "/machines/%s/status" % mid).json()
    check("H2.dyn.hightemp_only_valid", st["temperature_c"] == 180.0 and st["pressure_kpa"] == 2000.0
          and st["vibration_mm_s"] == 60.0 and st["status"] == "MAINTENANCE", st)

    # mixed valid/missing/503/malformed within a single poll cycle (drain once)
    reset_fleet()
    register("mx-a", "standard-v1")
    register("mx-b", "standard-v1")
    enqueue("mx-a", {"mode": "normal", "sequence": 1, "timestamp": "2026-08-18T10:00:00Z", "registers": REG()})
    enqueue("mx-b", {"mode": "missing"})
    enqueue("mx-a", {"mode": "5xx"})
    enqueue("mx-b", {"mode": "malformed"})
    run_polls(2)  # exactly two polls drain each device's single queued response
    ha = gw("GET", "/machines/mx-a/history").json()
    hb = gw("GET", "/machines/mx-b/history").json()
    check("H3.mixed.a_one_valid", len(ha["readings"]) == 1, ha)
    check("H3.mixed.b_zero", len(hb["readings"]) == 0, hb)
    # mx-a had 1 valid then 1 failure -> counter=1 (not comm lost)
    alarms = gw("GET", "/alarms").json()
    cl = [a for a in alarms if a["alarm_type"] == "COMMUNICATION_LOST" and a["machine_id"] == "mx-a"]
    check("H3.mixed.a_not_lost", (not cl) or cl[0]["state"] == "CLEARED", alarms)

    print("hidden matrix done")


def main():
    component1_decode()
    component2_history()
    component3_latest_state()
    component4_alarms()
    component5_api_concurrency()
    hidden_matrix()

    passed = sum(1 for _, ok in CHECKS if ok)
    total = len(CHECKS)
    print(f"\nTOTAL CHECKS: {passed}/{total}")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
