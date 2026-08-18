"""Alarms API endpoint (reference solution).

GET /alarms?machine_id=&state=&severity=  -> filterable alarm list (§9)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from deps import get_repo
from models.schemas import AlarmView
from repositories.telemetry import TelemetryRepository

router = APIRouter()


@router.get("/alarms", response_model=list[AlarmView])
def list_alarms(
    machine_id: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    repo: TelemetryRepository = Depends(get_repo),
):
    rows = repo.get_alarms(machine_id=machine_id, state=state, severity=severity)
    return [
        AlarmView(
            machine_id=r["machine_id"],
            alarm_type=r["alarm_type"],
            state=r["state"],
            severity=r["severity"],
        )
        for r in rows
    ]
