# -*- coding: utf-8 -*-
"""Pomocniki testów sprzętowych: inwentarz, klient lokalnego API HTTP i bramki bezpieczeństwa.

Ten moduł jest importowany przez testy w tym katalogu (bez wspólnego conftestu — każdy
moduł testowy sam dokłada ten katalog do `sys.path`, tak jak `tests/integration/stack.py`).

Zasady, które ten moduł wymusza za wszystkie testy sprzętowe:

* **Brak sprzętu nie jest błędem.** Gniazdko nieosiągalne pod swoim IP → `skip`
  z czytelnym komunikatem, nigdy wyjątek z gniazda sieciowego.
* **Brak klucza nie jest błędem.** Bez `MEROSS_KEY` testy wymagające podpisu → `skip`.
  Wyjątkiem jest test tożsamości, który celowo pyta z PUSTYM kluczem.
* **Zły klucz JEST błędem.** Jeśli `MEROSS_KEY` jest, a gniazdko zwraca `5001 sign error`,
  test pada — to realna informacja diagnostyczna, nie powód do cichego pominięcia.
* **Nic się nie przełącza bez jawnej zgody.** Patrz `require_switching_consent()`.

Świadomie tylko biblioteka standardowa (`urllib`) i testy synchroniczne: protokół to
jeden POST i jedna odpowiedź, więc `aiohttp` i pętla zdarzeń nic tu nie wnoszą.
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import md5
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

NS_ALL = "Appliance.System.All"
NS_ABILITY = "Appliance.System.Ability"
NS_TOGGLEX = "Appliance.Control.ToggleX"
NS_ELECTRICITY = "Appliance.Control.Electricity"
NS_CONSUMPTIONX = "Appliance.Control.ConsumptionX"

SIGN_ERROR_CODE = 5001

HTTP_TIMEOUT = float(os.environ.get("HW_HTTP_TIMEOUT", "6"))

# Sieciowe napięcie w Polsce to 230 V ±10 % (207–253 V). Bierzemy trochę szerzej, bo
# przeciążona instalacja i zaokrąglenia w sprzęcie potrafią wyjść poza normę, a test ma
# odsiać BŁĘDNĄ SKALĘ i zero, nie oceniać jakości sieci.
MAINS_VOLTAGE_MIN_V = 190.0
MAINS_VOLTAGE_MAX_V = 265.0

_APPLIANCE_FROM_RE = re.compile(r"^/appliance/(?P<uuid>[0-9a-fA-F]+)/(publish|subscribe)$")


@dataclass(frozen=True)
class DeviceSpec:
    """Jedno gniazdko z inwentarza CONTRACT.md §9."""

    ip: str
    uuid: str
    mac: str

    def __str__(self) -> str:  # czytelne id w nazwach testów i komunikatach
        return self.ip


# Inwentarz z CONTRACT.md §9 (potwierdzony skanem). MODELU I LICZBY KANAŁÓW TU NIE MA
# CELOWO — bez klucza z chmury nie dało się ich odczytać, więc żaden test nie ma prawa
# ich zakładać. Model wychodzi z `Appliance.System.All` i jest RAPORTOWANY, nie sprawdzany.
INVENTORY = (
    DeviceSpec("192.168.1.122", "2103163085044890845648e1e962e3a6", "48:e1:e9:62:e3:a6"),
    DeviceSpec("192.168.1.177", "2101157387887490839348e1e945c257", "48:e1:e9:45:c2:57"),
    DeviceSpec("192.168.1.240", "2101070928400790839048e1e944b53f", "48:e1:e9:44:b5:3f"),
)

# Wspólna parametryzacja: każdy test czytający leci po wszystkich trzech gniazdkach.
each_device = pytest.mark.parametrize("spec", INVENTORY, ids=[device.ip for device in INVENTORY])

UNREACHABLE_HINT = (
    "Gniazdko nie odpowiada pod adresem z inwentarza (CONTRACT.md §9). Najczęstsze przyczyny: "
    "DHCP przestawiło IP (./dot.sh discover pokaże aktualne), gniazdko jest wypięte, albo test "
    "leci w kontenerze bez dostępu do LAN-u 192.168.1.0/24."
)

KEY_HINT = (
    "Brak MEROSS_KEY — ani w środowisku, ani w .env. Bez klucza gniazdko odpowiada "
    "'5001 sign error' na wszystko poza samą tożsamością. Pobierz klucz: ./dot.sh login"
)

SWITCHING_HINT = (
    "Test PRZEŁĄCZA prawdziwe gniazdko, więc jest domyślnie wyłączony — nie wiadomo, co jest "
    "do niego wpięte (lampa? grzejnik? sprzęt, którego nie wolno wyłączyć?). Żeby go uruchomić, "
    "trzeba wskazać konkretne urządzenie i kanał ORAZ potwierdzić zgodę:\n"
    "  HW_ALLOW_SWITCHING=1 HW_TARGET_UUID=<uuid albo IP z inwentarza> HW_TARGET_CHANNEL=0 \\\n"
    "      ./dot.sh test hw -k przelaczanie"
)


# --- Środowisko ---------------------------------------------------------------------


def env_value(name: str) -> str:
    """Zmienna ze środowiska, a gdy pusta — z `.env` w katalogu repozytorium.

    Kontener testowy montuje repo w `/work`, więc `.env` jest w nim widoczny nawet wtedy,
    gdy Compose nie przekazał zmiennej. Czytamy plik przy każdym wywołaniu i nic nie
    cache'ujemy: użytkownik może odpalić `./dot.sh login` równolegle z testami.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value

    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return ""
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        if key.strip().removeprefix("export ").strip() != name:
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        return raw.strip()
    return ""


