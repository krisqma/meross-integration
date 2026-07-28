#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wykrywa gniazdka Meross w LAN-ie i aktualizuje `bridge/config/devices.json`.

Dlaczego tylko biblioteka standardowa i składnia Pythona 3.9:
    `./dot.sh discover` uruchamia ten skrypt HOSTOWYM pythonem, bo kontener nie
    widzi tablicy ARP hosta. Systemowy python na Macu to 3.9 i nie ma w nim ani
    `aiohttp`, ani `requests` — więc: zero zależności, zero składni 3.10+.

Przebieg wykrywania:
    1. ustalenie podsieci: `--subnet` > `LAN_SUBNET` z otoczenia > `.env` > domyślna trasa,
    2. równoległy ping sweep, żeby zapełnić tablicę ARP,
    3. odczyt `arp -an` (fallback: `ip -4 neigh show`),
    4. sonda HTTP: `POST http://IP/config` z podpisanym `Appliance.System.All`.

OUI `48:e1:e9` jest tylko PODPOWIEDZIĄ — decyduje o kolejności sondowania, nie o
wyniku. Dowodem jest odpowiedź sondy: gniazdko Meross zwraca JSON, w którego
`header` siedzi `uuid` oraz `from` w postaci `/appliance/<uuid>/publish` — i robi
to RÓWNIEŻ wtedy, gdy klucz jest zły albo nieznany (odpowiada wówczas
`{"error": {"code": 5001, "detail": "sign error"}}`). Sprawdzone empirycznie na
trzech gniazdkach w tej sieci. Dzięki temu wykrywanie działa bez klucza, a lista
OUI nie musi być kompletna.

Podpis jest generowany dokładnie tak jak w sprzęcie i w moście:
`md5(messageId + key + timestamp)` — patrz `_gen_boilerplate`
w `meross2mqtt/meross2homie/meross.py`.

Format pliku wyjściowego musi zgadzać się z klasą `Persistence`
z `meross2mqtt/meross2homie/persistence.py`, czyli:
`{"devices": {"<uuid>": {"ip_address": "..."}}}`.

Kody wyjścia:
    0 — znaleziono co najmniej jedno gniazdko
    1 — nie znaleziono żadnego gniazdka
    2 — błąd (zła podsieć, brak dostępu do pliku wyjściowego, brak `arp`)
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import random
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from hashlib import md5
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import urllib.error
import urllib.request

# --- Stałe -------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "bridge" / "config" / "devices.json"
ENV_FILE = REPO_ROOT / ".env"

# Podpowiedź, nie filtr: tylko ustawia kolejność sondowania. `48:e1:e9` to OUI
# potwierdzone na gniazdkach w tej sieci; Meross używa też innych prefiksów,
# dlatego sondujemy WSZYSTKIE hosty z ARP, a nie tylko te z tej listy.
MEROSS_OUI_HINTS = ("48:e1:e9",)

DEFAULT_NAMESPACE = "Appliance.System.All"
DEFAULT_HTTP_TIMEOUT = 3.0
PING_TIMEOUT = 1.0
MAX_WORKERS = 96
MAX_SUBNET_HOSTS = 4096
SIGN_ERROR_CODE = 5001

MAC_BROADCAST = "ff:ff:ff:ff:ff:ff"
MAC_IPV4_MULTICAST_PREFIX = "01:00:5e"

_ARP_IP_RE = re.compile(r"\((?P<ip>\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+(?P<mac>\S+)")
_APPLIANCE_FROM_RE = re.compile(r"^/appliance/(?P<uuid>[0-9a-zA-Z_-]+)/(?:publish|subscribe)$")


# --- Modele ------------------------------------------------------------------


@dataclass
class ArpEntry:
    """Jeden rozwiązany wpis z tablicy ARP."""

    ip: str
    mac: str


