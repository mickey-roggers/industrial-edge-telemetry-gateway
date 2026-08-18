"""Two-stage telemetry processing: historical acceptance (Stage A, §4) and
latest-state update (Stage B, §5) (reference solution).

The single most important design principle of this whole task lives here: a
reading being *stored in history* and a reading becoming the machine's *current
live state* are two separate decisions.

Stage A  -> does the reading get written to history?
Stage B  -> does the reading become the new latest state (and thus feed alarms)?

History is written unconditionally on a new, valid, uniquely-sequenced reading.
Latest state changes only when the Stage B predicate holds.
"""
from __future__ import annotations

import config
from models.schemas import HistoryReading
from services.decoder import DecodedReading

# Outcome categories for a processed reading.
STORED_LATEST = "stored_latest"
STORED_HISTORY_ONLY = "stored_history_only"
DUPLICATE = "duplicate"
REJECTED = "rejected"


def should_become_latest_state(
    decoded: DecodedReading,
    current_sequence: int | None,
    current_timestamp: str | None,
) -> bool:
    """A5 latest-state predicate. True if the reading becomes the new latest state.

    TODO(agent): implement the exact two-branch predicate:
      * Normal progression: sequence > current_sequence AND timestamp > current_timestamp
      * Restart: sequence < current_sequence AND timestamp > current_timestamp
                 AND current_sequence >= 900 AND sequence <= 100
    Both branches require a STRICTLY greater timestamp. A first reading (no
    current state) always becomes latest state.
    """
    raise NotImplementedError("should_become_latest_state is not implemented")

def build_history_reading(device_id: str, decoded: DecodedReading) -> HistoryReading:
    return HistoryReading(
        device_id=device_id,
        sequence=decoded.sequence,
        timestamp=decoded.timestamp,
        temperature_c=decoded.temperature_c,
        pressure_kpa=decoded.pressure_kpa,
        vibration_mm_s=decoded.vibration_mm_s,
        status=decoded.status,
        status_code=decoded.status_code,
    )
