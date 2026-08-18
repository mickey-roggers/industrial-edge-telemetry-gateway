"""Concurrent poller (reference solution). Implements the exact §8 control flow.

For each machine, on each POST /poll:
  1. Call the simulated device API for this machine.
  2. If response not syntactically valid (timeout/5xx/404/malformed JSON):
        increment failure counter. STOP.
  3. If any required register missing OR any value out of range OR invalid
     status: reject reading (no history). increment failure counter. STOP.
  4. If (device, sequence) already stored: duplicate -> reset counter to 0. STOP.
  5. Else new valid reading: store history (Stage A, unconditional). reset
     counter to 0. evaluate Stage B (§5).
       - satisfied: update latest_state, recalc alarms 1-4.
       - not: history-only.
  6. After ALL machines attempted in this POST /poll: recalc COMMUNICATION_LOST
     per machine from its current counter (Path B).
  7. Return response.

Concurrency (§7): all machines are polled concurrently via asyncio.gather.
Each machine's outcome is recorded into a shared result list keyed by machine.
Final COMMUNICATION_LOST evaluation walks every known machine once after all
gather tasks complete.

The failure counter is updated per-machine inside that machine's task. Because
SQLite writes are serialized by an asyncio.Lock in the repository and we also
hold a per-machine lock, overlapping polls of the same machine cannot corrupt a
row. The counter is read at the start of each machine task and re-read/written
atomically within the machine lock so two concurrent polls of the same machine
both contribute correctly.

Note on the counter under overlapping polls of the SAME machine: we read the
current count at the start, compute the new value, and write it inside the
machine lock. If two polls of machine X run concurrently, each reads the same
starting count, processes its own outcome, and the final committed count is the
result of sequential application inside the lock — no lost update.
"""
from __future__ import annotations

import asyncio
import json

import httpx

import config
from repositories.telemetry import TelemetryRepository
from services.alarms import (
    ALARM_COMMUNICATION_LOST,
    SEVERITY_CRITICAL,
    STATE_ACTIVE,
    STATE_CLEARED,
    communication_lost_active,
    evaluate_telemetry_alarms,
)
from services.decoder import decode_registers
from services.telemetry import (
    STORED_HISTORY_ONLY,
    STORED_LATEST,
    build_history_reading,
    should_become_latest_state,
)