@dataclass
class Probe:
    """Wynik sondy HTTP dla jednego adresu IP."""

    ip: str
    mac: Optional[str] = None  # z tablicy ARP
    uuid: Optional[str] = None  # obecny tylko dla gniazdek Meross
    key_ok: bool = False  # czy podpis został przyjęty (znamy klucz)
    model: Optional[str] = None  # all.system.hardware.type
    hw_version: Optional[str] = None
    fw_version: Optional[str] = None
    reported_mac: Optional[str] = None  # all.system.hardware.macAddress
    reported_ip: Optional[str] = None  # all.system.firmware.innerIp
    channels: Optional[int] = None  # len(all.digest.togglex)
    error_code: Optional[int] = None
    error_detail: Optional[str] = None
    failure: Optional[str] = None  # powód nieudanej sondy (diagnostyka)

    @property
    def is_meross(self) -> bool:
        return self.uuid is not None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ip": self.ip,
            "mac": self.mac,
            "uuid": self.uuid,
            "key_ok": self.key_ok,
            "model": self.model,
            "hw_version": self.hw_version,
            "fw_version": self.fw_version,
            "reported_mac": self.reported_mac,
            "reported_ip": self.reported_ip,
            "channels": self.channels,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
        }


@dataclass
class MergeStats:
    """Co się stało z `devices.json` po scaleniu."""

    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)  # znane, ale teraz nieobecne w sieci


# --- Pliki i otoczenie -------------------------------------------------------


def load_env_file(path: Path) -> Dict[str, str]:
    """Czyta plik w formacie `.env` (tylko KEY=VALUE, bez `export` i podstawień)."""
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def resolve_setting(cli_value: Optional[str], env_name: str, env_file: Dict[str, str]) -> Optional[str]:
    """Kolejność: flaga > zmienna środowiskowa > `.env` > None."""
    for candidate in (cli_value, os.environ.get(env_name), env_file.get(env_name)):
        if candidate:
            return candidate
    return None


# --- Sieć: podsieć, ping sweep, ARP ------------------------------------------


