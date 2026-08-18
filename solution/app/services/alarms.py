"""Alarm state machine (reference solution, implements §6 + §7).

There are FIVE alarm types and TWO independent trigger paths that must NOT be
merged:

  Path A (latest-state recalculation) — alarms 1-4. Evaluated ONLY when a
  reading becomes the new latest state (Stage B satisfied). A history-only
  reading must never touch these.

  Path B (per-poll evaluation) — COMMUNICATION_LOST. Evaluated once per POST
  /poll for every machine, regardless of latest-state outcome, based on the
  consecutive-failure counter.

State: one alarm instance per (machine_id, alarm_type). It is either ACTIVE or
CLEARED. Raise to ACTIVE the first time the condition becomes true; clear to
CLEARED only on a subsequent false evaluation on the SAME trigger path. No
debounce/hysteresis in v1.
"""
from __future__ import annotations

import config
from services.decoder import (
    DecodedReading,
    map_max_temperature,
    map_min_pressure,
)


ALARM_HIGH_TEMPERATURE = "HIGH_TEMPERATURE"
ALARM_HIGH_VIBRATION = "HIGH_VIBRATION"
ALARM_CRITICAL_VIBRATION = "CRITICAL_VIBRATION"
ALARM_PRESSURE_FAULT = "PRESSURE_FAULT"
ALARM_COMMUNICATION_LOST = "COMMUNICATION_LOST"

SEVERITY_WARNING = "WARNING"
SEVERITY_CRITICAL = "CRITICAL"

STATE_ACTIVE = "ACTIVE"
STATE_CLEARED = "CLEARED"

# Severity per alarm type (§7 table).
ALARM_SEVERITY = {
    ALARM_HIGH_TEMPERATURE: SEVERITY_WARNING,
    ALARM_HIGH_VIBRATION: SEVERITY_WARNING,
    ALARM_CRITICAL_VIBRATION: SEVERITY_CRITICAL,
    ALARM_PRESSURE_FAULT: SEVERITY_CRITICAL,
    ALARM_COMMUNICATION_LOST: SEVERITY_CRITICAL,
}

# Alarms 1-4 are recalculated only on latest-state updates.
LATEST_STATE_ALARMS = [
    ALARM_HIGH_TEMPERATURE,
    ALARM_HIGH_VIBRATION,
    ALARM_CRITICAL_VIBRATION,
    ALARM_PRESSURE_FAULT,
]


def evaluate_telemetry_alarms(
    decoded: DecodedReading, register_map: str
) -> dict[str, bool]:
    """Return the desired ACTIVE-state (True/False) of alarms 1-4 for a reading
    that has just become the latest state.

    HIGH_TEMPERATURE:    temperature_c > map max
    HIGH_VIBRATION:      vibration_mm_s >= 15.0
    CRITICAL_VIBRATION:  vibration_mm_s >= 25.0
    PRESSURE_FAULT:      pressure_kpa < map min
    """
    temp_max = map_max_temperature(register_map)
    pres_min = map_min_pressure(register_map)
    vib = decoded.vibration_mm_s
    return {
        ALARM_HIGH_TEMPERATURE: decoded.temperature_c > temp_max,
        ALARM_HIGH_VIBRATION: vib >= 15.0,
        ALARM_CRITICAL_VIBRATION: vib >= 25.0,
        ALARM_PRESSURE_FAULT: decoded.pressure_kpa < pres_min,
    }


def communication_lost_active(consecutive_failures: int) -> bool:
    """COMMUNICATION_LOST condition: counter >= threshold (§6)."""
    return consecutive_failures >= config.COMMUNICATION_LOST_THRESHOLD
