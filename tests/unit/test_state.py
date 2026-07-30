"""Parser stanu Homie: strumień zdarzeń wejściowych kontra oczekiwany model.

Każdy przypadek podaje surowe pary (temat, payload) — dokładnie w postaci, w jakiej
przychodzą retainowane wiadomości z brokera — i sprawdza migawkę z CONTRACT.md §3.
"""

import random
import sys
from pathlib import Path

WEBAPP = Path(__file__).resolve().parents[2] / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

from app.state import HomieState, channel_node, split_channel  # noqa: E402

DEV = "2103163085044890845648e1e962e3a6"


def device_attrs(dev=DEV, state="ready", nodes="system,switch,energy,electricity"):
    return [
        (f"homie/{dev}/$homie", "4.0.0"),
        (f"homie/{dev}/$name", "Gniazdko salon"),
        (f"homie/{dev}/$state", state),
        (f"homie/{dev}/$nodes", nodes),
        (f"homie/{dev}/$implementation", "meross2homie"),
        (f"homie/{dev}/$extensions", "org.homie.legacy-firmware:0.1.1:[4.x]"),
        (f"homie/{dev}/$localip", "192.168.1.122"),
        (f"homie/{dev}/$mac", "48:e1:e9:62:e3:a6"),
        (f"homie/{dev}/$fw/name", "mss310"),
        (f"homie/{dev}/$fw/version", "6.1.9"),
    ]


def system_node(dev=DEV):
    return [
        (f"homie/{dev}/system/$name", "System"),
        (f"homie/{dev}/system/$type", "system"),
        (f"homie/{dev}/system/$properties", "reboot"),
        (f"homie/{dev}/system/reboot/$name", "Reboot"),
        (f"homie/{dev}/system/reboot/$datatype", "enum"),
        (f"homie/{dev}/system/reboot/$settable", "true"),
        (f"homie/{dev}/system/reboot/$retained", "false"),
        (f"homie/{dev}/system/reboot/$format", "REQUEST"),
    ]


def switch_node(dev=DEV, channel=0, on=True, settable="true"):
    node = channel_node("switch", channel)
    name = "Switch" if channel == 0 else f"Switch {channel + 1}"
    return [
        (f"homie/{dev}/{node}/$name", name),
        (f"homie/{dev}/{node}/$type", "switch"),
        (f"homie/{dev}/{node}/$properties", "power"),
        (f"homie/{dev}/{node}/power/$name", "Power"),
        (f"homie/{dev}/{node}/power/$datatype", "boolean"),
        (f"homie/{dev}/{node}/power/$settable", settable),
        (f"homie/{dev}/{node}/power/$retained", "true"),
        (f"homie/{dev}/{node}/power", "true" if on else "false"),
    ]


def energy_node(dev=DEV, channel=0, daily="0.42", total="12.5"):
    node = channel_node("energy", channel)
    return [
        (f"homie/{dev}/{node}/$name", "Energy"),
        (f"homie/{dev}/{node}/$type", "stats"),
        (f"homie/{dev}/{node}/$properties", "history,daily,total"),
        (f"homie/{dev}/{node}/daily/$datatype", "float"),
        (f"homie/{dev}/{node}/daily/$unit", "kWh"),
        (f"homie/{dev}/{node}/daily/$settable", "false"),
        (f"homie/{dev}/{node}/daily", daily),
        (f"homie/{dev}/{node}/total", total),
        (
            f"homie/{dev}/{node}/history",
            '[{"timestamp": "2026-07-28T00:00:00", "total_consumption_kwh": 0.42}]',
        ),
    ]


def electricity_node(dev=DEV, channel=0, power="41.2", voltage="233.1", current="0.18"):
    node = channel_node("electricity", channel)
    return [
        (f"homie/{dev}/{node}/$name", "Electricity"),
        (f"homie/{dev}/{node}/$type", "stats"),
        (f"homie/{dev}/{node}/$properties", "voltage,current,power"),
        (f"homie/{dev}/{node}/voltage/$unit", "V"),
        (f"homie/{dev}/{node}/current/$unit", "A"),
        (f"homie/{dev}/{node}/power/$unit", "W"),
        (f"homie/{dev}/{node}/voltage", voltage),
        (f"homie/{dev}/{node}/current", current),
        (f"homie/{dev}/{node}/power", power),
    ]


def mss310_events(dev=DEV, state="ready"):
    return (
        device_attrs(dev, state=state)
        + system_node(dev)
        + switch_node(dev)
        + energy_node(dev)
        + electricity_node(dev)
    )


def parse(events, prefix="homie"):
    state = HomieState(prefix=prefix)
    state.ingest_many(events)
    return state


# ------------------------------------------------------------------ przypadek bazowy


def test_pelny_kafelek_jednokanalowy_zgodny_z_kontraktem():
    snapshot = parse(mss310_events()).snapshot()

    assert snapshot == {
        "devices": [
            {
                "id": DEV,
                "name": "Gniazdko salon",
                "state": "ready",
                "mac": "48:e1:e9:62:e3:a6",
                "ip": "192.168.1.122",
                "model": "mss310",
                "fw": "6.1.9",
                "channels": [
                    {
                        "node": "switch",
                        "name": "Switch",
                        "on": True,
                        "settable": True,
                        "power_w": 41.2,
                        "voltage_v": 233.1,
                        "current_a": 0.18,
                        "energy_today_kwh": 0.42,
                    }
                ],
            }
        ]
    }


