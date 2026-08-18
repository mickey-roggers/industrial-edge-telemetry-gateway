"""Machines API endpoints (reference solution).

GET /machines               -> summary per known machine (§9)
GET /machines/{id}/status   -> latest state only (§9)
GET /machines/{id}/history?from=&to=&limit=  -> Stage-A history (§9)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import get_machine_repo, get_repo
from models.schemas import HistoryResponse, HistoryReading, MachineStatus, MachineSummary
from repositories.machines import MachineRepository
from repositories.telemetry import TelemetryRepository

router = APIRouter()


def _to_machine_summary(row, repo: TelemetryRepository) -> MachineSummary:
    mid = row["id"]
    latest = repo.get_latest_state(mid)
    active = repo.count_active_alarms(mid)
    last_seen = latest["timestamp"] if latest else None
    status = latest["status"] if latest else None
    return MachineSummary(
        id=mid, status=status, last_seen=last_seen, active_alarms=active
    )


@router.get("/machines", response_model=list[MachineSummary])
def list_machines(
    repo: TelemetryRepository = Depends(get_repo),
    machine_repo: MachineRepository = Depends(get_machine_repo),
):
    out = []
    for row in machine_repo.all_machines():
        out.append(_to_machine_summary(row, repo))
    return out


@router.get("/machines/{machine_id}/status", response_model=MachineStatus)
def machine_status(
    machine_id: str,
    repo: TelemetryRepository = Depends(get_repo),
    machine_repo: MachineRepository = Depends(get_machine_repo),
):
    if machine_repo.get_machine(machine_id) is None:
        raise HTTPException(status_code=404, detail="machine not found")
    latest = repo.get_latest_state(machine_id)
    if latest is None:
        return MachineStatus(
            machine_id=machine_id,
            status=None,
            temperature_c=None,
            pressure_kpa=None,
            vibration_mm_s=None,
            telemetry_timestamp=None,
        )
    return MachineStatus(
        machine_id=machine_id,
        status=latest["status"],
        temperature_c=latest["temperature_c"],
        pressure_kpa=latest["pressure_kpa"],
        vibration_mm_s=latest["vibration_mm_s"],
        telemetry_timestamp=latest["timestamp"],
    )


@router.get("/machines/{machine_id}/history", response_model=HistoryResponse)
def machine_history(
    machine_id: str,
    from_ts: Optional[str] = Query(default=None, alias="from"),
    to_ts: Optional[str] = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=1000),
    repo: TelemetryRepository = Depends(get_repo),
    machine_repo: MachineRepository = Depends(get_machine_repo),
):
    if machine_repo.get_machine(machine_id) is None:
        raise HTTPException(status_code=404, detail="machine not found")
    rows = repo.get_history(machine_id, from_ts, to_ts, limit)
    readings = [
        HistoryReading(
            device_id=r["device_id"],
            sequence=r["sequence"],
            timestamp=r["timestamp"],
            temperature_c=r["temperature_c"],
            pressure_kpa=r["pressure_kpa"],
            vibration_mm_s=r["vibration_mm_s"],
            status=r["status"],
            status_code=r["status_code"],
        )
        for r in rows
    ]
    return HistoryResponse(machine_id=machine_id, readings=readings)
