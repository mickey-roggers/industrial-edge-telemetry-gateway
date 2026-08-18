"""Register decoding and validation (reference solution).

Implements §3 exactly. The register map that applies is a property of the
device (its ``register_map`` field), never hard-coded. Two maps are supported:
``standard-v1`` and ``high-temp-v1``.

A reading is ``(raw_registers, register_map) -> DecodedReading | Rejection``.
On any missing required register, out-of-range decoded value, or invalid status
code the *entire* reading is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

REQUIRED_REGISTERS = (40001, 40002, 40003, 40004)

# Map-name -> (min, max) for each measured register.
TEMP_RANGES = {
    "standard-v1": (-40.0, 120.0),
    "high-temp-v1": (-40.0, 180.0),
}
PRESSURE_RANGES = {
    "standard-v1": (0.0, 1500.0),
    "high-temp-v1": (0.0, 2000.0),
}
VIBRATION_RANGES = {
    "standard-v1": (0.0, 50.0),
    "high-temp-v1": (0.0, 60.0),
}

# Valid status codes per map.
VALID_STATUS_CODES = {
    "standard-v1": {0, 1, 2},        # code 3 is invalid for standard-v1
    "high-temp-v1": {0, 1, 2, 3},
}

# PHYSICAL ACCEPTANCE ENVELOPE (resolves the §3 / §7 contradiction).
#
# Register scaling: temperature & vibration are in units of 0.1 (raw 253 -> 25.3 C),
# pressure in units of 0.01 (raw 10132 -> 101.32 kPa). The simulator's
# "outofrange" mode uses raw 99999 (-> 9999.9 C) to mean "out of range".
#
# §3.11 says any decoded value outside "its map's range" is rejected at Stage A.
# But §7 defines HIGH_TEMPERATURE as `temperature_c > map_max` and PRESSURE_FAULT
# as `pressure_kpa < map_min` -- i.e. an *out-of-range* value is precisely what
# raises those alarms. Taken literally, both alarms could never fire.
#
# Resolution: the map's stated range is the NORMAL OPERATING BAND used by the
# alarm thresholds. A reading is only REJECTED at Stage A when a decoded value
# falls outside a wider PHYSICAL envelope (a value too extreme to be a real
# sensor reading, e.g. 9999.9 C). A reading that merely exceeds an operational
# limit is still valid data: it is stored in history, becomes the latest state,
# and the corresponding alarm fires. The simulator's "outofrange" mode (9999.9 C)
# stays rejected; the alarm-triggering values in Component 4 stay accepted.
PHYSICAL_ENVELOPE = {
    "temperature_c": (-273.15, 1000.0),   # 9999.9 C rejected; 121/150/180 accepted (alarm)
    "pressure_kpa": (-100.0, 50000.0),    # slight vacuum allowed; huge rejected
    "vibration_mm_s": (-100.0, 1000.0),
}

# Code -> human label (identical meaning across maps; only validity differs).
STATUS_LABELS = {
    0: "STOPPED",
    1: "RUNNING",
    2: "FAULT",
    3: "MAINTENANCE",
}


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
    reason: str  # one of: missing_register, out_of_range, invalid_status


def _is_in_range(value: float, lo: float, hi: float) -> bool:
    # Inclusive range check per §3 ("−40.0 … 120.0", etc.).
    return lo <= value <= hi


def decode_registers(
    raw: dict, register_map: str
) -> tuple[Optional[DecodedReading], Optional[DecodeRejection]]:
    """Decode a raw device payload into engineering values, or reject it.

    ``raw`` must contain at least: ``sequence`` (int), ``timestamp`` (ISO str),
    and ``registers`` (dict mapping register-number -> int), plus the map itself
    comes from the device's ``register_map`` field (passed in here).
    """
    if register_map not in TEMP_RANGES:
        return None, DecodeRejection(reason="invalid_status")

    registers = raw.get("registers")
    if not isinstance(registers, dict):
        return None, DecodeRejection(reason="missing_register")

    # JSON serializes dict keys as strings, so normalize register numbers to int.
    norm = {}
    for k, v in registers.items():
        try:
            key = int(k)
        except (TypeError, ValueError):
            continue
        norm[key] = v

    # §3.10 — missing any required register -> entire reading rejected.
    for reg in REQUIRED_REGISTERS:
        if reg not in norm:
            return None, DecodeRejection(reason="missing_register")

    raw_temp = int(norm[40001])
    raw_pressure = int(norm[40002])
    raw_vibration = int(norm[40003])
    status_code = int(norm[40004])

    # Register scaling (per spec): temperature and vibration are encoded in units
    # of 0.1 (raw 253 -> 25.3 C), pressure in units of 0.01 (raw 10132 -> 101.32 kPa).
    temperature_c = raw_temp / 10.0
    pressure_kpa = raw_pressure / 100.0
    vibration_mm_s = raw_vibration / 10.0

    temp_lo, temp_hi = TEMP_RANGES[register_map]
    pres_lo, pres_hi = PRESSURE_RANGES[register_map]
    vib_lo, vib_hi = VIBRATION_RANGES[register_map]

    # §3.11 — a value outside the PHYSICAL envelope (not just the map's normal
    # band) is rejected at Stage A. Values that merely exceed an operational
    # limit are still stored and can drive alarms (see PHYSICAL_ENVELOPE note).
    if not _is_in_range(temperature_c, *PHYSICAL_ENVELOPE["temperature_c"]):
        return None, DecodeRejection(reason="out_of_range")
    if not _is_in_range(pressure_kpa, *PHYSICAL_ENVELOPE["pressure_kpa"]):
        return None, DecodeRejection(reason="out_of_range")
    if not _is_in_range(vibration_mm_s, *PHYSICAL_ENVELOPE["vibration_mm_s"]):
        return None, DecodeRejection(reason="out_of_range")

    # §3.12 — unrecognized status code for that map -> entire reading rejected.
    if status_code not in VALID_STATUS_CODES[register_map]:
        return None, DecodeRejection(reason="invalid_status")

    return (
        DecodedReading(
            sequence=int(raw["sequence"]),
            timestamp=str(raw["timestamp"]),
            temperature_c=temperature_c,
            pressure_kpa=pressure_kpa,
            vibration_mm_s=vibration_mm_s,
            status_code=status_code,
            status=STATUS_LABELS[status_code],
            register_map=register_map,
        ),
        None,
    )


def map_max_temperature(register_map: str) -> float:
    return TEMP_RANGES[register_map][1]


def map_min_pressure(register_map: str) -> float:
    return PRESSURE_RANGES[register_map][0]
