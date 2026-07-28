"""Testy integracyjne: prawdziwy most + prawdziwy broker + trzy atrapy gniazdek.

Wymagają podniesionego stacku z docker-compose.test.yml i uruchomienia w kontenerze `tests`:

    docker compose -f docker-compose.test.yml up -d --build --wait
    docker compose -f docker-compose.test.yml run --rm tests pytest -m integration

Marker `integration` jest w pytest.ini domyślnie pomijany.
"""

from __future__ import annotations

import asyncio
import sys
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
    channel_node,
    expected_switch_topics,
    fake_force_state,
    fake_state,
    publish,
    snapshot,
    switch_topic,
    wait_for,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session", autouse=True)
def stack_ready() -> dict[str, str]:
    """Czeka raz na sesję, aż most przepyta wszystkie atrapy i wystawi je jako `ready`."""
    state = asyncio.run(snapshot(all_devices_ready, timeout=STARTUP_TIMEOUT))
    if not all_devices_ready(state):
        missing = [uuid for uuid in FAKES if state.get(f"{HOMIE_PREFIX}/{uuid}/$state") != "ready"]
        pytest.fail(
            "Most nie wystawił wszystkich atrap jako ready w "
            f"{STARTUP_TIMEOUT} s. Brakuje: {missing}. "
            "Sprawdź logi: docker compose -f docker-compose.test.yml logs bridge"
        )
    return state


async def test_bridge_exposes_all_fakes_as_ready(stack_ready: dict[str, str]) -> None:
    state = await snapshot()

    for uuid in FAKES:
        assert state.get(f"{HOMIE_PREFIX}/{uuid}/$state") == "ready"
        assert state.get(f"{HOMIE_PREFIX}/{uuid}/$homie") == "4.0.0"
        # Rozszerzenie legacy-firmware: most publikuje adres, pod którym widzi urządzenie.
        assert state.get(f"{HOMIE_PREFIX}/{uuid}/$localip") == FAKES[uuid]["host"]

    for topic in expected_switch_topics():
        assert topic in state, f"brak własności {topic}"
        assert state[topic] in ("true", "false")
        assert state[f"{topic}/$datatype"] == "boolean"
        assert state[f"{topic}/$settable"] == "true"


async def test_multichannel_fake_gets_switch_n_nodes() -> None:
    uuid, spec = next((u, s) for u, s in FAKES.items() if s["channels"] > 1)
    state = await snapshot()

    nodes = state[f"{HOMIE_PREFIX}/{uuid}/$nodes"].split(",")
    for channel in range(spec["channels"]):
        node = channel_node("switch", channel)
        assert node in nodes, f"brak węzła {node} w $nodes={nodes}"
        assert state[f"{HOMIE_PREFIX}/{uuid}/{node}/$type"] == "switch"
    assert "switch-1" in nodes and f"switch-{spec['channels'] - 1}" in nodes
    assert f"switch-{spec['channels']}" not in nodes


async def test_set_command_reaches_the_device_and_comes_back() -> None:
    uuid = next(u for u, s in FAKES.items() if s["channels"] == 1)
    host = FAKES[uuid]["host"]

    before = await fake_state(host)
    sets_before = before["requests_by_method"].get("SET Appliance.Control.ToggleX", 0)
    desired = not before["channels"]["0"]

    await publish(f"{switch_topic(uuid)}/set", "true" if desired else "false")

    async def device_switched():
        state = await fake_state(host)
        return state if state["channels"]["0"] == desired else None

    after = await wait_for(
        device_switched,
        timeout=POLL_TIMEOUT,
        what=f"atrapa {host} przestawi kanał 0 na {desired}",
    )

    sets_after = after["requests_by_method"].get("SET Appliance.Control.ToggleX", 0)
    assert sets_after > sets_before, "atrapa nie odebrała SET Appliance.Control.ToggleX"
    last_set = after["set_log"][-1]
    assert last_set["channel"] == 0 and last_set["onoff"] == int(desired)

    # A po cyklu pollingu stan wraca na tematy Homie.
    expected = "true" if desired else "false"
    state = await snapshot(
        lambda s: s.get(switch_topic(uuid)) == expected,
        timeout=POLL_TIMEOUT,
    )
    assert state.get(switch_topic(uuid)) == expected


