"""HTTP and WebSocket server: runs the simulation loop and serves the dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import metrics
from .catalog import catalog_as_dict, load_catalog
from .engine import Engine, SiteConfig, compare
from .exchange import ExchangePublisher

STATIC = Path(__file__).with_name("static")
TICK_SECONDS = 0.5
CONFIG_FIELDS = {f.name for f in fields(SiteConfig)}


class Sim:
    """The running simulation plus everything attached to it."""

    def __init__(self) -> None:
        self.cat = load_catalog()
        self.engine = Engine(SiteConfig(), self.cat)
        self.bus = ExchangePublisher()
        self.dt_min = 2.0
        self.paused = False
        self.clients: set[WebSocket] = set()
        self.log: list[dict[str, Any]] = []
        self.tick()

    def tick(self) -> dict[str, Any]:
        snap = self.engine.step(self.dt_min)
        events = self.engine.drain_events()
        logs = [e for e in events if e["kind"] == "log"]
        for entry in logs:
            entry["clock"] = self.engine.clock()
        self.log = (self.log + logs)[-100:]
        messages = self.bus.publish_tick(self.engine, snap, events)
        return {"type": "tick", "snap": snap, "log": logs, "bus": messages, "bus_status": self.bus.status(),
                "sim": {"dt_min": self.dt_min, "paused": self.paused}}  # fmt: skip

    def hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "catalog": catalog_as_dict(self.cat),
            "snap": self.engine.snapshot,
            "history": list(self.engine.history),
            "log": self.log,
            "bus": list(self.bus.feed)[-60:],
            "bus_status": self.bus.status(),
            "sim": {"dt_min": self.dt_min, "paused": self.paused},
        }

    async def broadcast(self, message: dict[str, Any]) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                self.clients.discard(ws)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if not self.paused:
                await self.broadcast(self.tick())


sim: Sim | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global sim
    sim = Sim()
    sim.bus.connect()
    task = asyncio.create_task(sim.run())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    sim.bus.close()


app = FastAPI(title="AI Factory Sim", version="0.1.0", lifespan=lifespan)


class LoadTarget(BaseModel):
    fraction: float = 0.75
    duration_min: float = 60
    ramp_min: float = 10


class Fault(BaseModel):
    kind: str
    index: int | None = None


class SimControl(BaseModel):
    dt_min: float | None = None
    paused: bool | None = None


def _sim() -> Sim:
    assert sim is not None
    return sim


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/catalog")
async def get_catalog() -> dict[str, Any]:
    return catalog_as_dict(_sim().cat)


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    return _sim().engine.snapshot


@app.post("/api/config")
async def set_config(patch: dict[str, Any]) -> dict[str, Any]:
    s = _sim()
    unknown = set(patch) - CONFIG_FIELDS
    if unknown:
        raise HTTPException(422, f"unknown config fields: {sorted(unknown)}")
    try:
        cfg = replace(s.engine.cfg, **patch)
        if "platform" in patch and "supply_c" not in patch:
            # A new platform starts at its own warmest allowed supply temperature.
            cfg = replace(cfg, supply_c=s.cat.platforms[cfg.platform].max_supply_c)
        s.engine.configure(cfg)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    message = s.tick() if not s.paused else {**s.tick(), "type": "tick"}
    message["rebuilt"] = s.engine.rebuilt
    if s.engine.rebuilt:
        message["history"] = list(s.engine.history)
    await s.broadcast(message)
    return asdict(s.engine.cfg)


@app.post("/api/sim")
async def control_sim(body: SimControl) -> dict[str, Any]:
    s = _sim()
    if body.dt_min is not None:
        s.dt_min = min(30.0, max(0.5, body.dt_min))
    if body.paused is not None:
        s.paused = body.paused
    await s.broadcast({"type": "sim", "sim": {"dt_min": s.dt_min, "paused": s.paused}})
    return {"dt_min": s.dt_min, "paused": s.paused}


@app.post("/api/actions/load-target")
async def load_target(body: LoadTarget) -> dict[str, Any]:
    tg = _sim().engine.set_load_target(body.fraction, body.duration_min, body.ramp_min)
    return {"correlation_id": tg["correlation_id"], "limit_kw": tg["limit_kw"]}


@app.post("/api/actions/fault")
async def fault(body: Fault) -> dict[str, Any]:
    try:
        return _sim().engine.inject_fault(body.kind, body.index)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/reset")
async def reset() -> dict[str, Any]:
    s = _sim()
    s.engine = Engine(s.engine.cfg, s.cat)
    s.log = []
    message = s.tick()
    message["rebuilt"] = True
    message["history"] = list(s.engine.history)
    await s.broadcast(message)
    return {"status": "reset"}


@app.get("/api/compare")
async def get_compare() -> dict[str, Any]:
    return compare(_sim().engine.cfg, _sim().cat)


@app.get("/api/bus")
async def get_bus(limit: int = 100) -> dict[str, Any]:
    s = _sim()
    return {"status": s.bus.status(), "messages": list(s.bus.feed)[-limit:]}


@app.get("/metrics", response_class=PlainTextResponse)
async def get_metrics() -> str:
    s = _sim()
    ids = [s.engine.rack_id(i) for i in range(s.engine.n)]
    return metrics.render(s.engine.snapshot, ids, s.bus.status())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    s = _sim()
    await ws.accept()
    await ws.send_json(s.hello())
    s.clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        s.clients.discard(ws)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