def local_ip() -> Optional[str]:
    """Adres interfejsu, którym wychodzi domyślna trasa (bez wysyłania pakietu)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _prefix_len_for_ip(ip: str) -> int:
    """Długość prefiksu dla podanego IP, odczytana z systemu; 24 jako fallback."""
    # Linux
    out = _run(["ip", "-o", "-4", "addr", "show"])
    if out:
        for line in out.splitlines():
            match = re.search(r"\binet\s+" + re.escape(ip) + r"/(\d{1,2})\b", line)
            if match:
                return int(match.group(1))
    # macOS / BSD: "inet 192.168.1.114 netmask 0xffffff00 broadcast ..."
    out = _run(["ifconfig"])
    if out:
        match = re.search(r"\binet\s+" + re.escape(ip) + r"\s+netmask\s+(0x[0-9a-fA-F]{8}|\S+)", out)
        if match:
            raw = match.group(1)
            try:
                mask = int(raw, 16) if raw.startswith("0x") else int(ipaddress.IPv4Address(raw))
                return bin(mask).count("1")
            except (ValueError, ipaddress.AddressValueError):
                pass
    return 24


def detect_subnet() -> Optional[str]:
    """Ustala podsieć z domyślnej trasy, np. `192.168.1.0/24`."""
    ip = local_ip()
    if not ip:
        return None
    prefix = _prefix_len_for_ip(ip)
    try:
        network = ipaddress.ip_network("{0}/{1}".format(ip, prefix), strict=False)
    except ValueError:
        return None
    return str(network)


def _run(cmd: Sequence[str], timeout: float = 5.0) -> Optional[str]:
    """Uruchamia polecenie i zwraca stdout albo None, gdy się nie da."""
    try:
        result = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 and not result.stdout:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def ping_command(ip: str, timeout: float = PING_TIMEOUT) -> List[str]:
    """Argumenty `ping` dla jednego pakietu — `-W` ma inną jednostkę na macOS (ms) i Linuksie (s)."""
    if platform.system() == "Darwin":
        return ["ping", "-c", "1", "-n", "-W", str(max(100, int(timeout * 1000))), ip]
    return ["ping", "-c", "1", "-n", "-W", str(max(1, int(round(timeout)))), ip]


def ping_host(ip: str, timeout: float = PING_TIMEOUT) -> bool:
    try:
        result = subprocess.run(
            ping_command(ip, timeout),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def ping_sweep(hosts: Sequence[str], workers: int = MAX_WORKERS) -> int:
    """Równoległy ping po całej podsieci — jedyny cel to zapełnienie tablicy ARP."""
    if not hosts:
        return 0
    with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as pool:
        return sum(1 for ok in pool.map(ping_host, hosts) if ok)


def normalize_mac(raw: str) -> Optional[str]:
    """Normalizuje MAC do `aa:bb:cc:dd:ee:ff`.

    macOS skraca oktety z wiodącym zerem (`4a:6:75:1f:9d:65`), więc każdy oktet
    trzeba dopełnić do dwóch znaków — inaczej porównanie z OUI nie zadziała.
    """
    if not raw:
        return None
    token = raw.strip().lower().replace("-", ":")
    parts = token.split(":")
    if len(parts) != 6:
        return None
    octets = []
    for part in parts:
        if not part or len(part) > 2 or any(char not in "0123456789abcdef" for char in part):
            return None
        octets.append(part.rjust(2, "0"))
    return ":".join(octets)


def is_usable_arp_entry(ip: str, mac: str) -> bool:
    """Odsiewa broadcast, multicast i adresy, pod którymi nie ma czego szukać."""
    if mac == MAC_BROADCAST or mac.startswith(MAC_IPV4_MULTICAST_PREFIX):
        return False
    try:
        address = ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return False
    return not (address.is_multicast or address.is_loopback or address.is_link_local or address.is_unspecified)


def parse_arp_output(text: str) -> List[ArpEntry]:
    """Parsuje wyjście `arp -an` z macOS i Linuksa.

    macOS:  `? (192.168.1.122) at 48:e1:e9:62:e3:a6 on en0 ifscope [ethernet]`
    Linux:  `? (192.168.1.122) at 48:e1:e9:62:e3:a6 [ether] on eth0`
    Pomijane: `(incomplete)` (macOS), `<incomplete>` (Linux), multicast i broadcast.
    """
    entries: Dict[str, str] = {}
    for line in text.splitlines():
        match = _ARP_IP_RE.search(line)
        if not match:
            continue
        mac = normalize_mac(match.group("mac"))
        if mac is None:  # `(incomplete)`, `<incomplete>`, śmieci
            continue
        ip = match.group("ip")
        if not is_usable_arp_entry(ip, mac):
            continue
        entries.setdefault(ip, mac)
    return [ArpEntry(ip=ip, mac=mac) for ip, mac in entries.items()]


def parse_ip_neigh_output(text: str) -> List[ArpEntry]:
    """Parsuje `ip -4 neigh show` — fallback dla systemów bez `arp` (np. slim kontenery)."""
    entries: Dict[str, str] = {}
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 2 or "lladdr" not in tokens:
            continue
        ip = tokens[0]
        mac = normalize_mac(tokens[tokens.index("lladdr") + 1])
        if mac is None or not is_usable_arp_entry(ip, mac):
            continue
        entries.setdefault(ip, mac)
    return [ArpEntry(ip=ip, mac=mac) for ip, mac in entries.items()]


def read_arp_table() -> List[ArpEntry]:
    """Czyta tablicę ARP systemu: najpierw `arp -an`, potem `ip -4 neigh show`."""
    out = _run(["arp", "-an"])
    if out:
        entries = parse_arp_output(out)
        if entries:
            return entries
    out = _run(["ip", "-4", "neigh", "show"])
    if out:
        return parse_ip_neigh_output(out)
    return []


def is_meross_oui(mac: Optional[str]) -> bool:
    """Czy MAC ma znany prefiks Meross. To PODPOWIEDŹ, nie dowód."""
    if not mac:
        return False
    return mac.lower().startswith(MEROSS_OUI_HINTS)


# --- Protokół Meross: podpis i sonda ----------------------------------------


def sign_request(message_id: str, dev_key: str, timestamp: int) -> str:
    """`md5(messageId + key + timestamp)` — dokładnie tak liczy to sprzęt.

    Wzór: `_gen_boilerplate` w `meross2mqtt/meross2homie/meross.py`.
    """
    return md5("{0}{1}{2}".format(message_id, dev_key, timestamp).encode("utf8")).hexdigest().lower()


def new_message_id() -> str:
    return md5(str(random.randint(0xFFFFFFFF, 0xFFFFFFFFF)).encode("utf-8")).hexdigest().lower()


def build_probe_message(
    dev_key: str,
    namespace: str = DEFAULT_NAMESPACE,
    message_id: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> Dict[str, Any]:
    """Buduje żądanie `GET <namespace>` w formacie lokalnego API HTTP gniazdka.

    Odpowiada `meross_http_payload()` z mostu (pole `from` jest puste dla HTTP).
    """
    if message_id is None:
        message_id = new_message_id()
    if timestamp is None:
        timestamp = int(round(time.time()))
    return {
        "header": {
            "from": "",
            "messageId": message_id,
            "method": "GET",
            "namespace": namespace,
            "payloadVersion": 1,
            "sign": sign_request(message_id, dev_key, timestamp),
            "timestamp": timestamp,
        },
        "payload": {},
    }


def uuid_from_response_header(header: Dict[str, Any]) -> Optional[str]:
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


def count_channels(digest: Dict[str, Any]) -> Optional[int]:
    """Liczba kanałów z `digest.togglex` (starsze firmware'y mają `toggle` = 1 kanał)."""
    togglex = digest.get("togglex")
    if isinstance(togglex, list):
        return len(togglex)
    if isinstance(togglex, dict):
        return 1
    if isinstance(digest.get("toggle"), dict):
        return 1
    return None


def parse_probe_response(payload: Any, ip: str = "", mac: Optional[str] = None) -> Probe:
    """Rozpoznaje gniazdko Meross po odpowiedzi na `POST /config`.

    Gniazdko udowadnia swoją tożsamość samym nagłówkiem (`uuid` / `from`) —
    również gdy klucz jest zły i w `payload` wraca `5001 sign error`.
    Gdy klucz jest poprawny, dochodzą jeszcze model i liczba kanałów z `payload.all`.
    """
    probe = Probe(ip=ip, mac=mac)
    if not isinstance(payload, dict):
        probe.failure = "odpowiedź nie jest obiektem JSON"
        return probe

    header = payload.get("header")
    if not isinstance(header, dict):
        probe.failure = "brak nagłówka Meross w odpowiedzi"
        return probe

    probe.uuid = uuid_from_response_header(header)
    if probe.uuid is None:
        probe.failure = "nagłówek bez uuid ani /appliance/<uuid>/publish"
        return probe

    body = payload.get("payload")
    body = body if isinstance(body, dict) else {}

    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        probe.error_code = code if isinstance(code, int) else None
        detail = error.get("detail")
        probe.error_detail = detail if isinstance(detail, str) else None
        return probe

    everything = body.get("all")
    if not isinstance(everything, dict):
        # Gniazdko odpowiedziało, ale nie tym, o co pytaliśmy — nadal je znamy.
        probe.key_ok = True
        return probe

    probe.key_ok = True
    system = everything.get("system") if isinstance(everything.get("system"), dict) else {}
    hardware = system.get("hardware") if isinstance(system.get("hardware"), dict) else {}
    firmware = system.get("firmware") if isinstance(system.get("firmware"), dict) else {}
    digest = everything.get("digest") if isinstance(everything.get("digest"), dict) else {}

    for attribute, source, key in (
        ("model", hardware, "type"),
        ("hw_version", hardware, "version"),
        ("reported_mac", hardware, "macAddress"),
        ("fw_version", firmware, "version"),
        ("reported_ip", firmware, "innerIp"),
    ):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            setattr(probe, attribute, value.strip())

    if probe.reported_mac:
        probe.reported_mac = normalize_mac(probe.reported_mac) or probe.reported_mac
    probe.channels = count_channels(digest)
    return probe


def tcp_port_open(ip: str, port: int = 80, timeout: float = 1.0) -> bool:
    """Szybka bramka przed sondą: bez otwartego portu 80 nie ma po co wysyłać POST-a."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def probe_device(ip: str, dev_key: str, timeout: float, mac: Optional[str] = None) -> Probe:
    """Sonduje jeden adres: `POST http://IP/config` z `Appliance.System.All`."""
    if not tcp_port_open(ip, 80, min(timeout, 1.5)):
        return Probe(ip=ip, mac=mac, failure="port 80 zamknięty")

    message = build_probe_message(dev_key)
    request = urllib.request.Request(
        "http://{0}/config".format(ip),
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:  # odpowiedział, ale kodem błędu
        raw = exc.read() if hasattr(exc, "read") else b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Probe(ip=ip, mac=mac, failure="brak odpowiedzi HTTP ({0})".format(exc.__class__.__name__))

    try:
        decoded = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        # Np. brama zwraca HTML — normalna sytuacja, nie błąd.
        return Probe(ip=ip, mac=mac, failure="odpowiedź nie jest JSON-em")
    return parse_probe_response(decoded, ip=ip, mac=mac)


# --- devices.json ------------------------------------------------------------


def _existing_devices(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Wyciąga słownik urządzeń z istniejącego pliku, tolerując wariant listowy.

    `Persistence.load` przyjmuje `devices` jako listę UUID-ów i zamienia ją na
    słownik z pustymi właściwościami — robimy to samo, żeby nie zgubić wpisów.
    """
    if not isinstance(raw, dict):
        return {}
    devices = raw.get("devices")
    if isinstance(devices, list):
        return {str(uuid): {} for uuid in devices}
    if not isinstance(devices, dict):
        return {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for uuid, props in devices.items():
        normalized[str(uuid)] = dict(props) if isinstance(props, dict) else {}
    return normalized


def load_devices_file(path: Path) -> Dict[str, Any]:
    """Czyta `devices.json`; brak pliku i pusty plik traktuje jak brak urządzeń."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def merge_devices(existing: Any, discovered: Dict[str, str]) -> Tuple[Dict[str, Any], MergeStats]:
    """Scala wykryte adresy z istniejącym stanem, nie gubiąc znanych urządzeń.

    Urządzenie, którego chwilowo nie ma w sieci (wyłączone, inny VLAN), zostaje
    w pliku z poprzednim IP — most spróbuje go użyć, a jeśli się nie uda, sam
    wróci do MQTT. Wynik ma kształt wymagany przez `Persistence`:
    `{"devices": {"<uuid>": {"ip_address": "..."}}}`.
    """
    original = _existing_devices(existing)
    devices = {uuid: dict(props) for uuid, props in original.items()}
    stats = MergeStats()

    for uuid, ip in sorted(discovered.items()):
        props = devices.setdefault(uuid, {})
        previous = props.get("ip_address")
        if uuid not in original:
            stats.added.append(uuid)
        elif previous != ip:
            stats.updated.append(uuid)
        else:
            stats.unchanged.append(uuid)
        props["ip_address"] = ip

    for uuid in devices:
        if uuid not in discovered:
            stats.kept.append(uuid)

    merged = dict(existing) if isinstance(existing, dict) else {}
    merged["devices"] = devices
    return merged, stats


def write_devices_file(path: Path, content: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as handle:
        json.dump(content, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


# --- Prezentacja -------------------------------------------------------------


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Prosta tabelka wyrównana do najszerszej komórki."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def line(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells)).rstrip()

    out = [line(headers), "  ".join("-" * width for width in widths)]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def describe_key_state(probe: Probe) -> str:
    if probe.key_ok:
        return "ok"
    if probe.error_code == SIGN_ERROR_CODE:
        return "zły/nieznany klucz (5001)"
    if probe.error_code is not None:
        return "błąd {0}".format(probe.error_code)
    return "nieznany"


def print_results(probes: Sequence[Probe], key_known: bool) -> None:
    headers = ["IP", "UUID", "MAC", "Model", "Kanały", "Klucz"]
    rows = []
    for probe in probes:
        rows.append(
            [
                probe.ip,
                probe.uuid or "?",
                probe.mac or probe.reported_mac or "?",
                probe.model or "?",
                str(probe.channels) if probe.channels is not None else "?",
                describe_key_state(probe),
            ]
        )
    print(render_table(headers, rows))
    if not key_known:
        print(
            "\nKlucz urządzeń nie jest znany, więc model i liczba kanałów zostają nieznane.\n"
            "Uruchom `./dot.sh login`, żeby pobrać klucz z chmury, i powtórz wykrywanie."
        )


# --- Główny przebieg ---------------------------------------------------------


def subnet_hosts(subnet: str) -> List[str]:
    """Lista adresów hostów w podsieci, z zabezpieczeniem przed skanem pół internetu."""
    network = ipaddress.ip_network(subnet, strict=False)
    if network.version != 4:
        raise ValueError("obsługiwany jest tylko IPv4")
    hosts = [str(host) for host in network.hosts()] if network.num_addresses > 2 else [str(network.network_address)]
    if len(hosts) > MAX_SUBNET_HOSTS:
        raise ValueError(
            "podsieć {0} ma {1} adresów, limit to {2} — podaj węższy zakres przez --subnet".format(
                subnet, len(hosts), MAX_SUBNET_HOSTS
            )
        )
    return hosts


def order_candidates(entries: Sequence[ArpEntry]) -> List[ArpEntry]:
    """Najpierw znane OUI Meross (szybszy pierwszy wynik), potem reszta — rosnąco po IP."""

    def sort_key(entry: ArpEntry) -> Tuple[int, int]:
        return (0 if is_meross_oui(entry.mac) else 1, int(ipaddress.IPv4Address(entry.ip)))

    return sorted(entries, key=sort_key)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discover.py",
        description="Wykrywa gniazdka Meross w LAN-ie i aktualizuje bridge/config/devices.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Przykłady:\n"
            "  ./tools/discover.py                       # skan domyślnej podsieci i zapis devices.json\n"
            "  ./tools/discover.py --dry-run             # tylko pokaż, nic nie zapisuj\n"
            "  ./tools/discover.py --subnet 192.168.1.0/24 --key abc123\n"
            "  ./tools/discover.py --json > urzadzenia.json\n"
        ),
    )
    parser.add_argument("--subnet", help="zakres do skanowania, np. 192.168.1.0/24 (domyślnie: LAN_SUBNET albo trasa domyślna)")
    parser.add_argument("--key", help="klucz urządzeń Meross (domyślnie: MEROSS_KEY z otoczenia albo .env)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_HTTP_TIMEOUT, help="limit czasu sondy HTTP w sekundach (domyślnie %(default)s)")
    parser.add_argument("--output", help="ścieżka devices.json (domyślnie {0})".format(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true", help="nie zapisuj pliku, tylko pokaż wynik")
    parser.add_argument("--json", action="store_true", dest="as_json", help="wypisz wynik jako JSON (do skryptów)")
    parser.add_argument("--only-oui", action="store_true", help="sonduj wyłącznie adresy z MAC-iem o znanym prefiksie Meross")
    parser.add_argument("--probe-all", action="store_true", help="sonduj każdy adres z podsieci, nie tylko obecne w ARP")
    parser.add_argument("--no-ping", action="store_true", help="pomiń ping sweep (korzystaj z tego, co już jest w ARP)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="liczba równoległych wątków (domyślnie %(default)s)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    def log(message: str) -> None:
        """Postęp na stdout, ale w trybie --json stdout jest zarezerwowany na wynik."""
        if not args.as_json:
            print(message)

    env_file = load_env_file(ENV_FILE)
    dev_key = resolve_setting(args.key, "MEROSS_KEY", env_file) or ""
    subnet = resolve_setting(args.subnet, "LAN_SUBNET", env_file) or detect_subnet()
    if not subnet:
        print("Nie udało się ustalić podsieci. Podaj ją przez --subnet, np. --subnet 192.168.1.0/24.", file=sys.stderr)
        return 2

    try:
        hosts = subnet_hosts(subnet)
    except ValueError as exc:
        print("Zła podsieć: {0}".format(exc), file=sys.stderr)
        return 2

    output = Path(args.output).expanduser() if args.output else DEFAULT_OUTPUT
    workers = max(1, args.workers)

    log("Podsieć: {0} ({1} adresów)".format(subnet, len(hosts)))
    log("Klucz urządzeń: {0}".format("znany" if dev_key else "NIEZNANY (wykryję gniazdka, ale nie model)"))

    if not args.no_ping:
        log("Ping sweep, żeby zapełnić tablicę ARP...")
        replied = ping_sweep(hosts, workers)
        log("  odpowiedziało na ping: {0}".format(replied))

    arp_entries = [entry for entry in read_arp_table() if entry.ip in set(hosts)]
    log("Wpisów w ARP w tej podsieci: {0}".format(len(arp_entries)))

    candidates = order_candidates(arp_entries)
    if args.only_oui:
        candidates = [entry for entry in candidates if is_meross_oui(entry.mac)]
    macs = {entry.ip: entry.mac for entry in candidates}
    if args.probe_all:
        known = {entry.ip for entry in candidates}
        candidates = candidates + [ArpEntry(ip=ip, mac="") for ip in hosts if ip not in known]

    if not candidates:
        print(
            "Brak adresów do sprawdzenia. Tablica ARP jest pusta — sprawdź, czy jesteś w tej samej sieci\n"
            "co gniazdka, i czy nie uruchamiasz skryptu w kontenerze (ARP hosta jest wtedy niewidoczny).",
            file=sys.stderr,
        )
        return 1

    log("Sonduję HTTP POST /config na {0} adresach...".format(len(candidates)))
    with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as pool:
        probes = list(
            pool.map(
                lambda entry: probe_device(entry.ip, dev_key, args.timeout, macs.get(entry.ip) or None),
                candidates,
            )
        )

    found = sorted((probe for probe in probes if probe.is_meross), key=lambda probe: int(ipaddress.IPv4Address(probe.ip)))
    discovered = {probe.uuid: probe.ip for probe in found if probe.uuid}

    existing = load_devices_file(output)
    merged, stats = merge_devices(existing, discovered)

    written = False
    if found and not args.dry_run:
        try:
            write_devices_file(output, merged)
            written = True
        except OSError as exc:
            print("Nie udało się zapisać {0}: {1}".format(output, exc), file=sys.stderr)
            return 2

    if args.as_json:
        print(
            json.dumps(
                {
                    "subnet": subnet,
                    "scanned": len(candidates),
                    "arp_entries": len(arp_entries),
                    "key_known": bool(dev_key),
                    "devices": [probe.as_dict() for probe in found],
                    "output": str(output),
                    "written": written,
                    "dry_run": bool(args.dry_run),
                    "merge": {
                        "added": stats.added,
                        "updated": stats.updated,
                        "unchanged": stats.unchanged,
                        "kept": stats.kept,
                    },
                    "devices_json": merged,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if found else 1

    if not found:
        print(
            "\nNie znalazłem żadnego gniazdka Meross.\n"
            "Co sprawdzić: czy jesteś w tej samej sieci Wi-Fi/LAN, czy podsieć ({0}) jest właściwa,\n"
            "czy gniazdka są zasilone, oraz czy router nie izoluje klientów (AP isolation).".format(subnet)
        )
        return 1

    print("\nZnalezione gniazdka Meross ({0}):\n".format(len(found)))
    print_results(found, bool(dev_key))

    print("")
    if args.dry_run:
        print("--dry-run: nic nie zapisałem. Bez tej flagi zaktualizowałbym {0}.".format(output))
    else:
        print("Zapisano {0}".format(output))
    print(
        "  nowe: {0}, zaktualizowane IP: {1}, bez zmian: {2}, zachowane (offline): {3}".format(
            len(stats.added), len(stats.updated), len(stats.unchanged), len(stats.kept)
        )
    )
    if stats.kept:
        print("  zachowane wpisy, których nie widziałem w sieci: {0}".format(", ".join(sorted(stats.kept))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
