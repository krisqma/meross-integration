"""Atrapa gniazdka Meross: serwer HTTP udający lokalne API `POST /config`.

Odpowiada tak, jak prawdziwy sprzęt: weryfikuje podpis `md5(messageId + key + timestamp)`
(wzór z `meross2homie/meross.py::_gen_boilerplate`), a przy złym podpisie zwraca
`method: "ERROR"` z `{"error": {"code": 5001, "detail": "sign error"}}`.

Obsługiwane przestrzenie nazw:
    Appliance.System.All, Appliance.System.Ability, Appliance.Control.ToggleX,
    Appliance.Control.Electricity, Appliance.Control.ConsumptionX, Appliance.System.DNDMode

Poza protokołem Meross wystawia endpointy diagnostyczne (`/debug/*`), z których korzystają
testy integracyjne — patrz `tests/integration/`.

Uruchomienie:
    python3 fake_device.py --uuid <32 hex> --mac 48:e1:e9:00:00:01 --inner-ip fake-1
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import date, datetime, timedelta
from hashlib import md5
from typing import Any

from aiohttp import web

NS_ALL = "Appliance.System.All"
NS_ABILITY = "Appliance.System.Ability"
NS_ONLINE = "Appliance.System.Online"
NS_TOGGLEX = "Appliance.Control.ToggleX"
NS_ELECTRICITY = "Appliance.Control.Electricity"
NS_CONSUMPTIONX = "Appliance.Control.ConsumptionX"
NS_DND = "Appliance.System.DNDMode"

DATE_FORMAT = "%Y-%m-%d"

# Moc bazowa kanału przy włączonym przekaźniku. Kanał 0 dostaje najwięcej, kolejne mniej,
# żeby w testach dało się rozróżnić kanały po odczycie.
BASE_POWER_W = 41.2
CHANNEL_POWER_STEP_W = 7.5


class FakeMerossDevice:
    def __init__(
        self,
        uuid: str,
        mac: str,
        key: str,
        dev_type: str,
        channels: int,
        inner_ip: str,
        sub_type: str = "eu",
        hw_version: str = "2.0.0",
        fw_version: str = "6.1.9",
        chip_type: str = "mt7686",
    ) -> None:
        self.uuid = uuid
        self.mac = mac.lower()
        self.key = key
        self.dev_type = dev_type
        self.inner_ip = inner_ip
        self.sub_type = sub_type
        self.hw_version = hw_version
        self.fw_version = fw_version
        self.chip_type = chip_type

        self.channels: dict[int, bool] = {i: False for i in range(channels)}
        self.dnd = False

        # Licznik zużycia narastająco (Wh), naliczany leniwie z upływu czasu i bieżącej mocy.
        # Start od niezerowej wartości: udajemy gniazdko, które pracowało już dziś rano,
        # żeby panel miał co pokazać zaraz po starcie stacku.
        self.energy_wh: dict[int, float] = {i: 120.0 + 30.0 * i for i in range(channels)}
        self._accrued_at = time.time()
        self.boot_time = time.time()

        # Diagnostyka dla testów integracyjnych.
        self.request_counts: dict[str, int] = {}
        self.method_counts: dict[str, int] = {}
        self.sign_errors = 0
        self.bad_requests = 0
        self.last_request: dict[str, Any] | None = None
        self.set_log: list[dict[str, Any]] = []

    # --- protokół -----------------------------------------------------------------

    def sign(self, message_id: str, timestamp: int) -> str:
        return md5(f"{message_id}{self.key}{timestamp}".encode("utf8")).hexdigest().lower()

    def _envelope(self, method: str, namespace: str, payload: dict, message_id: str | None = None) -> dict:
        timestamp = int(round(time.time()))
        if not message_id:
            message_id = md5(str(random.randint(0xFFFFFFFF, 0xFFFFFFFFF)).encode("utf-8")).hexdigest().lower()
        return {
            "header": {
                "from": f"/appliance/{self.uuid}/publish",
                "messageId": message_id,
                "method": method,
                "namespace": namespace,
                "payloadVersion": 1,
                "sign": self.sign(message_id, timestamp),
                "timestamp": timestamp,
                "triggerSrc": "Device",
            },
            "payload": payload,
        }

    def error(self, namespace: str, code: int, detail: str, message_id: str | None = None) -> dict:
        return self._envelope("ERROR", namespace, {"error": {"code": code, "detail": detail}}, message_id)

    def handle(self, request_body: dict) -> dict:
        header = request_body.get("header") or {}
        namespace = str(header.get("namespace", ""))
        method = str(header.get("method", ""))
        message_id = str(header.get("messageId", ""))
        timestamp = header.get("timestamp", "")
        payload = request_body.get("payload") or {}

        self.last_request = {"namespace": namespace, "method": method, "at": time.time()}
        self.request_counts[namespace] = self.request_counts.get(namespace, 0) + 1
        self.method_counts[f"{method} {namespace}"] = self.method_counts.get(f"{method} {namespace}", 0) + 1

        if header.get("sign") != self.sign(message_id, timestamp):
            self.sign_errors += 1
            return self.error(namespace, 5001, "sign error", message_id)

        handler = {
            NS_ALL: self._ns_all,
            NS_ABILITY: self._ns_ability,
            NS_ONLINE: self._ns_online,
            NS_TOGGLEX: self._ns_togglex,
            NS_ELECTRICITY: self._ns_electricity,
            NS_CONSUMPTIONX: self._ns_consumptionx,
            NS_DND: self._ns_dnd,
        }.get(namespace)

        if handler is None:
            return self.error(namespace, 5000, "namespace error", message_id)

        try:
            response_payload = handler(method, payload)
        except (KeyError, TypeError, ValueError):
            return self.error(namespace, 5000, "payload error", message_id)

        ack = "SETACK" if method == "SET" else "GETACK"
        return self._envelope(ack, namespace, response_payload, message_id)

    # --- przestrzenie nazw --------------------------------------------------------

    def _ns_all(self, method: str, payload: dict) -> dict:
        now = int(round(time.time()))
        return {
            "all": {
                "system": {
                    "hardware": {
                        "type": self.dev_type,
                        "subType": self.sub_type,
                        "version": self.hw_version,
                        "chipType": self.chip_type,
                        "uuid": self.uuid,
                        "macAddress": self.mac,
                    },
                    "firmware": {
                        "version": self.fw_version,
                        "compileTime": "2023/01/01 00:00:00 GMT +02:00",
                        "wifiMac": self.mac,
                        # Most po interview przełącza się na ten adres, więc musi tu być nazwa/IP,
                        # pod którym most faktycznie widzi atrapę.
                        "innerIp": self.inner_ip,
                        "server": "0.0.0.0",
                        "port": 1883,
                        "userId": 0,
                    },
                    "time": {
                        "timestamp": now,
                        "timezone": os.environ.get("TZ", "Europe/Warsaw"),
                        "timeRule": [],
                    },
                    "online": {"status": 1},
                },
                "digest": {
                    # Z tej listy most wylicza kanały i buduje węzły switch / switch-N.
                    "togglex": [
                        {"channel": channel, "onoff": int(state), "lmTime": now}
                        for channel, state in sorted(self.channels.items())
                    ],
                    "triggerx": [],
                    "timerx": [],
                },
            }
        }

    def _ns_ability(self, method: str, payload: dict) -> dict:
        # meross_iot dobiera mixiny po tej liście, więc wymieniamy dokładnie to, co obsługujemy.
        return {
            "ability": {
                NS_ALL: {},
                NS_ABILITY: {},
                NS_ONLINE: {},
                NS_TOGGLEX: {},
                NS_ELECTRICITY: {},
                NS_CONSUMPTIONX: {},
                NS_DND: {},
            }
        }

    def _ns_online(self, method: str, payload: dict) -> dict:
        return {"online": {"status": 1}}

    def _ns_togglex(self, method: str, payload: dict) -> dict:
        now = int(round(time.time()))
        if method == "SET":
            self._accrue_energy()
            entries = payload["togglex"]
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries:
                channel = int(entry.get("channel", 0))
                if channel not in self.channels:
                    raise ValueError(f"unknown channel {channel}")
                state = bool(int(entry["onoff"]))
                self.channels[channel] = state
                self.set_log.append({"at": time.time(), "channel": channel, "onoff": int(state)})
            return {}

        channel = payload.get("channel")
        if channel is None:
            return {
                "togglex": [
                    {"channel": ch, "onoff": int(state), "lmTime": now} for ch, state in sorted(self.channels.items())
                ]
            }
        channel = int(channel)
        return {"togglex": {"channel": channel, "onoff": int(self.channels[channel]), "lmTime": now}}

    def _ns_electricity(self, method: str, payload: dict) -> dict:
        self._accrue_energy()
        channel = int(payload.get("channel", 0))
        if channel not in self.channels:
            raise ValueError(f"unknown channel {channel}")
        voltage_v = round(random.uniform(228.0, 236.0), 1)
        power_w = self.power_w(channel)
        current_a = power_w / voltage_v if voltage_v else 0.0
        # Sprzęt raportuje: napięcie w dV, prąd w mA, moc w mW.
        return {
            "electricity": {
                "channel": channel,
                "current": int(round(current_a * 1000)),
                "voltage": int(round(voltage_v * 10)),
                "power": int(round(power_w * 1000)),
                "config": {"voltageRatio": 188, "electricityRatio": 102},
            }
        }

    def _ns_consumptionx(self, method: str, payload: dict) -> dict:
        self._accrue_energy()
        channel = int(payload.get("channel", 0))
        if channel not in self.channels:
            raise ValueError(f"unknown channel {channel}")
        today = date.today()
        entries = []
        # Kilka dni historii + dzisiejszy, narastający licznik. Most sortuje po dacie
        # i bierze najświeższy wpis jako dzienne zużycie, więc dzisiejsza data musi tu być.
        for days_ago in range(3, 0, -1):
            day = today - timedelta(days=days_ago)
            entries.append(
                {
                    "date": day.strftime(DATE_FORMAT),
                    "time": int(datetime.combine(day, datetime.min.time()).timestamp()),
                    "value": 150 * days_ago + channel,
                }
            )
        entries.append(
            {
                "date": today.strftime(DATE_FORMAT),
                "time": int(round(time.time())),
                "value": int(round(self.energy_wh[channel])),
            }
        )
        return {"consumptionx": entries}

    def _ns_dnd(self, method: str, payload: dict) -> dict:
        if method == "SET":
            self.dnd = bool(int(payload["DNDMode"]["mode"]))
            return {}
        return {"DNDMode": {"mode": int(self.dnd)}}

    # --- stan pomiarowy -----------------------------------------------------------

    def power_w(self, channel: int) -> float:
        if not self.channels[channel]:
            # Wyłączony kanał: moc bliska zeru (przekaźnik otwarty, zostaje pobór własny).
            return round(random.uniform(0.0, 0.3), 2)
        base = BASE_POWER_W + channel * CHANNEL_POWER_STEP_W
        return round(base * random.uniform(0.97, 1.03), 2)

    def _accrue_energy(self) -> None:
        now = time.time()
        hours = (now - self._accrued_at) / 3600.0
        self._accrued_at = now
        for channel, state in self.channels.items():
            if state:
                self.energy_wh[channel] += (BASE_POWER_W + channel * CHANNEL_POWER_STEP_W) * hours

    def debug_state(self) -> dict:
        self._accrue_energy()
        return {
            "uuid": self.uuid,
            "mac": self.mac,
            "type": self.dev_type,
            "inner_ip": self.inner_ip,
            "channels": {str(ch): state for ch, state in sorted(self.channels.items())},
            "dnd": self.dnd,
            "energy_wh": {str(ch): round(value, 4) for ch, value in sorted(self.energy_wh.items())},
            "requests": dict(sorted(self.request_counts.items())),
            "requests_by_method": dict(sorted(self.method_counts.items())),
            "requests_total": sum(self.request_counts.values()),
            "sign_errors": self.sign_errors,
            "bad_requests": self.bad_requests,
            "last_request": self.last_request,
            "set_log": self.set_log[-20:],
            "uptime_s": round(time.time() - self.boot_time, 1),
        }


# --- serwer HTTP ------------------------------------------------------------------


async def handle_config(request: web.Request) -> web.Response:
    device: FakeMerossDevice = request.app["device"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    if not isinstance(body, dict):
        # Prawdziwy sprzęt na taki śmieć się wywraca i restartuje (tak działa `reboot_device`
        # w moście). Atrapa świadomie tego nie udaje i odpowiada błędem.
        device.bad_requests += 1
        return web.json_response(device.error("", 5000, "payload error"))
    return web.json_response(device.handle(body))


async def handle_debug_state(request: web.Request) -> web.Response:
    device: FakeMerossDevice = request.app["device"]
    return web.json_response(device.debug_state())


async def handle_debug_set_state(request: web.Request) -> web.Response:
    """Zmiana stanu z pominięciem protokołu — odpowiednik naciśnięcia przycisku na gniazdku."""
    device: FakeMerossDevice = request.app["device"]
    body = await request.json()
    for channel, state in (body.get("channels") or {}).items():
        device.channels[int(channel)] = bool(state)
    if "dnd" in body:
        device.dnd = bool(body["dnd"])
    return web.json_response(device.debug_state())


async def handle_debug_reset(request: web.Request) -> web.Response:
    device: FakeMerossDevice = request.app["device"]
    device.request_counts.clear()
    device.method_counts.clear()
    device.set_log.clear()
    device.sign_errors = 0
    device.bad_requests = 0
    return web.json_response(device.debug_state())


def build_app(device: FakeMerossDevice) -> web.Application:
    app = web.Application()
    app["device"] = device
    app.add_routes(
        [
            web.post("/config", handle_config),
            web.get("/debug/state", handle_debug_state),
            web.post("/debug/state", handle_debug_set_state),
            web.post("/debug/reset", handle_debug_reset),
        ]
    )
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atrapa gniazdka Meross (lokalne API HTTP)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FAKE_PORT", "80")))
    parser.add_argument("--uuid", default=os.environ.get("FAKE_UUID", "0" * 31 + "1"))
    parser.add_argument("--mac", default=os.environ.get("FAKE_MAC", "48:e1:e9:00:00:01"))
    parser.add_argument("--key", default=os.environ.get("FAKE_DEVICE_KEY", "testkey123"))
    parser.add_argument("--type", dest="dev_type", default=os.environ.get("FAKE_TYPE", "mss310"))
    parser.add_argument("--channels", type=int, default=int(os.environ.get("FAKE_CHANNELS", "1")))
    parser.add_argument(
        "--inner-ip",
        default=os.environ.get("FAKE_INNER_IP", ""),
        help="Adres, pod którym most widzi tę atrapę (nazwa kontenera albo IP). "
        "Trafia do all.system.firmware.innerIp, a most po interview na niego przechodzi.",
    )
    parser.add_argument("--sub-type", default=os.environ.get("FAKE_SUB_TYPE", "eu"))
    parser.add_argument("--hw-version", default=os.environ.get("FAKE_HW_VERSION", "2.0.0"))
    parser.add_argument("--fw-version", default=os.environ.get("FAKE_FW_VERSION", "6.1.9"))
    parser.add_argument("--chip-type", default=os.environ.get("FAKE_CHIP_TYPE", "mt7686"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.inner_ip:
        raise SystemExit("--inner-ip jest wymagany: most po interview odpytuje urządzenie pod tym adresem")
    device = FakeMerossDevice(
        uuid=args.uuid,
        mac=args.mac,
        key=args.key,
        dev_type=args.dev_type,
        channels=args.channels,
        inner_ip=args.inner_ip,
        sub_type=args.sub_type,
        hw_version=args.hw_version,
        fw_version=args.fw_version,
        chip_type=args.chip_type,
    )
    print(
        f"Atrapa {device.dev_type} {device.uuid} ({device.mac}), kanały: {len(device.channels)}, "
        f"innerIp: {device.inner_ip}, port: {args.port}",
        flush=True,
    )
    web.run_app(build_app(device), port=args.port, access_log=None)


if __name__ == "__main__":
    main()
