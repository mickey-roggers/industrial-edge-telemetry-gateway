"""FastAPI application entrypoint (reference solution).

On startup:
  * open the SQLite repositories
  * start the httpx client pointed at the simulator
  * discover devices from the simulator (GET /devices) and register each known
    machine with its register_map in the machine repository

Endpoints:
  GET  /machines
  GET  /machines/{id}/status
  GET  /machines/{id}/history
  GET  /alarms
  POST /poll
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException

import config
from api.alarms import router as alarms_router
from api.machines import router as machines_router
from deps import get_machine_repo, get_poller
from repositories.machines import MachineRepository
from repositories.telemetry import TelemetryRepository
from services.poller import Poller

# Constants used for startup. These intentionally mirror what a separate
# "gateway" package might name them, so the agent version is drop-in compatible.
from models.schemas import PollResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    repo = TelemetryRepository(db_path=config.DB_PATH)
    machine_repo = MachineRepository(db_path=config.DB_PATH)
    # Allow injecting a pre-built httpx client (e.g. an in-process ASGI-bound
    # client for testing). Otherwise create a real async client.
    client = getattr(app.state, "client", None) or httpx.AsyncClient()
    poller = Poller(repo, machine_repo, client)
    app.state.repo = repo
    app.state.machine_repo = machine_repo
    app.state.poller = poller
    app.state.client = client

    # Discover the simulated fleet and register known machines.
    await _discover_devices(app)

    try:
        yield
    finally:
        if not getattr(app.state, "_client_injected", False):
            await client.aclose()
        repo.close()
        machine_repo.close()


async def _discover_devices(app: FastAPI):
    client = app.state.client
    machine_repo = app.state.machine_repo
    try:
        resp = await client.get(
            f"{config.SIMULATOR_BASE_URL}/devices", timeout=config.POLL_TIMEOUT_SECONDS
        )
    except httpx.HTTPError:
        return
    if resp.status_code != 200:
        return
    try:
        devices = resp.json()
    except (json.JSONDecodeError, ValueError):
        return
    for dev in devices:
        mid = dev.get("id")
        rmap = dev.get("register_map")
        if mid and rmap:
            machine_repo.add_machine(mid, rmap)


app = FastAPI(title="Industrial Edge Telemetry Gateway", lifespan=lifespan)
app.include_router(machines_router)
app.include_router(alarms_router)


@app.post("/poll", response_model=PollResponse)
async def post_poll(poller: Poller = Depends(get_poller)):
    result = await poller.poll_all()
    return PollResponse(
        polled=result["polled"],
        stored=result["stored"],
        history_only=result["history_only"],
        failures=result["failures"],
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/admin/reset")
async def admin_reset():
    """Test/admin convenience: wipe all gateway state and re-discover the
    simulator fleet. Used by the verifier to isolate scenarios. Not part of the
    §9 contract (the five required endpoints remain unchanged)."""
    app.state.repo.clear_all()
    app.state.machine_repo.clear()
    await _discover_devices(app)
    return {"ok": True}
