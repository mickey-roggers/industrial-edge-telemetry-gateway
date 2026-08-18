"""Pydantic schemas shared across the gateway (reference solution)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class MachineSummary(BaseModel):
    id: str
    status: str | None
    last_seen: str | None
    active_alarms: int


class MachineStatus(BaseModel):
    machine_id: str
    status: str | None
    temperature_c: float | None
    pressure_kpa: float | None
    vibration_mm_s: float | None
    telemetry_timestamp: str | None


class HistoryReading(BaseModel):
    device_id: str
    sequence: int
    timestamp: str
    temperature_c: float
    pressure_kpa: float
    vibration_mm_s: float
    status: str
    status_code: int


class HistoryResponse(BaseModel):
    machine_id: str
    readings: list[HistoryReading]


class AlarmView(BaseModel):
    machine_id: str
    alarm_type: str
    state: str
    severity: str


class PollResponse(BaseModel):
    polled: int
    stored: int
    history_only: int
    failures: int


class DeviceInfo(BaseModel):
    """Mirrors the shape returned by the simulator's GET /devices."""

    id: str
    register_map: str
    registers: list[int]
