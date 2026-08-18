"""Simulated industrial machine fleet (baked into the image, no runtime network).

This service stands in for a plant full of PLC-backed machines. It exposes:

  GET  /devices                      -> list of known device ids + register_map
  GET  /devices/{id}/registers       -> the device's current/next reading
  GET  /health

  POST /control/device               -> register/update a device
  POST /control/device/{id}/enqueue  -> append one scripted response to the queue
  POST /control/device/{id}/script   -> replace the queue with a list of responses
  POST /control/reset                -> wipe all devices and queues
  GET  /control/device/{id}/queue    -> inspect remaining queued responses

Each GET /devices/{id}/registers consumes the next scripted response for that
device (FIFO). If the queue is empty it returns the device's last delivered
reading (or a default). Scripted response "mode" controls what the call returns:

  normal        -> 200 JSON with sequence/timestamp/registers
  timeout       -> sleeps past the gateway's poll timeout (httpx timeout)
  5xx           -> HTTP 503
  notfound      -> HTTP 404
  malformed     -> 200 with non-JSON body
  missing       -> 200 JSON missing one required register
  outofrange    -> 200 JSON with a value outside the map range
  invalidstatus -> 200 JSON with an invalid status code for the map

The verifier uses the control API to script exact interleaved, out-of-order,
restart, duplicate, and failure sequences per machine.
"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

SIM_PORT = int(os.environ.get("SIMULATOR_PORT", "8777"))
GATEWAY_TIMEOUT = float(os.environ.get("POLL_TIMEOUT_SECONDS", "2.0"))

app = FastAPI(title="Machine Simulator")

# device_id -> {"register_map": str, "current": dict | None}
DEVICES: dict[str, dict] = {}
# device_id -> list of queued response specs (FIFO)
QUEUES: dict[str, list[dict]] = {}
_STATE_LOCK = asyncio.Lock()


class DeviceSpec(BaseModel):
    id: str
    register_map: str
    # optional initial reading payload
    initial: dict | None = None


class EnqueueSpec(BaseModel):
    mode: str = "normal"          # see mode list above
    sequence: int | None = None
    timestamp: str | None = None
    registers: dict | None = None


class ScriptSpec(BaseModel):
    responses: list[EnqueueSpec]


def _default_reading(register_map: str) -> dict:
    return {
        "sequence": 0,
        "timestamp": "2026-08-18T00:00:00Z",
        "registers": {40001: 250, 40002: 10132, 40003: 42, 40004: 1},
    }


def _build_normal(spec: dict, register_map: str) -> dict:
    """Construct a 'normal' reading payload from a spec. The verifier supplies
    explicit sequence/timestamp/registers for every scripted normal response;
    missing fields fall back to a fixed default."""
    regs = spec.get("registers") or _default_reading(register_map)["registers"]
    return {
        "sequence": spec.get("sequence", 0),
        "timestamp": spec.get("timestamp", "2026-08-18T00:00:00Z"),
        "registers": dict(regs),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=SIM_PORT, log_level="warning")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/devices")
async def list_devices():
    return [
        {"id": did, "register_map": d["register_map"]} for did, d in DEVICES.items()
    ]


@app.get("/devices/{device_id}/registers")
async def get_registers(device_id: str):
    if device_id not in DEVICES:
        raise HTTPException(status_code=404, detail="device not found")

    async with _STATE_LOCK:
        queue = QUEUES.setdefault(device_id, [])
        spec = queue.pop(0) if queue else None
        register_map = DEVICES[device_id]["register_map"]

    if spec is None:
        # No scripted response: re-deliver the last reading (acts as a stable
        # duplicate of the most recent real reading).
        last = DEVICES[device_id].get("current")
        if last is None:
            last = _default_reading(register_map)
        return JSONResponse(content=last)

    mode = spec.get("mode", "normal")

    if mode == "timeout":
        await asyncio.sleep(GATEWAY_TIMEOUT + 5.0)  # exceeds gateway poll timeout
        return JSONResponse(content=_default_reading(register_map))

    if mode == "5xx":
        raise HTTPException(status_code=503, detail="simulated 503")

    if mode == "notfound":
        raise HTTPException(status_code=404, detail="simulated 404")

    if mode == "malformed":
        return PlainTextResponse("this is not json {{{", media_type="text/plain")

    # Build the reading payload for the value-bearing modes.
    reading = _build_normal(spec, register_map)

    if mode == "missing":
        # Drop one required register.
        regs = dict(reading["registers"])
        for drop in (40001, 40002, 40003, 40004):
            if drop in regs:
                del regs[drop]
                break
        out = {"sequence": reading["sequence"], "timestamp": reading["timestamp"], "registers": regs}
        DEVICES[device_id]["current"] = out
        return JSONResponse(content=out)

    if mode == "outofrange":
        regs = dict(reading["registers"])
        # Force temperature to an impossible value (raw 99999 -> 9999.9 C) so it
        # is rejected at Stage A as out-of-range.
        regs[40001] = 99999
        out = {"sequence": reading["sequence"], "timestamp": reading["timestamp"], "registers": regs}
        DEVICES[device_id]["current"] = out
        return JSONResponse(content=out)

    if mode == "invalidstatus":
        regs = dict(reading["registers"])
        # high-temp-v1 accepts 3; standard-v1 does not. Use a code invalid for
        # standard-v1 to exercise the cross-map rejection reliably.
        regs[40004] = 42
        out = {"sequence": reading["sequence"], "timestamp": reading["timestamp"], "registers": regs}
        DEVICES[device_id]["current"] = out
        return JSONResponse(content=out)

    # normal
    DEVICES[device_id]["current"] = reading
    return JSONResponse(content=reading)


# ---------------- control API (used by the verifier) -------------------

@app.post("/control/device")
async def control_register(spec: DeviceSpec):
    async with _STATE_LOCK:
        DEVICES[spec.id] = {
            "register_map": spec.register_map,
            "current": spec.initial,
        }
        QUEUES.setdefault(spec.id, [])
    return {"ok": True, "id": spec.id, "register_map": spec.register_map}


@app.post("/control/device/{device_id}/enqueue")
async def control_enqueue(device_id: str, spec: EnqueueSpec):
    if device_id not in DEVICES:
        raise HTTPException(status_code=404, detail="no such device")
    async with _STATE_LOCK:
        QUEUES.setdefault(device_id, []).append(spec.model_dump())
    return {"ok": True, "queued": len(QUEUES[device_id])}


@app.post("/control/device/{device_id}/script")
async def control_script(device_id: str, spec: ScriptSpec):
    if device_id not in DEVICES:
        raise HTTPException(status_code=404, detail="no such device")
    async with _STATE_LOCK:
        QUEUES[device_id] = [s.model_dump() for s in spec.responses]
    return {"ok": True, "queued": len(QUEUES[device_id])}


@app.get("/control/device/{device_id}/queue")
async def control_queue(device_id: str):
    async with _STATE_LOCK:
        q = list(QUEUES.get(device_id, []))
    return {"id": device_id, "remaining": len(q), "queue": q}


@app.post("/control/reset")
async def control_reset():
    async with _STATE_LOCK:
        DEVICES.clear()
        QUEUES.clear()
    return {"ok": True, "devices": 0}
