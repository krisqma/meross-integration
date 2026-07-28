"""Testy integracyjne PANELU na atrapach: webapp -> broker -> most -> atrapa i powrót.

Tego nie da się sprawdzić testem jednostkowym. Testy jednostkowe webappa (`test_api.py`)
używają atrapy huba MQTT i `TestClient` Starlette, więc nie dotykają ani brokera, ani
mostu, ani SSE (`TestClient` wisi na nieskończonym strumieniu). Tutaj panel jest
prawdziwy, chodzi pod uvicornem w kontenerze `webapp` i rozmawia z tym samym brokerem,
co most.

Wymagają podniesionego stacku z docker-compose.test.yml i uruchomienia w kontenerze `tests`:

    docker compose -f docker-compose.test.yml up -d --build --wait
    docker compose -f docker-compose.test.yml run --rm tests pytest -m integration

Marker `integration` jest w pytest.ini domyślnie pomijany.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stack import (  # noqa: E402
    FAKES,
    HOMIE_PREFIX,
    POLL_TIMEOUT,
    STARTUP_TIMEOUT,
    all_devices_ready,
    fake_force_state,
    fake_state,
    snapshot,
    wait_for,
)

pytestmark = pytest.mark.integration

WEBAPP_URL = os.environ.get("WEBAPP_URL", "http://webapp:8080").rstrip("/")

#: Atrapa jednokanałowa, na której przełączamy — pozostałe zostają nietknięte.
DEV = "2103163085044890845648e1e962e3a6"
DEV_HOST = FAKES[DEV]["host"]
NODE = "switch"

#: Licznik z `/debug/state` atrapy. Rośnie TYLKO wtedy, gdy most naprawdę wysłał
#: `SET Appliance.Control.ToggleX` — panel nie ma jak go podbić „optymistycznie”.
TOGGLEX_SET = "SET Appliance.Control.ToggleX"

#: MAC-i z CONTRACT.md §9 — panel dostaje je z `homie/<dev>/$mac`.
MACS = {
    "2103163085044890845648e1e962e3a6": "48:e1:e9:62:e3:a6",
    "2101157387887490839348e1e945c257": "48:e1:e9:45:c2:57",
    "2101070928400790839048e1e944b53f": "48:e1:e9:44:b5:3f",
}

WEBAPP_HINT = (
    "Panel nie odpowiada. Podnieś stack testowy razem z nim:\n"
    "  docker compose -f docker-compose.test.yml up -d --build --wait"
)


# --------------------------------------------------------------------- pomocnicze


async def api_get(path: str) -> dict:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(f"{WEBAPP_URL}{path}") as response:
                response.raise_for_status()
                return await response.json()
    except aiohttp.ClientError as e:  # pragma: no cover - zależy od środowiska
        raise AssertionError(f"{WEBAPP_HINT}\nBłąd HTTP przy GET {path}: {e}") from e


async def api_post(path: str, body: dict, expected: int) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.post(f"{WEBAPP_URL}{path}", json=body) as response:
            payload = await response.json()
            assert response.status == expected, f"POST {path} -> {response.status}: {payload}"
            return payload


async def api_delete(path: str) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.delete(f"{WEBAPP_URL}{path}") as response:
            assert response.status in (204, 404), f"DELETE {path} -> {response.status}"


def find_channel(devices: dict, dev: str, node: str) -> dict:
    device = next((d for d in devices["devices"] if d["id"] == dev), None)
    assert device is not None, f"panel nie widzi urządzenia {dev}: {devices}"
    channel = next((c for c in device["channels"] if c["node"] == node), None)
    assert channel is not None, f"urządzenie {dev} nie ma węzła {node}: {device}"
    return channel


async def togglex_count(host: str) -> int:
    state = await fake_state(host)
    return state["requests_by_method"].get(TOGGLEX_SET, 0)


async def set_power(dev: str, node: str, on: bool) -> dict:
    return await api_post(f"/api/devices/{dev}/{node}/power", {"on": on}, expected=202)


async def wait_for_panel(dev: str, node: str, on: bool, *, timeout: float) -> dict:
    async def check():
        channel = find_channel(await api_get("/api/devices"), dev, node)
        return channel if channel["on"] is on else None

    return await wait_for(check, timeout=timeout, what=f"panel pokazuje {node} = {on}")


# ---------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session", autouse=True)
def panel_ready() -> None:
    """Czeka raz na sesję, aż most przepyta atrapy, a panel zbierze z nich retained."""
    state = asyncio.run(snapshot(all_devices_ready, timeout=STARTUP_TIMEOUT))
    if not all_devices_ready(state):
        pytest.fail("Most nie wystawił atrap jako ready — patrz testy w test_bridge_stack.py")

    async def panel_sees_everything():
        health = await api_get("/api/health")
        return health if health["mqtt"] and health["devices"] == len(FAKES) else None

    asyncio.run(
        wait_for(
            panel_sees_everything,
            timeout=POLL_TIMEOUT,
            what=f"panel widzi {len(FAKES)} urządzenia i ma połączenie z brokerem",
        )
    )


@pytest.fixture
async def switched_off() -> None:
    """Każdy test przełączający startuje od wyłączonego kanału atrapy."""
    await fake_force_state(DEV_HOST, {0: False})
    await wait_for_panel(DEV, NODE, False, timeout=POLL_TIMEOUT)


# ------------------------------------------------------------------------- testy


async def test_health_pokazuje_prawdziwe_polaczenie_z_brokerem() -> None:
    health = await api_get("/api/health")
    assert health["mqtt"] is True, "panel nie ma połączenia z brokerem stacku testowego"
    assert health["devices"] == len(FAKES)


async def test_panel_widzi_wszystkie_atrapy_z_wlasciwa_liczba_kanalow() -> None:
    """Dwie atrapy jednokanałowe i jedna listwa 4-kanałowa — model zbudowany z retained."""
    devices = await api_get("/api/devices")

    by_id = {d["id"]: d for d in devices["devices"]}
    assert set(by_id) == set(FAKES), "panel widzi inny zbiór urządzeń niż stack testowy"

    for uuid, spec in FAKES.items():
        device = by_id[uuid]
        assert device["state"] == "ready"
        assert device["mac"] == MACS[uuid]
        # W stacku testowym `innerIp` atrapy to nazwa usługi, nie adres IPv4 — zamierzone.
        assert device["ip"] == spec["host"]
        assert device["fw"], "$fw/version nie dotarło do panelu"

        nodes = [c["node"] for c in device["channels"]]
        expected = ["switch"] + [f"switch-{n}" for n in range(1, spec["channels"])]
        assert nodes == expected, f"{uuid} ({spec['type']}) ma węzły {nodes}, oczekiwano {expected}"


async def test_panel_pokazuje_odczyty_mocy_napiecia_pradu_i_energii() -> None:
    """Atrapy mają Electricity i ConsumptionX, więc żadne pole pomiarowe nie może być None."""
    devices = await api_get("/api/devices")

    for device in devices["devices"]:
        for channel in device["channels"]:
            for field in ("power_w", "voltage_v", "current_a", "energy_today_kwh"):
                assert channel[field] is not None, (
                    f"{device['id']}/{channel['node']}: brak odczytu {field}"
                )
            assert 200.0 <= channel["voltage_v"] <= 250.0
            assert channel["power_w"] >= 0.0
            assert channel["current_a"] >= 0.0
            assert channel["energy_today_kwh"] >= 0.0
            assert channel["settable"] is True


async def test_przelaczenie_z_panelu_dochodzi_do_atrapy(switched_off: None) -> None:
    """Dowód, że to nie jest optymistyczne odbicie stanu w panelu.

    Licznik `SET Appliance.Control.ToggleX` w atrapie rośnie tylko wtedy, gdy most
    naprawdę wysłał jej polecenie po HTTP.
    """
    before = await togglex_count(DEV_HOST)

    response = await set_power(DEV, NODE, True)
    assert response == {"dev": DEV, "node": NODE, "on": True}

    async def fake_got_the_command():
        state = await fake_state(DEV_HOST)
        got = state["requests_by_method"].get(TOGGLEX_SET, 0) > before
        return state if got and state["channels"]["0"] is True else None

    state = await wait_for(
        fake_got_the_command,
        timeout=POLL_TIMEOUT,
        what=f"atrapa {DEV_HOST} dostała {TOGGLEX_SET} i włączyła kanał 0",
    )
    assert state["channels"]["0"] is True


async def test_zmiana_stanu_wraca_do_api_devices_po_cyklu_pollingu(switched_off: None) -> None:
    await set_power(DEV, NODE, True)
    channel = await wait_for_panel(DEV, NODE, True, timeout=POLL_TIMEOUT)
    assert channel["on"] is True

    # I w drugą stronę: „przycisk na obudowie” (POST /debug/state omija protokół),
    # czyli zmiana, o której panel może się dowiedzieć wyłącznie przez polling mostu.
    await fake_force_state(DEV_HOST, {0: False})
    channel = await wait_for_panel(DEV, NODE, False, timeout=POLL_TIMEOUT)
    assert channel["on"] is False


async def test_sse_pod_prawdziwym_uvicornem_wysyla_ramki(switched_off: None) -> None:
    """SSE czytane po prawdziwym HTTP — pierwsza ramka od razu, druga po zmianie stanu.

    `TestClient` Starlette nie potrafi tego sprawdzić (nie wysyła `http.disconnect`
    i wisi na nieskończonym strumieniu), dlatego ten test istnieje tylko tutaj.
    """
    frames: list[dict] = []

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT + 15)) as session:
        async with session.get(f"{WEBAPP_URL}/api/stream") as response:
            assert response.status == 200
            assert response.headers["content-type"].startswith("text/event-stream")

            async def read_frames() -> None:
                event = None
                async for raw in response.content:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        assert event == "state", f"nazwa zdarzenia SSE to {event!r}, a ma być 'state'"
                        frames.append(json.loads(line.split(":", 1)[1].strip()))

            async def first_frame():
                return frames[0] if frames else None

            async def frame_with_switch_on():
                return next(
                    (f for f in frames[1:] if find_channel(f, DEV, NODE)["on"] is True), None
                )

            reader = asyncio.create_task(read_frames())
            try:
                # Kontrakt §3: po połączeniu serwer wysyła pełny stan natychmiast.
                opening = await wait_for(
                    first_frame,
                    timeout=15,
                    interval=0.2,
                    what="pierwsza ramka SSE zaraz po połączeniu",
                )
                assert len(opening["devices"]) == len(FAKES)
                assert find_channel(opening, DEV, NODE)["on"] is False

                await set_power(DEV, NODE, True)

                changed = await wait_for(
                    frame_with_switch_on,
                    timeout=POLL_TIMEOUT,
                    interval=0.5,
                    what="ramka SSE ze zmienionym stanem przełącznika",
                )
            finally:
                reader.cancel()

    assert len(frames) >= 2, f"strumień dał tylko {len(frames)} ramek"
    assert len(changed["devices"]) == len(FAKES), "ramka SSE ma inny kształt niż /api/devices"


async def test_harmonogram_odpala_i_przelacza_atrape(switched_off: None) -> None:
    """Harmonogram jednorazowy realnie dochodzi do sprzętu, a nie tylko do bazy.

    Termin liczymy w strefie panelu (`TZ`), bo `run_at` bez offsetu jest interpretowany
    jako czas lokalny — to ten sam kod, który obsługuje „codziennie 6:30”.
    """
    before = await togglex_count(DEV_HOST)
    delay = 20
    run_at = (datetime.now().astimezone() + timedelta(seconds=delay)).isoformat(timespec="seconds")

    created = await api_post(
        "/api/schedules",
        {
            "label": "Test integracyjny: jednorazowe włączenie",
            "dev": DEV,
            "node": NODE,
            "action": "on",
            "kind": "once",
            "run_at": run_at,
            "enabled": True,
        },
        expected=201,
    )
    try:
        assert created["next_run"] is not None, "job nie został zarejestrowany w APSchedulerze"
        assert (await api_get("/api/health"))["jobs"] >= 1

        async def fake_was_switched_by_the_job():
            state = await fake_state(DEV_HOST)
            fired = state["requests_by_method"].get(TOGGLEX_SET, 0) > before
            return state if fired and state["channels"]["0"] is True else None

        await wait_for(
            fake_was_switched_by_the_job,
            timeout=delay + POLL_TIMEOUT,
            what=f"harmonogram odpalił i włączył atrapę {DEV_HOST}",
        )

        channel = await wait_for_panel(DEV, NODE, True, timeout=POLL_TIMEOUT)
        assert channel["on"] is True

        # Po odpaleniu jednorazowy job znika, a wiersz zostaje w bazie jako historia.
        listed = await api_get("/api/schedules")
        row = next(s for s in listed["schedules"] if s["id"] == created["id"])
        assert row["next_run"] is None
    finally:
        await api_delete(f"/api/schedules/{created['id']}")
