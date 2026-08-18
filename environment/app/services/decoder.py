"""Register decoding and validation (reference solution).

Implements §3 exactly. The register map that applies is a property of the
device (its ``register_map`` field), never hard-coded. Two maps are supported:
``standard-v1`` and ``high-temp-v1``.

A reading is ``(raw_registers, register_map) -> DecodedReading | Rejection``.
On any missing required register, physically impossible decoded value, or
invalid status code the *entire* reading is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

REQUIRED_REGISTERS = (40001, 40002, 40003, 40004)

# Map-name -> normal operating band for each measured register.
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

# PHYSICAL ACCEPTANCE ENVELOPE.
#
# Register scaling: temperature & vibration are in units of 0.1 (raw 253 -> 25.3 C),
# pressure in units of 0.01 (raw 10132 -> 101.32 kPa). The simulator's
# "outofrange" mode uses raw 99999 (-> 9999.9 C) to mean "out of range".
#
# The map ranges are normal operating bands used by alarm thresholds. Stage A
# rejects only values outside this wider physical envelope: values too extreme
# to be plausible sensor data, e.g. simulator mode "outofrange" at 9999.9 C.
# A reading that merely exceeds an operational limit is still valid data: it is
# stored in history, may become latest state, and can fire an alarm.
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

    TODO(agent): implement A3. Apply the device's register_map scaling
    (temperature & vibration = raw / 10.0; pressure = raw / 100.0; status is a
    direct enum). Reject the ENTIRE reading when any required register is
    missing, any decoded numeric value is outside the physical validity
    envelope, or the status code is invalid for that map. Return
    (DecodedReading, None) on success, else
    (None, DecodeRejection).
    """
    raise NotImplementedError("decode_registers is not implemented")

def map_max_temperature(register_map: str) -> float:
    return TEMP_RANGES[register_map][1]


def map_min_pressure(register_map: str) -> float:
    return PRESSURE_RANGES[register_map][0]
