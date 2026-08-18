"""Small, mechanism-level tests shipped with the task for local development.

These tests intentionally cover individual rules only. The sealed verifier in
../verifier_runner.py owns integration and combinatorial grading.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "environment" / "app"
sys.path.insert(0, str(APP))

from services.decoder import decode_registers  # noqa: E402
from services.telemetry import should_be_latest_state  # noqa: E402


def test_standard_decode():
    reading, rejection = decode_registers(
        {
            "sequence": 1,
            "timestamp": "2026-08-18T10:00:00Z",
            "registers": {40001: 253, 40002: 10132, 40003: 42, 40004: 1},
        },
        "standard-v1",
    )
    assert rejection is None
    assert reading.temperature_c == 25.3
    assert reading.pressure_kpa == 101.32
    assert reading.vibration_mm_s == 4.2
    assert reading.status == "RUNNING"


def test_physical_envelope_rejects_corrupt_value():
    reading, rejection = decode_registers(
        {
            "sequence": 1,
            "timestamp": "2026-08-18T10:00:00Z",
            "registers": {40001: 99999, 40002: 10132, 40003: 42, 40004: 1},
        },
        "standard-v1",
    )
    assert reading is None
    assert rejection is not None
    assert rejection.reason == "out_of_range"


def test_history_only_ordering_rule():
    assert should_be_latest_state(102, "2026-08-18T10:02:00Z", 100, "2026-08-18T10:00:00Z")
    assert not should_be_latest_state(101, "2026-08-18T10:01:00Z", 102, "2026-08-18T10:02:00Z")


def test_restart_boundary():
    assert should_be_latest_state(0, "2026-08-18T10:06:00Z", 900, "2026-08-18T10:05:00Z")
    assert not should_be_latest_state(101, "2026-08-18T10:06:00Z", 900, "2026-08-18T10:05:00Z")