def test_wezel_system_nie_jest_kanalem():
    """`system` i `dnd` istnieją zawsze/często, ale kafelków z nich nie robimy."""
    events = mss310_events() + [
        (f"homie/{DEV}/dnd/$type", "settings"),
        (f"homie/{DEV}/dnd/dnd", "false"),
    ]
    channels = parse(events).snapshot()["devices"][0]["channels"]
    assert [c["node"] for c in channels] == ["switch"]


# ---------------------------------------------------------------------- wiele kanałów


def test_listwa_wielogniazdkowa_mapuje_pomiary_na_wlasciwy_kanal():
    events = device_attrs(nodes="system,switch,switch-1,switch-2,energy,energy-1,energy-2") + system_node()
    for channel, (on, power, daily) in enumerate(
        [(True, "41.2", "0.42"), (False, "0.0", "1.10"), (True, "7.5", "0.03")]
    ):
        events += switch_node(channel=channel, on=on)
        events += energy_node(channel=channel, daily=daily)
        events += electricity_node(channel=channel, power=power)

    channels = parse(events).snapshot()["devices"][0]["channels"]

    assert [c["node"] for c in channels] == ["switch", "switch-1", "switch-2"]
    assert [c["on"] for c in channels] == [True, False, True]
    assert [c["power_w"] for c in channels] == [41.2, 0.0, 7.5]
    assert [c["energy_today_kwh"] for c in channels] == [0.42, 1.10, 0.03]
    assert [c["name"] for c in channels] == ["Switch", "Switch 2", "Switch 3"]


def test_kanaly_sortuja_sie_numerycznie_a_nie_leksykalnie():
    events = device_attrs(nodes="switch,switch-2,switch-10")
    for channel in (0, 2, 10):
        events += switch_node(channel=channel)
    channels = parse(events).snapshot()["devices"][0]["channels"]
    assert [c["node"] for c in channels] == ["switch", "switch-2", "switch-10"]


def test_cloud_channel_names_nadpisuja_bridge_name():
    events = device_attrs(nodes="switch,switch-1") + switch_node(channel=0) + switch_node(channel=1)
    cloud = {DEV: {"name": "Biurko", "channel_names": ["Lampa", "Monitor"]}}
    state = HomieState(cloud_devices=cloud)
    state.ingest_many(events)
    channels = state.snapshot()["devices"][0]["channels"]
    assert [c["name"] for c in channels] == ["Lampa", "Monitor"]


def test_split_channel_nie_lapie_nazw_ktore_tylko_wygladaja_jak_kanal():
    assert split_channel("switch") == ("switch", 0)
    assert split_channel("switch-3") == ("switch", 3)
    assert split_channel("switch-abc") == ("switch-abc", 0)
    assert split_channel("switchboard") == ("switchboard", 0)


def test_wezel_o_nazwie_podobnej_do_switch_nie_tworzy_kanalu():
    events = device_attrs(nodes="switchboard") + [
        (f"homie/{DEV}/switchboard/$type", "stats"),
        (f"homie/{DEV}/switchboard/power", "true"),
    ]
    assert parse(events).snapshot()["devices"][0]["channels"] == []


# --------------------------------------------------------------------- przypadki brzegowe


def test_settable_false_oznacza_kanal_tylko_do_podgladu():
    events = device_attrs() + switch_node(settable="false")
    channel = parse(events).snapshot()["devices"][0]["channels"][0]
    assert channel["settable"] is False
    assert channel["on"] is True


def test_brak_atrybutu_settable_domyslnie_sterowalny():
    """Atrybuty potrafią przyjść później niż wartość — panel nie może być wtedy martwy."""
    events = device_attrs() + [(f"homie/{DEV}/switch/power", "true")]
    channel = parse(events).snapshot()["devices"][0]["channels"][0]
    assert channel["settable"] is True


def test_state_lost_z_lwt_nie_gubi_kanalow():
    snapshot = parse(mss310_events(state="lost")).snapshot()
    device = snapshot["devices"][0]
    assert device["state"] == "lost"
    assert device["channels"][0]["on"] is True  # ostatnia znana wartość zostaje


def test_urzadzenie_bez_pomiaru_energii_ma_pomiary_none():
    events = device_attrs(nodes="system,switch") + system_node() + switch_node(on=False)
    device = parse(events).snapshot()["devices"][0]
    channel = device["channels"][0]
    assert channel["on"] is False
    assert channel["power_w"] is None
    assert channel["voltage_v"] is None
    assert channel["current_a"] is None
    assert channel["energy_today_kwh"] is None


def test_brak_atrybutow_urzadzenia_daje_none_a_nie_wyjatek():
    events = [(f"homie/{DEV}/switch/power", "true")]
    device = parse(events).snapshot()["devices"][0]
    assert device["name"] is None
    assert device["state"] is None
    assert device["mac"] is None
    assert device["ip"] is None
    assert device["model"] is None
    assert device["fw"] is None