class Poller:
    def __init__(
        self,
        repo: TelemetryRepository,
        machine_repo,
        client: httpx.AsyncClient,
    ):
        self.repo = repo
        self.machine_repo = machine_repo
        self.client = client

    async def poll_machine(self, machine_id: str, register_map: str) -> dict:
        """Run the §8 control flow for a single machine. Returns an outcome dict."""
        lock = self.repo._machine_lock(machine_id)
        async with lock:
            try:
                resp = await self.client.get(
                    f"{config.SIMULATOR_BASE_URL}/devices/{machine_id}/registers",
                    timeout=config.POLL_TIMEOUT_SECONDS,
                )
            except httpx.TimeoutException:
                self._increment_failure(machine_id)
                return self._outcome("failure", machine_id, "timeout")
            except httpx.HTTPError:
                self._increment_failure(machine_id)
                return self._outcome("failure", machine_id, "http_error")

            if resp.status_code >= 500:
                self._increment_failure(machine_id)
                return self._outcome("failure", machine_id, "5xx")
            if resp.status_code == 404:
                self._increment_failure(machine_id)
                return self._outcome("failure", machine_id, "404")

            # Syntactically valid response required. Malformed JSON -> failure.
            try:
                payload = resp.json()
            except (json.JSONDecodeError, ValueError):
                self._increment_failure(machine_id)
                return self._outcome("failure", machine_id, "malformed_json")

            # §8.3 — missing register / out-of-range / invalid status -> reject.
            decoded, rejection = decode_registers(payload, register_map)
            if rejection is not None:
                self._increment_failure(machine_id)
                return self._outcome(
                    "rejected", machine_id, rejection.reason, register_map
                )

            device_id = machine_id
            # §8.4 — duplicate?
            if self.repo.history_exists(device_id, decoded.sequence):
                self._reset_failure(machine_id)
                return self._outcome(
                    "duplicate", machine_id, register_map, decoded=decoded
                )

            # §8.5 — new valid reading: store in history unconditionally.
            reading = build_history_reading(device_id, decoded)
            inserted = self.repo.insert_history(reading)
            if not inserted:
                # Race: another concurrent poll already stored this sequence.
                self._reset_failure(machine_id)
                return self._outcome(
                    "duplicate", machine_id, register_map, decoded=decoded
                )

            self._reset_failure(machine_id)

            # Stage B — latest-state predicate.
            latest = self.repo.get_latest_state(device_id)
            cur_seq = None if latest is None else int(latest["sequence"])
            cur_ts = None if latest is None else str(latest["timestamp"])
            if should_become_latest_state(decoded, cur_seq, cur_ts):
                self.repo.upsert_latest_state(reading)
                # Recalc alarms 1-4 (Path A) on latest-state only.
                desired = evaluate_telemetry_alarms(decoded, register_map)
                for alarm_type, active in desired.items():
                    self._apply_alarm(
                        machine_id, alarm_type, active,
                        severity=self._severity(alarm_type),
                    )
                return self._outcome(
                    "stored_latest", machine_id, register_map, decoded=decoded
                )
            else:
                # History-only: no latest-state write, no alarm recalc.
                return self._outcome(
                    "stored_history_only", machine_id, register_map, decoded=decoded
                )

    # -- failure counter helpers ------------------------------------------
    def _increment_failure(self, device_id: str):
        c = self.repo.get_failure_count(device_id)
        self.repo.set_failure_count(device_id, c + 1)

    def _reset_failure(self, device_id: str):
        self.repo.set_failure_count(device_id, 0)

    # -- alarm helpers -----------------------------------------------------
    def _severity(self, alarm_type: str) -> str:
        from services import alarms as alarms_mod

        return alarms_mod.ALARM_SEVERITY[alarm_type]

    def _apply_alarm(self, machine_id, alarm_type, desired_active, severity):
        existing = self.repo.get_alarm(machine_id, alarm_type)
        current_state = existing["state"] if existing else STATE_CLEARED
        if desired_active and current_state != STATE_ACTIVE:
            new_state = STATE_ACTIVE
        elif not desired_active and current_state != STATE_CLEARED:
            new_state = STATE_CLEARED
        else:
            new_state = current_state  # unchanged
        self.repo.upsert_alarm(machine_id, alarm_type, new_state, severity)

    def _outcome(self, kind, machine_id, detail=None, register_map=None, decoded=None):
        return {
            "kind": kind,
            "machine_id": machine_id,
            "detail": detail,
            "register_map": register_map,
            "sequence": decoded.sequence if decoded else None,
            "timestamp": decoded.timestamp if decoded else None,
        }

    # -- public ------------------------------------------------------------
    async def refresh_machines(self):
        """Re-read the simulator's device fleet and register any new machines.

        The simulator's /devices reflects the *current* fleet, which the verifier
        mutates at runtime (registering devices after startup). POST /poll must
        re-sync the known-machine set each cycle so dynamically added machines
        are polled. Machines already known keep their register_map.
        """
        try:
            resp = await self.client.get(
                f"{config.SIMULATOR_BASE_URL}/devices",
                timeout=config.POLL_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            return
        if resp.status_code != 200:
            return
        try:
            devices = resp.json()
        except (json.JSONDecodeError, ValueError):
            return
        for dev in devices:
            mid = dev.get("id")
            rmap = dev.get("register_map")
            if mid and rmap:
                self.machine_repo.add_machine(mid, rmap)

    async def poll_all(self) -> dict:
        """§8.6 — evaluate COMMUNICATION_LOST only after ALL machines attempted."""
        # Re-sync the known fleet from the simulator (devices may have been
        # registered after startup by the verifier).
        await self.refresh_machines()
        machines = self.machine_repo.all_machines()
        results = []
        if machines:
            tasks = [
                self.poll_machine(row["id"], row["register_map"]) for row in machines
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            norm = []
            for r in results:
                if isinstance(r, Exception):
                    norm.append({"kind": "error", "error": str(r)})
                else:
                    norm.append(r)
            results = norm

        # After all machines attempted: evaluate COMMUNICATION_LOST (Path B).
        comm_lost_changes = []
        for row in machines:
            mid = row["id"]
            count = self.repo.get_failure_count(mid)
            desired_active = communication_lost_active(count)
            existing = self.repo.get_alarm(mid, ALARM_COMMUNICATION_LOST)
            current_state = existing["state"] if existing else STATE_CLEARED
            if desired_active and current_state != STATE_ACTIVE:
                new_state = STATE_ACTIVE
            elif not desired_active and current_state != STATE_CLEARED:
                new_state = STATE_CLEARED
            else:
                new_state = current_state
            if new_state != current_state:
                self.repo.upsert_alarm(
                    mid, ALARM_COMMUNICATION_LOST, new_state, SEVERITY_CRITICAL
                )
                comm_lost_changes.append(mid)

        stored = sum(1 for r in results if r.get("kind") == "stored_latest")
        history_only = sum(1 for r in results if r.get("kind") == "stored_history_only")
        failures = sum(
            1 for r in results if r.get("kind") in ("failure", "rejected")
        )
        return {
            "polled": len(machines),
            "stored": stored,
            "history_only": history_only,
            "failures": failures,
            "details": results,
            "communication_lost_updated": comm_lost_changes,
        }
