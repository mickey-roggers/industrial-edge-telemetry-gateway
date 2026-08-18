"""Shared dependencies (reference solution).

The gateway's singletons (repositories, poller, httpx client) are created once
at startup in ``main.py`` and stored on ``app.state``. Endpoints receive them via
``Depends`` from here.
"""
from __future__ import annotations

from fastapi import Request

from repositories.machines import MachineRepository
from repositories.telemetry import TelemetryRepository
from services.poller import Poller


def get_repo(request: Request) -> TelemetryRepository:
    return request.app.state.repo


def get_machine_repo(request: Request) -> MachineRepository:
    return request.app.state.machine_repo


def get_poller(request: Request) -> Poller:
    return request.app.state.poller
