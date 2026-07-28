"""REST API webappa na FastAPI TestClient z atrapą huba MQTT.

Asercje idą na DOKŁADNY temat i payload publikacji (CONTRACT.md §2.4 i §3) oraz na kody
odpowiedzi z kontraktu. TestClient jest używany BEZ menedżera kontekstu — wtedy Starlette
nie odpala lifespanu, więc nie próbujemy się łączyć z brokerem i możemy wstrzyknąć atrapę
w `app.state.hub`.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEBAPP = Path(__file__).resolve().parents[2] / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

from app.main import _state_events, app  # noqa: E402
from app.mqtt import MqttHub, MqttNotConnected  # noqa: E402
from app.state import HomieState  # noqa: E402

DEV = "2103163085044890845648e1e962e3a6"


# --------------------------------------------------------------------------- atrapy


def snapshot_with(channels=(("switch", True, True),), dev=DEV, state="ready"):
    """Migawka zbudowana prawdziwym parserem — atrapa nie może się rozjechać z modelem."""
    parser = HomieState()
    events = [
        (f"homie/{dev}/$name", "Gniazdko salon"),
        (f"homie/{dev}/$state", state),
        (f"homie/{dev}/$localip", "192.168.1.122"),
        (f"homie/{dev}/$mac", "48:e1:e9:62:e3:a6"),
        (f"homie/{dev}/$fw/version", "6.1.9"),
    ]
    for node, on, settable in channels:
        events += [
            (f"homie/{dev}/{node}/$name", "Switch"),
            (f"homie/{dev}/{node}/$type", "switch"),
            (f"homie/{dev}/{node}/power/$settable", "true" if settable else "false"),
            (f"homie/{dev}/{node}/power", "true" if on else "false"),
        ]
    parser.ingest_many(events)
    return parser.snapshot()


class FakeHub:
    """Atrapa `MqttHub` — wystawia dokładnie styk z CONTRACT.md §5."""

    def __init__(self, snapshot=None, connected=True, publish_error=None):
        self.connected = connected
        self.published = []
        self.listeners = []
        self._snapshot = snapshot if snapshot is not None else snapshot_with()
        self._publish_error = publish_error

    async def publish(self, topic: str, payload: str) -> None:
        if self._publish_error is not None:
            raise self._publish_error
        self.published.append((topic, payload))

    async def publish_set(self, dev: str, node: str, prop: str, value: str) -> None:
        await self.publish(f"homie/{dev}/{node}/{prop}/set", value)

    def snapshot(self) -> dict:
        return self._snapshot

    def subscribe_updates(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)


@pytest.fixture
def hub():
    fake = FakeHub()
    app.state.hub = fake
    yield fake
    app.state.hub = None


@pytest.fixture
def client():
    # Bez `with`: lifespan się nie odpala, więc nie ma prób łączenia z brokerem.
    return TestClient(app)


# ------------------------------------------------------------------ GET /api/devices


def test_get_devices_zwraca_ksztalt_z_kontraktu(client, hub):
    res = client.get("/api/devices")
    assert res.status_code == 200
    body = res.json()
    assert list(body) == ["devices"]
    device = body["devices"][0]
    assert device["id"] == DEV
    assert device["state"] == "ready"
    assert device["ip"] == "192.168.1.122"
    assert device["mac"] == "48:e1:e9:62:e3:a6"
    assert device["fw"] == "6.1.9"
    assert device["channels"][0]["node"] == "switch"


def test_get_devices_bez_huba_daje_503(client):
    app.state.hub = None
    assert client.get("/api/devices").status_code == 503


# ----------------------------------------------------------- POST .../{node}/power


def test_wlaczenie_publikuje_dokladnie_ten_temat_i_payload(client, hub):
    res = client.post(f"/api/devices/{DEV}/switch/power", json={"on": True})
    assert res.status_code == 202
    assert hub.published == [(f"homie/{DEV}/switch/power/set", "true")]


def test_wylaczenie_publikuje_false(client, hub):
    res = client.post(f"/api/devices/{DEV}/switch/power", json={"on": False})
    assert res.status_code == 202
    assert hub.published == [(f"homie/{DEV}/switch/power/set", "false")]


def test_kolejny_kanal_listwy_ma_swoj_temat(client, hub):
    hub._snapshot = snapshot_with(
        channels=(("switch", True, True), ("switch-1", False, True), ("switch-2", False, True))
    )
    client.post(f"/api/devices/{DEV}/switch-2/power", json={"on": True})
    assert hub.published == [(f"homie/{DEV}/switch-2/power/set", "true")]


def test_nieznane_urzadzenie_daje_404_i_nic_nie_publikuje(client, hub):
    res = client.post("/api/devices/nie-ma-takiego/switch/power", json={"on": True})
    assert res.status_code == 404
    assert hub.published == []


def test_nieznany_wezel_daje_404_i_nic_nie_publikuje(client, hub):
    res = client.post(f"/api/devices/{DEV}/switch-7/power", json={"on": True})
    assert res.status_code == 404
    assert hub.published == []


def test_wezel_niesterowalny_daje_409_i_nic_nie_publikuje(client, hub):
    hub._snapshot = snapshot_with(channels=(("switch", True, False),))
    res = client.post(f"/api/devices/{DEV}/switch/power", json={"on": True})
    assert res.status_code == 409
    assert hub.published == []


def test_bledne_cialo_zadania_daje_422(client, hub):
    assert client.post(f"/api/devices/{DEV}/switch/power", json={}).status_code == 422
    assert client.post(f"/api/devices/{DEV}/switch/power", json={"on": "tak"}).status_code == 422
    assert hub.published == []


def test_zerwany_broker_daje_503(client, hub):
    hub._publish_error = MqttNotConnected("Brak połączenia z brokerem mosquitto:1883")
    res = client.post(f"/api/devices/{DEV}/switch/power", json={"on": True})
    assert res.status_code == 503


# ------------------------------------------------------------------- GET /api/health


def test_health_zgodny_z_kontraktem(client, hub):
    hub._snapshot = snapshot_with(dev="aaa")
    hub._snapshot["devices"].extend(snapshot_with(dev="bbb")["devices"])
    body = client.get("/api/health").json()
    assert body == {"mqtt": True, "devices": 2, "jobs": 0}


def test_health_pokazuje_zerwane_mqtt(client, hub):
    hub.connected = False
    assert client.get("/api/health").json()["mqtt"] is False


# ------------------------------------------------------------------- GET /api/stream


def test_stream_jest_zarejestrowany_jako_get():
    trasa = next(r for r in app.routes if getattr(r, "path", None) == "/api/stream")
    assert "GET" in trasa.methods


async def test_stream_wysyla_pelny_stan_natychmiast(hub):
    """Kontrakt §3: po nawiązaniu połączenia serwer wysyła pełny stan od razu."""
    events = _state_events(hub)
    frame = await events.__anext__()
    assert frame["event"] == "state"
    assert json.loads(frame["data"]) == hub.snapshot()
    await events.aclose()


async def test_stream_wysyla_kolejna_migawke_po_zmianie(hub):
    events = _state_events(hub)
    await events.__anext__()  # migawka początkowa

    hub._snapshot = snapshot_with(channels=(("switch", False, True),))
    for listener in hub.listeners:
        listener()

    frame = await asyncio.wait_for(events.__anext__(), timeout=2)
    assert frame["event"] == "state"
    assert json.loads(frame["data"])["devices"][0]["channels"][0]["on"] is False
    await events.aclose()


async def test_stream_zwalnia_obserwatora_po_rozlaczeniu(hub):
    events = _state_events(hub)
    await events.__anext__()
    assert len(hub.listeners) == 1
    await events.aclose()
    assert hub.listeners == []


async def test_stream_sklada_serie_zmian_w_jedna_migawke(hub):
    """Jeden cykl pollingu mostu to kilkadziesiąt wiadomości — nie kilkadziesiąt ramek."""
    events = _state_events(hub)
    await events.__anext__()

    for _ in range(50):
        for listener in hub.listeners:
            listener()

    await asyncio.wait_for(events.__anext__(), timeout=2)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(events.__anext__(), timeout=0.2)
    await events.aclose()


# ---------------------------------------------------------- prawdziwy MqttHub (styk §5)


async def test_mqtthub_publish_set_sklada_temat_dokladnie():
    """Atrapa mogłaby kłamać — sprawdzamy prawdziwy `MqttHub.publish_set`."""

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def publish(self, topic, payload, qos=0, retain=False):
            self.calls.append((topic, payload, qos, retain))

    hub = MqttHub(host="mosquitto", port=1883, prefix="homie")
    fake = FakeClient()
    hub._client = fake
    hub.connected = True

    await hub.publish_set(DEV, "switch-1", "power", "true")

    assert fake.calls == [(f"homie/{DEV}/switch-1/power/set", "true", 1, False)]


async def test_mqtthub_bez_polaczenia_nie_publikuje_w_cisze():
    hub = MqttHub()
    with pytest.raises(MqttNotConnected):
        await hub.publish_set(DEV, "switch", "power", "true")


async def test_mqtthub_powiadamia_obserwatorow_tylko_przy_zmianie():
    hub = MqttHub()
    seen = []
    unsubscribe = hub.subscribe_updates(lambda: seen.append(hub.snapshot()))

    hub.ingest(f"homie/{DEV}/switch/power", "true")
    hub.ingest(f"homie/{DEV}/switch/power", "true")
    assert len(seen) == 1
    assert seen[0]["devices"][0]["channels"][0]["on"] is True

    unsubscribe()
    hub.ingest(f"homie/{DEV}/switch/power", "false")
    assert len(seen) == 1


async def test_mqtthub_wyjatek_obserwatora_nie_zabija_huba():
    hub = MqttHub()

    def zly():
        raise RuntimeError("bum")

    hub.subscribe_updates(zly)
    dobry = []
    hub.subscribe_updates(lambda: dobry.append(1))
    hub.ingest(f"homie/{DEV}/switch/power", "true")
    assert dobry == [1]