def require_key() -> str:
    key = env_value("MEROSS_KEY")
    if not key:
        pytest.skip(KEY_HINT)
    return key


def find_device(needle: str) -> DeviceSpec | None:
    """Urządzenie z inwentarza po UUID albo po IP (jedno i drugie jest wygodne w powłoce)."""
    needle = needle.strip().lower()
    for device in INVENTORY:
        if needle in (device.uuid.lower(), device.ip):
            return device
    return None


def require_switching_consent() -> tuple[DeviceSpec, int]:
    """Bramka testu przełączającego: zgoda + wskazanie urządzenia i kanału.

    Zgoda jest świadomie „gruba” (osobna zmienna, której nikt nie ustawia przypadkiem),
    a cel trzeba podać wprost — żeby nie dało się kliknąć w losowe gniazdko w mieszkaniu.
    """
    if env_value("HW_ALLOW_SWITCHING") not in ("1", "true", "yes", "tak"):
        pytest.skip(f"HW_ALLOW_SWITCHING nie jest ustawione na 1.\n{SWITCHING_HINT}")

    target = env_value("HW_TARGET_UUID")
    if not target:
        pytest.skip(f"HW_ALLOW_SWITCHING jest, ale brakuje HW_TARGET_UUID.\n{SWITCHING_HINT}")

    device = find_device(target)
    if device is None:
        # Literówka w UUID-zie to nie „brak sprzętu”, tylko błąd wywołania: pominięcie
        # wyglądałoby jak sukces, a użytkownik myślałby, że przełączył swoje gniazdko.
        known = ", ".join(f"{d.uuid} ({d.ip})" for d in INVENTORY)
        pytest.fail(f"HW_TARGET_UUID='{target}' nie jest w inwentarzu. Znane urządzenia: {known}")

    raw_channel = env_value("HW_TARGET_CHANNEL") or "0"
    if not raw_channel.isdigit():
        pytest.fail(f"HW_TARGET_CHANNEL musi być liczbą kanału, a jest '{raw_channel}'.")
    return device, int(raw_channel)


# --- Protokół: podpis, żądanie, odpowiedź -------------------------------------------


class Unreachable(RuntimeError):
    """Gniazdko nie odpowiedziało po HTTP (albo odpowiedziało czymś, co nie jest protokołem)."""


def sign(message_id: str, key: str, timestamp: int) -> str:
    """`md5(messageId + key + timestamp)` — tak liczy to sprzęt i most (`_gen_boilerplate`)."""
    return md5(f"{message_id}{key}{timestamp}".encode("utf8")).hexdigest().lower()


