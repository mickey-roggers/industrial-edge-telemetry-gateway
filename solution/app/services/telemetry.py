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
    """§5 latest-state predicate. Returns True if the reading should become the
    new latest state.

    ``current_sequence`` / ``current_timestamp`` are the device's *current* latest
    state values. They are None when the device has no latest state yet (first
    reading). A first reading always becomes latest state (no current state to
    compare against).
    """
    if current_sequence is None or current_timestamp is None:
        return True

    seq = decoded.sequence
    ts = decoded.timestamp

    # Both branches require the incoming timestamp to be STRICTLY greater than
    # the current latest timestamp. A tied timestamp fails both predicates.
    if not (ts > current_timestamp):
        return False

    # Normal progression: strictly greater sequence AND strictly greater ts.
    normal = seq > current_sequence and ts > current_timestamp

    # Restart: sequence reset (went backwards) AND ts strictly greater, with the
    # high-water-mark guard that the *previous* latest sequence was high (>=900)
    # and the new sequence is low (<=100).
    restart = (
        seq < current_sequence
        and ts > current_timestamp
        and current_sequence >= config.RESTART_PREV_SEQ_MIN
        and seq <= config.RESTART_NEW_SEQ_MAX
    )

    return normal or restart


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
