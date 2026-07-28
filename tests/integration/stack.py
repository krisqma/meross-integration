"""Pomocniki testów integracyjnych: topologia stacku testowego, MQTT i diagnostyka atrap.

Ten moduł jest importowany przez testy w tym katalogu (bez wspólnego conftestu — każdy
moduł testowy sam dokłada ten katalog do sys.path).

Wszystko działa wewnątrz sieci Compose z docker-compose.test.yml: broker i atrapy są
widoczne po nazwach usług.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import time
from typing import Callable, Iterable

import aiohttp
import aiomqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
HOMIE_PREFIX = os.environ.get("HOMIE_PREFIX", "homie")

# Musi się zgadzać z docker-compose.test.yml (te same UUID-y co w CONTRACT.md §9).
FAKES: dict[str, dict] = {
    "2103163085044890845648e1e962e3a6": {"host": "fake-1", "type": "mss310", "channels": 1},
    "2101157387887490839348e1e945c257": {"host": "fake-2", "type": "mss310", "channels": 1},
    "2101070928400790839048e1e944b53f": {"host": "fake-3", "type": "mss425e", "channels": 4},
}

# Most potrzebuje czasu na start, interview trzech atrap i pierwszy pełny polling.
STARTUP_TIMEOUT = float(os.environ.get("TEST_STARTUP_TIMEOUT", "180"))
# polling_interval w tests/fakes/bridge-config/config.yml to 5 s.
POLL_TIMEOUT = float(os.environ.get("TEST_POLL_TIMEOUT", "45"))

STACK_HINT = (
    "Stack testowy nie odpowiada. Podnieś go najpierw:\n"
    "  docker compose -f docker-compose.test.yml up -d --build --wait"
)


def channel_node(prefix: str, channel: int) -> str:
    """Nazwa węzła Homie dla kanału — kanał 0 bez sufiksu (patrz `_channel_topic` w moście)."""
    return prefix if channel == 0 else f"{prefix}-{channel}"


def switch_topic(uuid: str, channel: int = 0) -> str:
    return f"{HOMIE_PREFIX}/{uuid}/{channel_node('switch', channel)}/power"


def _client(prefix: str = "m2h-tests") -> aiomqtt.Client:
    return aiomqtt.Client(
        hostname=MQTT_HOST,
        port=MQTT_PORT,
        identifier=f"{prefix}-{random.randint(0, 1_000_000)}",
        clean_session=True,
    )


async def snapshot(
    predicate: Callable[[dict[str, str]], bool] | None = None,
    *,
    timeout: float = 15.0,
    settle: float = 2.0,
    grace: float = 1.0,
    topic: str | None = None,
) -> dict[str, str]:
    """Zbiera retained z brokera do słownika `temat -> payload`.

    Bez `predicate` zbiera przez `settle` sekund (wystarcza na retained).
    Z `predicate` czeka, aż warunek będzie spełniony, maksymalnie `timeout` sekund,
    a potem jeszcze `grace` sekund — inaczej wynik urywa się w połowie retained
    i brakuje w nim atrybutów (`$unit`, `$datatype`, ...).
    """
    topic = topic or f"{HOMIE_PREFIX}/#"
    state: dict[str, str] = {}
    satisfied = asyncio.Event()

    try:
        async with _client() as client:
            await client.subscribe(topic, qos=1)

            async def reader() -> None:
                async for message in client.messages:
                    state[str(message.topic)] = message.payload.decode("utf-8", errors="replace")
                    if predicate is not None and predicate(state):
                        satisfied.set()

            task = asyncio.create_task(reader())
            try:
                if predicate is None:
                    await asyncio.sleep(settle)
                else:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(satisfied.wait(), timeout)
                    if satisfied.is_set():
                        await asyncio.sleep(grace)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    except aiomqtt.MqttError as e:  # pragma: no cover - zależy od środowiska
        raise AssertionError(f"{STACK_HINT}\nBłąd MQTT: {e}") from e

    return state


async def publish(topic: str, payload: str) -> None:
    try:
        async with _client("m2h-tests-pub") as client:
            await client.publish(topic, payload, qos=1)
    except aiomqtt.MqttError as e:  # pragma: no cover - zależy od środowiska
        raise AssertionError(f"{STACK_HINT}\nBłąd MQTT: {e}") from e


async def fake_state(host: str) -> dict:
    """Endpoint diagnostyczny atrapy: stan kanałów i liczniki żądań per przestrzeń nazw."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.get(f"http://{host}/debug/state") as response:
            response.raise_for_status()
            return await response.json()


async def fake_force_state(host: str, channels: dict[int, bool]) -> dict:
    """Przestawia kanały atrapy z pominięciem protokołu — jak przycisk na obudowie."""
    body = {"channels": {str(channel): state for channel, state in channels.items()}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.post(f"http://{host}/debug/state", json=body) as response:
            response.raise_for_status()
            return await response.json()


async def wait_for(check, *, timeout: float, interval: float = 1.0, what: str = "warunek"):
    """Odpytuje `check` (coroutine zwracającą wartość albo None) aż zwróci coś prawdziwego."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"Nie doczekano się: {what} (limit {timeout} s, ostatnio: {last!r})")


def expected_switch_topics() -> Iterable[str]:
    for uuid, spec in FAKES.items():
        for channel in range(spec["channels"]):
            yield switch_topic(uuid, channel)


def all_devices_ready(state: dict[str, str]) -> bool:
    for uuid in FAKES:
        if state.get(f"{HOMIE_PREFIX}/{uuid}/$state") != "ready":
            return False
    return all(topic in state for topic in expected_switch_topics())
