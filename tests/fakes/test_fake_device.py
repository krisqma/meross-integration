"""Testy samej atrapy — bez Dockera i bez sieci, na czystej logice protokołu.

Pilnują kontraktu z mostem: kształtu `Appliance.System.All` (z tego most buduje kanały
i węzły Homie), weryfikacji podpisu i jednostek w odczytach energii.
"""

from __future__ import annotations

import sys
import time
from datetime import date
from hashlib import md5
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_device import FakeMerossDevice  # noqa: E402

KEY = "testkey123"
UUID = "2103163085044890845648e1e962e3a6"
MAC = "48:e1:e9:62:e3:a6"


def make_device(channels: int = 1, dev_type: str = "mss310") -> FakeMerossDevice:
    return FakeMerossDevice(
        uuid=UUID,
        mac=MAC,
        key=KEY,
        dev_type=dev_type,
        channels=channels,
        inner_ip="fake-1",
    )


def request(namespace: str, method: str = "GET", payload: dict | None = None, key: str = KEY) -> dict:
    """Buduje żądanie tak, jak robi to `_gen_boilerplate` w moście."""
    message_id = md5(namespace.encode()).hexdigest().lower()
    timestamp = int(round(time.time()))
    sign = md5(f"{message_id}{key}{timestamp}".encode("utf8")).hexdigest().lower()
    return {
        "header": {
            "from": "",
            "messageId": message_id,
            "method": method,
            "namespace": namespace,
            "payloadVersion": 1,
            "sign": sign,
            "timestamp": timestamp,
        },
        "payload": payload or {},
    }


def test_wrong_key_yields_the_same_error_as_real_hardware():
    device = make_device()
    response = device.handle(request("Appliance.System.All", key="wrong"))

    assert response["header"]["method"] == "ERROR"
    assert response["payload"] == {"error": {"code": 5001, "detail": "sign error"}}
    assert device.sign_errors == 1


def test_system_all_carries_everything_the_bridge_reads():
    device = make_device()
    payload = device.handle(request("Appliance.System.All"))["payload"]

    hardware = payload["all"]["system"]["hardware"]
    firmware = payload["all"]["system"]["firmware"]
    assert hardware["uuid"] == UUID
    assert hardware["macAddress"] == MAC
    # MerossMqttDeviceInfo.from_system_all_payload sięga dokładnie po te pola.
    for field in ("type", "subType", "version", "chipType"):
        assert hardware[field]
    assert firmware["version"]
    # Most po interview odpytuje urządzenie pod adresem z innerIp.
    assert firmware["innerIp"] == "fake-1"
    assert payload["all"]["system"]["online"]["status"] == 1


def test_digest_togglex_lists_every_channel():
    device = make_device(channels=4, dev_type="mss425e")
    payload = device.handle(request("Appliance.System.All"))["payload"]

    togglex = payload["all"]["digest"]["togglex"]
    assert [entry["channel"] for entry in togglex] == [0, 1, 2, 3]
    assert all(entry["onoff"] == 0 for entry in togglex)


def test_ability_lists_only_supported_namespaces():
    device = make_device()
    abilities = device.handle(request("Appliance.System.Ability"))["payload"]["ability"]

    assert set(abilities) == {
        "Appliance.System.All",
        "Appliance.System.Ability",
        "Appliance.System.Online",
        "Appliance.Control.ToggleX",
        "Appliance.Control.Electricity",
        "Appliance.Control.ConsumptionX",
        "Appliance.System.DNDMode",
    }


def test_togglex_set_changes_state_and_is_visible_in_digest():
    device = make_device(channels=2)
    response = device.handle(
        request("Appliance.System.All", method="GET"),
    )
    assert response["header"]["method"] == "GETACK"

    ack = device.handle(
        request("Appliance.Control.ToggleX", method="SET", payload={"togglex": {"channel": 1, "onoff": 1}})
    )
    assert ack["header"]["method"] == "SETACK"
    assert device.channels == {0: False, 1: True}

    digest = device.handle(request("Appliance.System.All"))["payload"]["all"]["digest"]["togglex"]
    assert digest[1]["onoff"] == 1

    state = device.debug_state()
    assert state["channels"] == {"0": False, "1": True}
    assert state["requests_by_method"]["SET Appliance.Control.ToggleX"] == 1
    assert state["set_log"][-1]["channel"] == 1


def test_electricity_units_match_what_meross_iot_expects():
    device = make_device()
    device.handle(request("Appliance.Control.ToggleX", method="SET", payload={"togglex": {"channel": 0, "onoff": 1}}))

    data = device.handle(request("Appliance.Control.Electricity", payload={"channel": 0}))["payload"]["electricity"]

    # meross_iot dzieli: prąd przez 1000 (mA), napięcie przez 10 (dV), moc przez 1000 (mW).
    assert 200 < data["voltage"] / 10 < 260
    assert data["power"] / 1000 > 1.0
    assert data["current"] / 1000 > 0.0


def test_electricity_drops_to_almost_zero_when_the_channel_is_off():
    device = make_device()
    data = device.handle(request("Appliance.Control.Electricity", payload={"channel": 0}))["payload"]["electricity"]

    assert data["power"] / 1000 < 1.0
    assert 200 < data["voltage"] / 10 < 260


def test_consumption_contains_today():
    device = make_device()
    entries = device.handle(request("Appliance.Control.ConsumptionX", payload={"channel": 0}))["payload"]["consumptionx"]

    assert entries[-1]["date"] == date.today().strftime("%Y-%m-%d")
    assert len(entries) > 1
    assert all(isinstance(entry["value"], int) for entry in entries)


def test_dnd_round_trip():
    device = make_device()
    assert device.handle(request("Appliance.System.DNDMode"))["payload"] == {"DNDMode": {"mode": 0}}

    device.handle(request("Appliance.System.DNDMode", method="SET", payload={"DNDMode": {"mode": 1}}))
    assert device.handle(request("Appliance.System.DNDMode"))["payload"] == {"DNDMode": {"mode": 1}}


def test_unknown_namespace_is_reported_not_crashed():
    device = make_device()
    response = device.handle(request("Appliance.Control.Light"))

    assert response["header"]["method"] == "ERROR"
    assert response["payload"]["error"]["code"] == 5000


def test_debug_counters_group_requests_by_namespace():
    device = make_device()
    device.handle(request("Appliance.System.All"))
    device.handle(request("Appliance.System.All"))
    device.handle(request("Appliance.Control.Electricity", payload={"channel": 0}))

    state = device.debug_state()
    assert state["requests"]["Appliance.System.All"] == 2
    assert state["requests"]["Appliance.Control.Electricity"] == 1
    assert state["requests_total"] == 3
