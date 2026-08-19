"""Register decoding and validation (reference solution)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

REQUIRED_REGISTERS = (40001, 40002, 40003, 40004)
TEMP_RANGES = {"standard-v1": (-40.0, 120.0), "high-temp-v1": (-40.0, 180.0)}
PRESSURE_RANGES = {"standard-v1": (0.0, 1500.0), "high-temp-v1": (0.0, 2000.0)}
VIBRATION_RANGES = {"standard-v1": (0.0, 50.0), "high-temp-v1": (0.0, 60.0)}
VALID_STATUS_CODES = {"standard-v1": {0, 1, 2}, "high-temp-v1": {0, 1, 2, 3}}

# Stage-A acceptance envelope. Operating map limits are alarm thresholds, not
# automatic telemetry rejection limits; readings beyond an operating limit can
# still be valid and must be allowed to drive the relevant alarm.
PHYSICAL_ENVELOPE = {
    "temperature_c": (-273.15, 1000.0),
    "pressure_kpa": (-100.0, 50000.0),
    "vibration_mm_s": (-100.0, 1000.0),
}
STATUS_LABELS = {0: "STOPPED", 1: "RUNNING", 2: "FAULT", 3: "MAINTENANCE"}


@dataclass
class DecodedReading:
    sequence: int
    timestamp: str
    temperature_c: float
    pressure_kpa: float
    vibration_mm_s: float
    status_code: int
    status: str
    register_map: str


@dataclass
class DecodeRejection:
    reason: str


def _is_in_range(value: float, lo: float, hi: float) -> bool:
    return lo <= value <= hi


def decode_registers(raw: dict, register_map: str) -> tuple[Optional[DecodedReading], Optional[DecodeRejection]]:
    """Decode one simulator payload or return a deterministic rejection.

    Invalid register keys/values are treated as an invalid reading rather than
    being allowed to escape as ValueError/TypeError and become an unclassified
    poll-task exception. This keeps the failure path deterministic.
    """
    if register_map not in TEMP_RANGES:
        return None, DecodeRejection(reason="invalid_status")
    if not isinstance(raw, dict):
        return None, DecodeRejection(reason="missing_register")

    registers = raw.get("registers")
    if not isinstance(registers, dict):
        return None, DecodeRejection(reason="missing_register")

    norm = {}
    for k, v in registers.items():
        try:
            norm[int(k)] = v
        except (TypeError, ValueError):
            continue
    if any(reg not in norm for reg in REQUIRED_REGISTERS):
        return None, DecodeRejection(reason="missing_register")

    try:
        sequence = int(raw["sequence"])
        timestamp = str(raw["timestamp"])
        raw_temp = int(norm[40001])
        raw_pressure = int(norm[40002])
        raw_vibration = int(norm[40003])
        status_code = int(norm[40004])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, DecodeRejection(reason="out_of_range")

    temperature_c = raw_temp / 10.0
    pressure_kpa = raw_pressure / 100.0
    vibration_mm_s = raw_vibration / 10.0

    if not _is_in_range(temperature_c, *PHYSICAL_ENVELOPE["temperature_c"]):
        return None, DecodeRejection(reason="out_of_range")
    if not _is_in_range(pressure_kpa, *PHYSICAL_ENVELOPE["pressure_kpa"]):
        return None, DecodeRejection(reason="out_of_range")
    if not _is_in_range(vibration_mm_s, *PHYSICAL_ENVELOPE["vibration_mm_s"]):
        return None, DecodeRejection(reason="out_of_range")
    if status_code not in VALID_STATUS_CODES[register_map]:
        return None, DecodeRejection(reason="invalid_status")

    return DecodedReading(sequence, timestamp, temperature_c, pressure_kpa,
                          vibration_mm_s, status_code, STATUS_LABELS[status_code],
                          register_map), None


def map_max_temperature(register_map: str) -> float:
    return TEMP_RANGES[register_map][1]


def map_min_pressure(register_map: str) -> float:
    return PRESSURE_RANGES[register_map][0]