async def test_change_made_on_the_device_shows_up_after_polling() -> None:
    """Zmiana po stronie gniazdka (odpowiednik przycisku) musi wrócić do Homie."""
    uuid, spec = next((u, s) for u, s in FAKES.items() if s["channels"] > 1)
    host = spec["host"]
    channel = 2

    current = await fake_state(host)
    desired = not current["channels"][str(channel)]
    await fake_force_state(host, {channel: desired})

    expected = "true" if desired else "false"
    topic = switch_topic(uuid, channel)
    state = await snapshot(lambda s: s.get(topic) == expected, timeout=POLL_TIMEOUT)
    assert state.get(topic) == expected, "most nie wychwycił zmiany stanu przy pollingu"


async def test_electricity_and_energy_readings_are_published() -> None:
    uuid = next(u for u, s in FAKES.items() if s["channels"] == 1)
    host = FAKES[uuid]["host"]

    await publish(f"{switch_topic(uuid)}/set", "true")

    async def device_on():
        state = await fake_state(host)
        return state if state["channels"]["0"] else None

    await wait_for(device_on, timeout=POLL_TIMEOUT, what=f"atrapa {host} włączy kanał 0")

    def has_power(state: dict[str, str]) -> bool:
        value = state.get(f"{HOMIE_PREFIX}/{uuid}/electricity/power")
        return value is not None and float(value) > 1.0

    state = await snapshot(has_power, timeout=POLL_TIMEOUT)

    assert float(state[f"{HOMIE_PREFIX}/{uuid}/electricity/power"]) > 1.0
    assert 200.0 < float(state[f"{HOMIE_PREFIX}/{uuid}/electricity/voltage"]) < 260.0
    assert float(state[f"{HOMIE_PREFIX}/{uuid}/electricity/current"]) > 0.0
    assert state[f"{HOMIE_PREFIX}/{uuid}/electricity/power/$unit"] == "W"

    assert float(state[f"{HOMIE_PREFIX}/{uuid}/energy/daily"]) > 0.0
    assert float(state[f"{HOMIE_PREFIX}/{uuid}/energy/total"]) > 0.0
    assert state[f"{HOMIE_PREFIX}/{uuid}/energy/daily/$unit"] == "kWh"
    assert state[f"{HOMIE_PREFIX}/{uuid}/energy/history"].startswith("[")


async def test_every_channel_has_its_own_measurement_nodes() -> None:
    uuid, spec = next((u, s) for u, s in FAKES.items() if s["channels"] > 1)
    state = await snapshot()
    nodes = state[f"{HOMIE_PREFIX}/{uuid}/$nodes"].split(",")

    for channel in range(spec["channels"]):
        for prefix in ("electricity", "energy"):
            node = channel_node(prefix, channel)
            assert node in nodes, f"brak węzła {node}"
            assert state[f"{HOMIE_PREFIX}/{uuid}/{node}/$type"] == "stats"


async def test_bridge_polls_every_supported_namespace() -> None:
    """Każda atrapa dostaje pełen komplet zapytań, jakie most wysyła przy interview i pollingu."""
    for uuid, spec in FAKES.items():
        state = await fake_state(spec["host"])
        assert state["uuid"] == uuid
        assert state["requests"].get("Appliance.System.All", 0) > 0
        assert state["requests"].get("Appliance.System.Ability", 0) > 0
        assert state["requests"].get("Appliance.Control.Electricity", 0) > 0
        assert state["requests"].get("Appliance.Control.ConsumptionX", 0) > 0
        assert state["requests"].get("Appliance.System.DNDMode", 0) > 0


async def test_fake_rejects_a_wrong_signature() -> None:
    """Kontrola samej atrapy: przy złym podpisie zwraca 5001 sign error, jak sprzęt."""
    host = FAKES[next(iter(FAKES))]["host"]
    errors_before = (await fake_state(host))["sign_errors"]
    bogus = {
        "header": {
            "messageId": "0" * 32,
            "method": "GET",
            "namespace": "Appliance.System.All",
            "payloadVersion": 1,
            "sign": "f" * 32,
            "timestamp": 1,
        },
        "payload": {},
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.post(f"http://{host}/config", json=bogus) as response:
            body = await response.json()

    assert body["header"]["method"] == "ERROR"
    assert body["payload"]["error"] == {"code": 5001, "detail": "sign error"}
    assert (await fake_state(host))["sign_errors"] == errors_before + 1
