# -*- coding: utf-8 -*-
"""Testy jednostkowe `tools/discover.py` — bez sieci, bez Dockera, bez sprzętu.

Zakres:
    * parsowanie zapisanych wyjść `arp -an` (macOS i Linux) oraz `ip -4 neigh show`,
    * rozpoznawanie gniazdka po odpowiedzi na `POST /config` — i przy poprawnym
      kluczu, i przy `5001 sign error`,
    * poprawność podpisu `md5(messageId + key + timestamp)` względem wektora
      policzonego niezależnie w teście,
    * scalanie `devices.json` bez gubienia znanych urządzeń,
    * NAJWAŻNIEJSZE: wygenerowany `devices.json` wczytany prawdziwą klasą
      `Persistence` z klonu mostu (`meross2mqtt/meross2homie/persistence.py`).

Świadomie NIE importujemy `meross2homie.meross`, żeby sprawdzić podpis — ten moduł
ciąga `CONFIG`, `aiohttp` i `meross_iot`, których nie ma w środowisku testowym.
Wzór podpisu jest w teście powtórzony wprost z `_gen_boilerplate`.

Nie ma `tests/conftest.py` (celowo — pliki dzielone są własnością innych strumieni),
więc `sys.path` ustawiamy tutaj.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))

import discover  # noqa: E402  (import po ustawieniu sys.path)

MEROSS_IPS = ("192.168.1.122", "192.168.1.177", "192.168.1.240")
UUID_SALON = "2103163085044890845648e1e962e3a6"
UUID_LISTWA = "2101157387887490839348e1e945c257"
UUID_TRZECIE = "2101070928400790839048e1e944b53f"


def load_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_fixture(name):
    return json.loads(load_fixture(name))


# --- MAC-i -------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("48:e1:e9:62:e3:a6", "48:e1:e9:62:e3:a6"),
        ("48:E1:E9:45:C2:57", "48:e1:e9:45:c2:57"),
        # macOS skraca oktety z wiodącym zerem — realna pułapka przy porównaniu z OUI
        ("4a:6:75:1f:9d:65", "4a:06:75:1f:9d:65"),
        ("6:c:a7:27:f6:9f", "06:0c:a7:27:f6:9f"),
        ("70:c9:32:3:93:9", "70:c9:32:03:93:09"),
        ("48-e1-e9-62-e3-a6", "48:e1:e9:62:e3:a6"),
        ("  48:e1:e9:62:e3:A6 ", "48:e1:e9:62:e3:a6"),
    ],
)
def test_normalize_mac_dopelnia_pojedyncze_cyfry(raw, expected):
    assert discover.normalize_mac(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["(incomplete)", "<incomplete>", "incomplete", "", "48:e1:e9:62:e3", "48:e1:e9:62:e3:a6:00", "zz:e1:e9:62:e3:a6", "48:e1e:e9:62:e3:a6"],
)
def test_normalize_mac_odrzuca_smieci(raw):
    assert discover.normalize_mac(raw) is None


# --- Tablica ARP -------------------------------------------------------------


def test_parse_arp_macos():
    entries = {entry.ip: entry.mac for entry in discover.parse_arp_output(load_fixture("arp_macos.txt"))}

    for ip in MEROSS_IPS:
        assert ip in entries
    assert entries["192.168.1.122"] == "48:e1:e9:62:e3:a6"
    assert entries["192.168.1.177"] == "48:e1:e9:45:c2:57"  # w fixture wielkimi literami
    assert entries["192.168.1.240"] == "48:e1:e9:44:b5:3f"

    # skrócone oktety dopełnione
    assert entries["192.168.1.56"] == "4a:06:75:1f:9d:65"
    assert entries["192.168.1.69"] == "06:0c:a7:27:f6:9f"
    assert entries["192.168.1.162"] == "70:c9:32:03:93:09"

    # (incomplete) pominięte
    assert "192.168.1.2" not in entries
    assert "192.168.1.3" not in entries
    assert "169.254.169.254" not in entries

    # broadcast, multicast i link-local pominięte
    assert "192.168.1.255" not in entries
    assert "255.255.255.255" not in entries
    assert "224.0.0.251" not in entries
    assert "239.255.255.250" not in entries
    assert "169.254.250.93" not in entries


def test_parse_arp_linux():
    entries = {entry.ip: entry.mac for entry in discover.parse_arp_output(load_fixture("arp_linux.txt"))}

    assert set(entries) == {"192.168.1.1", "192.168.1.112", "192.168.1.122", "192.168.1.177", "192.168.1.240"}
    assert entries["192.168.1.240"] == "48:e1:e9:44:b5:3f"
    assert "192.168.1.4" not in entries  # <incomplete>
    assert "192.168.1.250" not in entries  # <incomplete>
    assert "224.0.0.251" not in entries
    assert "192.168.1.255" not in entries


def test_parse_arp_pusta_i_bezsensowna_tresc():
    assert discover.parse_arp_output("") == []
    assert discover.parse_arp_output("arp: no entries\n") == []
    assert discover.parse_arp_output("zupelnie inny format\n192.168.1.1 80:af:ca:7f:fe:64\n") == []


def test_parse_arp_dubluje_ip_bierze_pierwszy():
    text = "? (192.168.1.122) at 48:e1:e9:62:e3:a6 on en0 ifscope [ethernet]\n? (192.168.1.122) at 00:11:22:33:44:55 on en1 [ethernet]\n"
    entries = discover.parse_arp_output(text)
    assert len(entries) == 1
    assert entries[0].mac == "48:e1:e9:62:e3:a6"


def test_parse_ip_neigh_fallback():
    entries = {entry.ip: entry.mac for entry in discover.parse_ip_neigh_output(load_fixture("ip_neigh_linux.txt"))}

    assert set(entries) == {"192.168.1.1", "192.168.1.122", "192.168.1.177", "192.168.1.240"}
    assert entries["192.168.1.177"] == "48:e1:e9:45:c2:57"
    assert "192.168.1.4" not in entries  # FAILED, bez lladdr
    assert "224.0.0.251" not in entries


# --- OUI to tylko podpowiedź -------------------------------------------------


@pytest.mark.parametrize("mac, expected", [
    ("48:e1:e9:62:e3:a6", True),
    ("48:E1:E9:45:C2:57".lower(), True),
    ("80:af:ca:7f:fe:64", False),
    ("4a:06:75:1f:9d:65", False),
    ("", False),
    (None, False),
])
def test_is_meross_oui(mac, expected):
    assert discover.is_meross_oui(mac) is expected


def test_order_candidates_najpierw_oui_potem_po_ip():
    entries = [
        discover.ArpEntry("192.168.1.240", "48:e1:e9:44:b5:3f"),
        discover.ArpEntry("192.168.1.1", "80:af:ca:7f:fe:64"),
        discover.ArpEntry("192.168.1.122", "48:e1:e9:62:e3:a6"),
        discover.ArpEntry("192.168.1.10", "d4:c9:ef:52:50:20"),
    ]
    assert [entry.ip for entry in discover.order_candidates(entries)] == [
        "192.168.1.122",
        "192.168.1.240",
        "192.168.1.1",
        "192.168.1.10",
    ]


# --- Podpis ------------------------------------------------------------------


def test_sign_request_zgadza_sie_ze_wzorem_sprzetowym():
    """Wektor liczony niezależnie: md5(messageId + key + timestamp)."""
    message_id = "122e3e47835fefcd8aaf22d13ce21859"
    key = "sekretnyKluczUrzadzen1234"
    timestamp = 1785267080

    expected = hashlib.md5("{0}{1}{2}".format(message_id, key, timestamp).encode("utf8")).hexdigest().lower()

    assert discover.sign_request(message_id, key, timestamp) == expected
    assert discover.sign_request(message_id, key, timestamp) == "8cf6feb4f55c59857993f5f65d250bd5"


def test_sign_request_pusty_klucz_tez_ma_sens():
    """Bez klucza i tak podpisujemy — gniazdko odpowie `5001 sign error` i tym się zdradzi."""
    message_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    timestamp = 1700000000
    expected = hashlib.md5("{0}{1}".format(message_id, timestamp).encode("utf8")).hexdigest().lower()
    assert discover.sign_request(message_id, "", timestamp) == expected


def test_build_probe_message_ma_ksztalt_zadania_http():
    message = discover.build_probe_message("klucz", message_id="deadbeef" * 4, timestamp=1785267080)
    header = message["header"]

    assert header["namespace"] == "Appliance.System.All"
    assert header["method"] == "GET"
    assert header["payloadVersion"] == 1
    assert header["from"] == ""  # dla transportu HTTP most też wysyła puste `from`
    assert header["messageId"] == "deadbeef" * 4
    assert header["timestamp"] == 1785267080
    assert header["sign"] == discover.sign_request("deadbeef" * 4, "klucz", 1785267080)
    assert message["payload"] == {}


def test_build_probe_message_losuje_messageid():
    first = discover.build_probe_message("klucz")["header"]["messageId"]
    second = discover.build_probe_message("klucz")["header"]["messageId"]
    assert first != second
    assert len(first) == 32


# --- Rozpoznawanie odpowiedzi gniazdka ---------------------------------------


def test_rozpoznanie_przy_zlym_kluczu_sign_error():
    """Bez znajomości klucza gniazdko i tak się ujawnia — to podstawa wykrywania."""
    probe = discover.parse_probe_response(load_json_fixture("response_sign_error.json"), ip="192.168.1.122")

    assert probe.is_meross is True
    assert probe.uuid == UUID_SALON
    assert probe.key_ok is False
    assert probe.error_code == discover.SIGN_ERROR_CODE == 5001
    assert probe.error_detail == "sign error"
    assert probe.model is None and probe.channels is None
    assert "5001" in discover.describe_key_state(probe)


def test_rozpoznanie_przy_poprawnym_kluczu_daje_model_i_kanaly():
    probe = discover.parse_probe_response(load_json_fixture("response_system_all.json"), ip="192.168.1.122", mac="48:e1:e9:62:e3:a6")

    assert probe.is_meross is True
    assert probe.uuid == UUID_SALON
    assert probe.key_ok is True
    assert probe.error_code is None
    assert probe.model == "mss310"
    assert probe.channels == 1
    assert probe.fw_version == "6.1.9"
    assert probe.hw_version == "6.0.0"
    assert probe.reported_mac == "48:e1:e9:62:e3:a6"
    assert probe.reported_ip == "192.168.1.122"
    assert discover.describe_key_state(probe) == "ok"


def test_listwa_wielokanalowa_i_uuid_z_pola_from():
    """Ten fixture nie ma `header.uuid` — UUID musi wyjść z `/appliance/<uuid>/publish`."""
    payload = load_json_fixture("response_system_all_powerstrip.json")
    assert "uuid" not in payload["header"]

    probe = discover.parse_probe_response(payload, ip="192.168.1.177")

    assert probe.uuid == UUID_LISTWA
    assert probe.channels == 4
    assert probe.model == "mss425e"
    assert probe.reported_mac == "48:e1:e9:45:c2:57"  # znormalizowany z zapisu wielkimi literami


def test_uuid_z_naglowka_ma_pierwszenstwo_i_jest_male_litery():
    header = {"uuid": "2103163085044890845648E1E962E3A6", "from": "/appliance/cosinnego/publish"}
    assert discover.uuid_from_response_header(header) == UUID_SALON


def test_odpowiedz_bez_naglowka_meross_to_nie_gniazdko():
    probe = discover.parse_probe_response(load_json_fixture("response_not_meross.json"), ip="192.168.1.1")
    assert probe.is_meross is False
    assert probe.uuid is None
    assert probe.failure


@pytest.mark.parametrize("payload", [None, "<html>brama</html>", [1, 2, 3], 42, {"header": "nie-slownik"}, {"header": {}}])
def test_odpowiedzi_ktore_nie_sa_odpowiedzia_gniazdka(payload):
    probe = discover.parse_probe_response(payload, ip="192.168.1.1")
    assert probe.is_meross is False
    assert probe.failure


def test_gniazdko_ktore_odpowiedzialo_inaczej_niz_pytalismy_wciaz_sie_liczy():
    payload = {"header": {"uuid": UUID_TRZECIE, "from": "/appliance/{0}/publish".format(UUID_TRZECIE)}, "payload": {}}
    probe = discover.parse_probe_response(payload, ip="192.168.1.240")
    assert probe.uuid == UUID_TRZECIE
    assert probe.key_ok is True
    assert probe.model is None


def test_count_channels_dla_starszego_firmware_z_toggle():
    assert discover.count_channels({"toggle": {"onoff": 1}}) == 1
    assert discover.count_channels({"togglex": [{"channel": 0}, {"channel": 1}]}) == 2
    assert discover.count_channels({}) is None


# --- Scalanie devices.json ---------------------------------------------------


def test_merge_nie_gubi_urzadzen_ktorych_nie_ma_w_sieci():
    existing = load_json_fixture("devices_json_existing.json")
    discovered = {UUID_SALON: "192.168.1.122", UUID_LISTWA: "192.168.1.177"}

    merged, stats = discover.merge_devices(existing, discovered)

    assert merged["devices"][UUID_SALON]["ip_address"] == "192.168.1.122"  # IP podmienione
    assert merged["devices"][UUID_LISTWA]["ip_address"] == "192.168.1.177"  # nowy wpis
    assert merged["devices"]["ffffffffffffffffffffffffffffffff"]["ip_address"] == "192.168.1.200"  # zachowane

    assert stats.added == [UUID_LISTWA]
    assert stats.updated == [UUID_SALON]
    assert stats.unchanged == []
    assert stats.kept == ["ffffffffffffffffffffffffffffffff"]


def test_merge_bez_zmian_gdy_ip_takie_samo():
    existing = {"devices": {UUID_SALON: {"ip_address": "192.168.1.122"}}}
    merged, stats = discover.merge_devices(existing, {UUID_SALON: "192.168.1.122"})

    assert stats.unchanged == [UUID_SALON]
    assert stats.added == [] and stats.updated == [] and stats.kept == []
    assert merged["devices"][UUID_SALON]["ip_address"] == "192.168.1.122"


def test_merge_nie_modyfikuje_wejscia():
    existing = {"devices": {UUID_SALON: {"ip_address": "192.168.1.99"}}}
    discover.merge_devices(existing, {UUID_SALON: "192.168.1.122"})
    assert existing["devices"][UUID_SALON]["ip_address"] == "192.168.1.99"


def test_merge_obsluguje_listowy_wariant_devices():
    """`Persistence.load` przyjmuje `devices` jako listę UUID-ów — my też musimy."""
    existing = load_json_fixture("devices_json_legacy_list.json")
    merged, stats = discover.merge_devices(existing, {UUID_SALON: "192.168.1.122"})

    assert merged["devices"][UUID_TRZECIE] == {}
    assert merged["devices"][UUID_SALON]["ip_address"] == "192.168.1.122"
    assert stats.kept == [UUID_TRZECIE]


@pytest.mark.parametrize("existing", [None, {}, {"devices": None}, {"devices": "smieci"}, [], "nie-json-obiekt"])
def test_merge_toleruje_uszkodzony_plik(existing):
    merged, _ = discover.merge_devices(existing, {UUID_SALON: "192.168.1.122"})
    assert merged["devices"] == {UUID_SALON: {"ip_address": "192.168.1.122"}}


def test_merge_zachowuje_nieznane_wlasciwosci_urzadzenia():
    existing = {"devices": {UUID_SALON: {"ip_address": "192.168.1.99", "cos_z_przyszlosci": True}}}
    merged, _ = discover.merge_devices(existing, {UUID_SALON: "192.168.1.122"})
    assert merged["devices"][UUID_SALON] == {"ip_address": "192.168.1.122", "cos_z_przyszlosci": True}


def test_load_devices_file_dla_braku_pliku_i_smieci(tmp_path):
    assert discover.load_devices_file(tmp_path / "nie-ma-mnie.json") == {}
    uszkodzony = tmp_path / "devices.json"
    uszkodzony.write_text("{to nie jest json", encoding="utf-8")
    assert discover.load_devices_file(uszkodzony) == {}
    uszkodzony.write_text("   \n", encoding="utf-8")
    assert discover.load_devices_file(uszkodzony) == {}


def test_write_devices_file_tworzy_katalog_i_poprawny_json(tmp_path):
    target = tmp_path / "bridge" / "config" / "devices.json"
    merged, _ = discover.merge_devices({}, {UUID_SALON: "192.168.1.122"})

    discover.write_devices_file(target, merged)

    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text) == {"devices": {UUID_SALON: {"ip_address": "192.168.1.122"}}}


# --- Najważniejszy test: prawdziwe Persistence.load z klonu mostu ------------


def test_devices_json_wczytuje_sie_prawdziwym_persistence(tmp_path):
    """Gdyby most nie wczytał naszego pliku, cały pomysł na tryb HTTP się sypie.

    `Persistence` z klonu wymaga `dataclasses_json`. Gdy biblioteki nie ma w
    środowisku testowym, test jest POMIJANY — i wtedy faza 2 musi go uruchomić
    w obrazie mostu, gdzie ta zależność jest zainstalowana.
    """
    pytest.importorskip("dataclasses_json", reason="Persistence z klonu mostu wymaga dataclasses_json")

    clone = REPO_ROOT / "meross2mqtt"
    if not (clone / "meross2homie" / "persistence.py").exists():
        pytest.skip("brak klonu meross2mqtt (jest w .gitignore) — nie ma czego weryfikować")
    if str(clone) not in sys.path:
        sys.path.insert(0, str(clone))

    from meross2homie.persistence import Persistence

    discovered = {UUID_SALON: "192.168.1.122", UUID_LISTWA: "192.168.1.177", UUID_TRZECIE: "192.168.1.240"}
    merged, _ = discover.merge_devices(load_json_fixture("devices_json_existing.json"), discovered)

    target = tmp_path / "devices.json"
    discover.write_devices_file(target, merged)

    persistence = Persistence.load(target)

    assert set(persistence.devices) == set(discovered) | {"ffffffffffffffffffffffffffffffff"}
    for uuid, ip in discovered.items():
        assert persistence.devices[uuid].ip_address == ip
    # wpis nieobecny w sieci przetrwał i most dalej zna jego ostatnie IP
    assert persistence.devices["ffffffffffffffffffffffffffffffff"].ip_address == "192.168.1.200"

    # round-trip: to, co most zapisze z powrotem, musi się zgadzać z naszym formatem
    persistence.persist(target)
    assert json.loads(target.read_text(encoding="utf-8")) == merged


# --- Podsieć, ping, .env -----------------------------------------------------


def test_subnet_hosts_dla_24():
    hosts = discover.subnet_hosts("192.168.1.0/24")
    assert len(hosts) == 254
    assert hosts[0] == "192.168.1.1" and hosts[-1] == "192.168.1.254"
    assert "192.168.1.0" not in hosts and "192.168.1.255" not in hosts


def test_subnet_hosts_przyjmuje_adres_hosta_z_maska():
    assert discover.subnet_hosts("192.168.1.114/24") == discover.subnet_hosts("192.168.1.0/24")


def test_subnet_hosts_pojedynczy_adres():
    assert discover.subnet_hosts("192.168.1.122/32") == ["192.168.1.122"]


def test_subnet_hosts_odmawia_zbyt_szerokiego_zakresu():
    """Odmowa musi być NATYCHMIASTOWA, a nie po wyliczeniu 16,7 mln adresów.

    Limit liczymy z `num_addresses`, więc /8 odpada bez materializowania listy. Gdyby
    ktoś wrócił do sprawdzania `len(hosts)`, ten test zacząłby trwać sekundy i zjadać
    ponad gigabajt RAM-u — na Raspberry Pi nie do odróżnienia od zawieszenia.
    """
    started = time.monotonic()
    with pytest.raises(ValueError) as excinfo:
        discover.subnet_hosts("10.0.0.0/8")
    assert time.monotonic() - started < 0.5, "limit sprawdzany po zbudowaniu listy adresów"
    assert "16777214" in str(excinfo.value)
    assert str(discover.MAX_SUBNET_HOSTS) in str(excinfo.value)


def test_subnet_hosts_odrzuca_bzdury():
    with pytest.raises(ValueError):
        discover.subnet_hosts("to-nie-podsiec")


def test_ping_command_ma_wlasciwa_jednostke_na_kazdym_systemie(monkeypatch):
    """macOS liczy `-W` w milisekundach, Linux w sekundach — pomyłka daje ping bez limitu."""
    monkeypatch.setattr(discover.platform, "system", lambda: "Darwin")
    assert discover.ping_command("192.168.1.1", 1.0) == ["ping", "-c", "1", "-n", "-W", "1000", "192.168.1.1"]

    monkeypatch.setattr(discover.platform, "system", lambda: "Linux")
    assert discover.ping_command("192.168.1.1", 1.0) == ["ping", "-c", "1", "-n", "-W", "1", "192.168.1.1"]


def test_is_usable_arp_entry():
    assert discover.is_usable_arp_entry("192.168.1.122", "48:e1:e9:62:e3:a6") is True
    assert discover.is_usable_arp_entry("192.168.1.255", "ff:ff:ff:ff:ff:ff") is False
    assert discover.is_usable_arp_entry("224.0.0.251", "01:00:5e:00:00:fb") is False
    assert discover.is_usable_arp_entry("169.254.250.93", "74:d4:35:5b:5d:92") is False
    assert discover.is_usable_arp_entry("127.0.0.1", "00:11:22:33:44:55") is False


def test_load_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# komentarz\n"
        "MEROSS_KEY=abc123\n"
        'LAN_SUBNET="192.168.1.0/24"\n'
        "PUSTA=\n"
        "bez_znaku_rownosci\n"
        "  TZ = Europe/Warsaw  \n",
        encoding="utf-8",
    )
    values = discover.load_env_file(env)

    assert values["MEROSS_KEY"] == "abc123"
    assert values["LAN_SUBNET"] == "192.168.1.0/24"
    assert values["PUSTA"] == ""
    assert values["TZ"] == "Europe/Warsaw"
    assert "bez_znaku_rownosci" not in values


def test_load_env_file_brak_pliku(tmp_path):
    assert discover.load_env_file(tmp_path / "nie-ma-mnie") == {}


def test_resolve_setting_kolejnosc_zrodel(monkeypatch):
    monkeypatch.delenv("MEROSS_KEY", raising=False)
    assert discover.resolve_setting("z-flagi", "MEROSS_KEY", {"MEROSS_KEY": "z-env-file"}) == "z-flagi"

    monkeypatch.setenv("MEROSS_KEY", "z-otoczenia")
    assert discover.resolve_setting(None, "MEROSS_KEY", {"MEROSS_KEY": "z-env-file"}) == "z-otoczenia"

    monkeypatch.delenv("MEROSS_KEY", raising=False)
    assert discover.resolve_setting(None, "MEROSS_KEY", {"MEROSS_KEY": "z-env-file"}) == "z-env-file"
    assert discover.resolve_setting(None, "MEROSS_KEY", {}) is None


# --- Prezentacja wyniku ------------------------------------------------------


def test_render_table_wyrownuje_kolumny():
    out = discover.render_table(["IP", "UUID"], [["192.168.1.1", "abc"], ["10.0.0.1", "d"]])
    lines = out.splitlines()
    assert lines[0].startswith("IP")
    assert len(lines) == 4
    assert "192.168.1.1  abc" in lines[2]


def test_describe_key_state_dla_innych_bledow():
    probe = discover.Probe(ip="192.168.1.122", uuid=UUID_SALON, error_code=1234)
    assert "1234" in discover.describe_key_state(probe)
    assert discover.describe_key_state(discover.Probe(ip="192.168.1.122", uuid=UUID_SALON)) == "nieznany"
