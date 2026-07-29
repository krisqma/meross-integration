"""Aplikacja FastAPI: REST + SSE dla panelu, montaż modułu harmonogramów.

Uruchamiana przez `uvicorn app.main:app` z workdirem `/app` (patrz `webapp/Dockerfile`).
REST jest zamrożony w CONTRACT.md §3, styk z modułami — w §5.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import scheduler
from .mqtt import MqttHub, MqttNotConnected
from .scheduler import job_count, start_scheduler, stop_scheduler

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _load_cloud_names() -> Dict[str, str]:
    cloud_path = Path("/data/cloud-devices.json")
    if not cloud_path.is_file():
        return {}
    try:
        data = json.loads(cloud_path.read_text())
        return {uuid: info["name"] for uuid, info in data.get("devices", {}).items()}
    except Exception:
        log.warning("Nie udało się wczytać %s", cloud_path)
        return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = MqttHub(
        host=_env("MQTT_HOST", "mosquitto"),
        port=int(_env("MQTT_PORT", "1883")),
        prefix=_env("HOMIE_PREFIX", "homie"),
        cloud_names=_load_cloud_names(),
    )
    app.state.hub = hub
    # Hub łączy się w tle: brak brokera przy starcie nie blokuje panelu.
    await hub.start()
    await start_scheduler(hub)
    try:
        yield
    finally:
        await stop_scheduler()
        await hub.stop()


app = FastAPI(title="Gniazdka — panel", lifespan=lifespan)
app.include_router(scheduler.router)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class PowerIn(BaseModel):
    """Ciało `POST /api/devices/{dev}/{node}/power`."""

    on: bool


def _require_hub(request: Request) -> MqttHub:
    hub = getattr(request.app.state, "hub", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="Hub MQTT nie jest gotowy")
    return hub


def _find_channel(snapshot: dict, dev: str, node: str) -> dict:
    """Szuka kanału w migawce stanu; 404 dla nieznanego, 409 dla niesterowalnego."""
    device = next((d for d in snapshot.get("devices", []) if d.get("id") == dev), None)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Nieznane urządzenie: {dev}")
    channel = next((c for c in device.get("channels", []) if c.get("node") == node), None)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"Nieznany węzeł: {node}")
    if not channel.get("settable", False):
        raise HTTPException(status_code=409, detail=f"Węzeł {node} nie jest sterowalny")
    return channel


@app.get("/")
async def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="Brak pliku index.html")
    return FileResponse(str(index_file))


@app.get("/api/devices")
async def get_devices(request: Request) -> dict:
    return _require_hub(request).snapshot()


@app.post("/api/devices/{dev}/{node}/power", status_code=202)
async def set_power(dev: str, node: str, payload: PowerIn, request: Request):
    """Polecenie „wyślij” — prawdziwy stan wróci przez SSE po pollingu mostu."""
    hub = _require_hub(request)
    _find_channel(hub.snapshot(), dev, node)
    try:
        await hub.publish_set(dev, node, "power", "true" if payload.on else "false")
    except MqttNotConnected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(
        status_code=202,
        content={"dev": dev, "node": node, "on": payload.on},
    )


async def _state_events(hub: MqttHub):
    """Generator zdarzeń SSE: pełna migawka od razu, potem po każdej zmianie stanu.

    Wydzielony z trasy, żeby dał się przetestować bez HTTP — TestClient Starlette nie
    wysyła `http.disconnect`, więc czytanie nieskończonego strumienia przez niego wisi.
    """
    # maxsize=1 daje darmowe sklejanie: seria zmian z jednego cyklu pollingu mostu
    # wyprodukuje jedną migawkę, a nie kilkadziesiąt.
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    def on_change() -> None:
        if queue.empty():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    unsubscribe = hub.subscribe_updates(on_change)
    try:
        yield {"event": "state", "data": json.dumps(hub.snapshot(), ensure_ascii=False)}
        while True:
            await queue.get()
            yield {"event": "state", "data": json.dumps(hub.snapshot(), ensure_ascii=False)}
    finally:
        unsubscribe()


@app.get("/api/stream")
async def stream(request: Request):
    """SSE: zdarzenie `state` z tym samym kształtem, co `GET /api/devices`."""
    hub = _require_hub(request)
    return EventSourceResponse(_state_events(hub))


@app.get("/api/health")
async def health(request: Request) -> dict:
    hub: Optional[MqttHub] = getattr(request.app.state, "hub", None)
    devices = len(hub.snapshot().get("devices", [])) if hub is not None else 0
    return {
        "mqtt": bool(hub.connected) if hub is not None else False,
        "devices": devices,
        "jobs": job_count(),
    }