def build_message(namespace: str, key: str, method: str = "GET", payload: dict | None = None) -> dict:
    """Żądanie w formacie lokalnego API HTTP (`from` jest puste — tak robi most przy HTTP)."""
    message_id = md5(str(random.randint(0xFFFFFFFF, 0xFFFFFFFFF)).encode("utf-8")).hexdigest().lower()
    timestamp = int(round(time.time()))
    return {
        "header": {
            "from": "",
            "messageId": message_id,
            "method": method,
            "namespace": namespace,
            "payloadVersion": 1,
            "sign": sign(message_id, key, timestamp),
            "timestamp": timestamp,
        },
        "payload": payload or {},
    }


def rpc(ip: str, namespace: str, key: str, method: str = "GET", payload: dict | None = None) -> dict:
    """Jeden `POST http://IP/config`. Zwraca całą kopertę; brak sprzętu → `Unreachable`."""
    request = urllib.request.Request(
        f"http://{ip}/config",
        data=json.dumps(build_message(namespace, key, method, payload)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:  # odpowiedział, tylko kodem błędu — treść nas interesuje
        raw = exc.read() if hasattr(exc, "read") else b""
    except (urllib.error.URLError, OSError) as exc:
        raise Unreachable(f"{ip}: brak odpowiedzi na POST /config ({exc.__class__.__name__}: {exc})") from exc

    try:
        decoded = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise Unreachable(f"{ip}: odpowiedź na POST /config nie jest JSON-em") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("header"), dict):
        raise Unreachable(f"{ip}: odpowiedź nie ma nagłówka Meross — to nie gniazdko")
    return decoded


def port_open(ip: str, port: int = 80, timeout: float = 1.5) -> bool:
    """Szybka bramka przed POST-em: 3 s czekania na TCP zamiast 6 s na HTTP."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((ip, port)) == 0
        except OSError:
            return False


def uuid_from_header(header: dict) -> str | None:
    """UUID z `header.uuid`, a gdy go nie ma — z `header.from` (`/appliance/<uuid>/publish`)."""
    raw_uuid = header.get("uuid")
    if isinstance(raw_uuid, str) and raw_uuid.strip():
        return raw_uuid.strip().lower()
    raw_from = header.get("from")
    if isinstance(raw_from, str):
        match = _APPLIANCE_FROM_RE.match(raw_from.strip())
        if match:
            return match.group("uuid").lower()
    return None


def error_of(envelope: dict) -> tuple[int, str] | None:
    """`(kod, opis)` z koperty błędu albo `None`, gdy odpowiedź jest poprawna."""
    payload = envelope.get("payload")
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    try:
        code = int(error.get("code", 0))
    except (TypeError, ValueError):
        code = 0
    return code, str(error.get("detail", ""))


def ask(spec: DeviceSpec, namespace: str, key: str, method: str = "GET", payload: dict | None = None) -> dict:
    """Odpytanie gniazdka z gotową obsługą trzech przypadków brzegowych.

    Brak sprzętu → `skip`, zły klucz → `fail` z jasnym powodem, poprawna odpowiedź →
    sam `payload`. Wszystkie testy czytające idą tą drogą, żeby komunikaty były spójne.
    """
    try:
        envelope = rpc(spec.ip, namespace, key, method=method, payload=payload)
    except Unreachable as exc:
        pytest.skip(f"{exc}\n{UNREACHABLE_HINT}")

    error = error_of(envelope)
    if error is not None:
        code, detail = error
        if code == SIGN_ERROR_CODE:
            pytest.fail(
                f"{spec.ip}: gniazdko odrzuciło podpis ({code} {detail}). MEROSS_KEY jest ustawiony, "
                f"ale nie pasuje do tego urządzenia — sprawdź, czy klucz pochodzi z konta, na którym "
                f"gniazdko jest zaparowane (./dot.sh login, ewentualnie inny MEROSS_API_URL)."
            )
        pytest.fail(f"{spec.ip}: {namespace} zwróciło błąd {code} ({detail})")

    return envelope.get("payload") or {}


# --- Odczyty ------------------------------------------------------------------------


def channel_states(all_payload: dict) -> dict[int, bool]:
    """Stan kanałów z `all.digest.togglex` (starsze firmware'y mają `toggle` = jeden kanał)."""
    digest = ((all_payload.get("all") or {}).get("digest")) or {}
    togglex = digest.get("togglex")
    if isinstance(togglex, dict):
        togglex = [togglex]
    if isinstance(togglex, list) and togglex:
        return {int(entry.get("channel", 0)): bool(int(entry.get("onoff", 0))) for entry in togglex}

    toggle = digest.get("toggle")
    if isinstance(toggle, dict) and "onoff" in toggle:
        return {0: bool(int(toggle["onoff"]))}
    return {}


def ability_namespaces(spec: DeviceSpec, key: str) -> set[str]:
    payload = ask(spec, NS_ABILITY, key)
    ability = payload.get("ability")
    if not isinstance(ability, dict) or not ability:
        pytest.fail(f"{spec.ip}: {NS_ABILITY} nie zwróciło słownika zdolności: {payload!r}")
    return set(ability)


def fake_ability_namespaces() -> set[str]:
    """Zdolności, jakie udaje atrapa ze strumienia C — POBRANE Z JEJ KODU, nie przepisane.

    Gdyby lista była zduplikowana w teście, rozjechałaby się przy pierwszej zmianie atrapy
    i porównanie „sprzęt kontra atrapa” zaczęłoby kłamać. Import jest leniwy, bo
    `fake_device` ciągnie `aiohttp`.
    """
    fakes_dir = str(REPO_ROOT / "tests" / "fakes")
    if fakes_dir not in sys.path:
        sys.path.insert(0, fakes_dir)
    try:
        from fake_device import FakeMerossDevice
    except ImportError as exc:  # pragma: no cover - zależy od środowiska
        pytest.skip(f"Nie da się zaimportować atrapy z tests/fakes ({exc}) — nie ma z czym porównywać.")

    key = "hw-ability-probe"
    device = FakeMerossDevice(
        uuid="0" * 31 + "1",
        mac="48:e1:e9:00:00:01",
        key=key,
        dev_type="mss310",
        channels=1,
        inner_ip="127.0.0.1",
    )
    response = device.handle(build_message(NS_ABILITY, key))
    return set((response.get("payload") or {}).get("ability") or {})


def normalize_electricity(raw: dict) -> dict:
    """Surowe `Appliance.Control.Electricity` na jednostki SI.

    Sprzęt raportuje napięcie w dV, prąd w mA, a moc w mW (tak to dzieli `meross_iot`).
    Skalę potwierdzamy napięciem: jeśli po podzieleniu przez 10 wychodzi wartość sieciowa,
    to reszta też jest w jednostkach „mili”. Gdyby trafił się firmware podający wolty
    wprost, zostawiamy wartości bez dzielenia i mówimy o tym w raporcie — lepiej pokazać
    nieznaną skalę niż wywalić test na urządzeniu, którego nikt jeszcze nie widział.
    """
    voltage_raw = float(raw.get("voltage", 0) or 0)
    current_raw = float(raw.get("current", 0) or 0)
    power_raw = float(raw.get("power", 0) or 0)

    if MAINS_VOLTAGE_MIN_V <= voltage_raw / 10.0 <= MAINS_VOLTAGE_MAX_V:
        return {
            "scale": "dV/mA/mW (jak w meross_iot)",
            "voltage_v": round(voltage_raw / 10.0, 1),
            "current_a": round(current_raw / 1000.0, 3),
            "power_w": round(power_raw / 1000.0, 2),
        }
    return {
        "scale": "NIEROZPOZNANA — wartości surowe, bez dzielenia",
        "voltage_v": round(voltage_raw, 1),
        "current_a": round(current_raw, 3),
        "power_w": round(power_raw, 2),
    }


# --- Raportowanie -------------------------------------------------------------------


def report(title: str, lines: list[str]) -> None:
    """Blok diagnostyczny na stdout.

    Testy sprzętowe mają nie tylko sprawdzać, ale i POKAZYWAĆ, co stoi w mieszkaniu —
    model i liczba kanałów są tu odkrywane po raz pierwszy. `./dot.sh test hw` odpala
    pytest z `-s`, więc te bloki są widoczne także przy zielonym przebiegu.
    """
    print(f"\n--- {title} ---", flush=True)
    for line in lines:
        print(f"    {line}", flush=True)