def test_nodes_zaklada_kafelek_przed_pierwsza_wartoscia():
    """`$nodes` przychodzi retained przed pierwszym odczytem — kafelek ma już być."""
    events = device_attrs(nodes="system,switch,switch-1")
    channels = parse(events).snapshot()["devices"][0]["channels"]
    assert [c["node"] for c in channels] == ["switch", "switch-1"]
    assert [c["on"] for c in channels] == [None, None]


def test_smieciowe_i_nieznane_tematy_nie_wywalaja_parsera():
    events = mss310_events() + [
        (f"homie/{DEV}/dnd/dnd", "false"),  # znany, ale nie-kanałowy węzeł
        (f"homie/{DEV}/nieznanywezel/$type", "cosnowego"),
        (f"homie/{DEV}/nieznanywezel/dziwnaWlasnosc", "42"),
        (f"homie/{DEV}/nieznanywezel/glebiej/niz/kontrakt/przewiduje", "x"),
        (f"homie/{DEV}/switch/power/set", "true"),  # echo naszego własnego polecenia
        (f"homie/{DEV}/electricity/power", "NaN-nie-liczba"),
        (f"homie/{DEV}/energy/daily", "brak-danych"),
        ("homie/", "x"),
        ("homie", "x"),
        (f"homie/{DEV}", "x"),
        (f"homie//switch/power", "true"),
        ("zupelnie/inny/prefiks/power", "true"),
        ("", "x"),
        (None, "x"),
        (f"homie/{DEV}/switch/power", None),
        (f"homie/{DEV}/switch/$name", b"bajty-nie-str"),
    ]
    snapshot = parse(events).snapshot()

    assert [d["id"] for d in snapshot["devices"]] == [DEV]
    channel = snapshot["devices"][0]["channels"][0]
    assert channel["node"] == "switch"
    assert channel["power_w"] is None  # nieparsowalny float, nie wyjątek
    assert channel["energy_today_kwh"] is None
    assert channel["name"] == "Switch"  # payload nie-str zignorowany, stara wartość została


def test_set_nie_zmienia_modelu():
    state = parse(mss310_events())
    assert state.ingest(f"homie/{DEV}/switch/power/set", "false") is False
    assert state.snapshot()["devices"][0]["channels"][0]["on"] is True


# ------------------------------------------------------------------- losowa kolejność


def test_losowa_kolejnosc_retained_daje_ten_sam_model():
    """W praktyce atrybuty potrafią przyjść po wartościach — kolejność nie może mieć znaczenia."""
    events = device_attrs(nodes="system,switch,switch-1,energy,energy-1,electricity,electricity-1")
    events += system_node()
    for channel in (0, 1):
        events += switch_node(channel=channel, on=channel == 0)
        events += energy_node(channel=channel, daily=f"0.{channel}5")
        events += electricity_node(channel=channel, power=f"{channel}1.5")

    expected = parse(events).snapshot()

    for seed in range(40):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        assert parse(shuffled).snapshot() == expected, f"kolejność (seed={seed}) zmieniła model"


def test_wartosc_przed_atrybutami():
    ordered = parse(mss310_events()).snapshot()
    reversed_snapshot = parse(list(reversed(mss310_events()))).snapshot()
    assert reversed_snapshot == ordered


def test_wiele_urzadzen_jest_rozdzielane():
    events = mss310_events(dev="aaa") + mss310_events(dev="bbb", state="lost")
    snapshot = parse(events).snapshot()
    assert [d["id"] for d in snapshot["devices"]] == ["aaa", "bbb"]
    assert [d["state"] for d in snapshot["devices"]] == ["ready", "lost"]


# ----------------------------------------------------------------- flaga zmiany (SSE)


def test_ingest_raportuje_faktyczna_zmiane():
    state = HomieState()
    assert state.ingest(f"homie/{DEV}/switch/power", "true") is True
    assert state.ingest(f"homie/{DEV}/switch/power", "true") is False  # most robi pełny refresh co minutę
    assert state.ingest(f"homie/{DEV}/switch/power", "false") is True
    assert state.ingest(f"homie/{DEV}/$state", "ready") is True
    assert state.ingest(f"homie/{DEV}/$state", "ready") is False


def test_pusty_payload_czysci_retained():
    state = parse(mss310_events())
    assert state.ingest(f"homie/{DEV}/switch/power", "") is True
    assert state.snapshot()["devices"][0]["channels"][0]["on"] is None
    assert state.ingest(f"homie/{DEV}/switch/power", "") is False
    assert state.ingest(f"homie/{DEV}/$state", "") is True
    assert state.snapshot()["devices"][0]["state"] is None


def test_wlasny_prefiks_homie():
    events = [(t.replace("homie/", "dom/", 1), p) for t, p in mss310_events()]
    assert parse(events, prefix="dom").snapshot()["devices"][0]["channels"][0]["on"] is True
    assert parse(events, prefix="homie").snapshot() == {"devices": []}
