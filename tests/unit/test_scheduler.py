"""Harmonogramy: strefy czasowe (w tym zmiana czasu), minutnik, persystencja, restart.

Testy dzielą się na trzy grupy:

* czysta arytmetyka triggerów — bez uruchamiania APSchedulera, z `freezegun`,
* REST + SQLite — FastAPI TestClient nad bazą w `tmp_path`,
* silnik APSchedulera — testy asynchroniczne z faktycznie wystartowanym schedulerem.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

WEBAPP = Path(__file__).resolve().parents[2] / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

from app import db, scheduler  # noqa: E402
from app.db import Schedule  # noqa: E402
from app.main import app  # noqa: E402

DEV = "2103163085044890845648e1e962e3a6"
WARSAW = ZoneInfo("Europe/Warsaw")


class FakeHub:
    """Styk z CONTRACT.md §5 w minimalnej postaci."""

    def __init__(self):
        self.connected = True
        self.published = []

    async def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))

    async def publish_set(self, dev: str, node: str, prop: str, value: str) -> None:
        await self.publish(f"homie/{dev}/{node}/{prop}/set", value)

    def snapshot(self) -> dict:
        return {"devices": []}

    def subscribe_updates(self, cb):
        return lambda: None


def cron_schedule(hour=6, minute=30, days=None, enabled=True, action="on", schedule_id=1):
    return Schedule(
        id=schedule_id,
        label="Codziennie o 6:30",
        dev=DEV,
        node="switch",
        action=action,
        kind="cron",
        hour=hour,
        minute=minute,
        days=days,
        enabled=enabled,
        created_at="2026-01-01T00:00:00+01:00",
    )


def fire_times(schedule, start, count):
    """Kolejne terminy odpalenia triggera, tak jak policzy je APScheduler."""
    trigger = scheduler._build_trigger(schedule)
    out = []
    previous = None
    for _ in range(count):
        nxt = trigger.get_next_fire_time(previous, previous or start)
        assert nxt is not None
        out.append(nxt)
        previous = nxt
    return out


def stamps(times):
    return [t.strftime("%Y-%m-%d %H:%M %z") for t in times]


@pytest.fixture(autouse=True)
def warsaw_tz(monkeypatch):
    """Kontrakt §7: harmonogramy liczą się w strefie z `TZ`."""
    monkeypatch.setenv("TZ", "Europe/Warsaw")


# =========================================================== 1. strefa i zmiana czasu


def utc_stamps(times):
    """Prawdziwe instanty. Odejmowanie dwóch datetime z tym samym `tzinfo` liczy
    różnicę zegara ściennego, więc do sprawdzania realnego odstępu trzeba iść przez UTC."""
    return [t.astimezone(timezone.utc) for t in times]


def test_cron_630_przezywa_przejscie_na_czas_letni():
    """29.03.2026 zegary skaczą 02:00 -> 03:00. „Codziennie 6:30" ma zostać 6:30 LOKALNIE."""
    times = fire_times(cron_schedule(), datetime(2026, 3, 27, 12, 0, tzinfo=WARSAW), 4)
    assert stamps(times) == [
        "2026-03-28 06:30 +0100",
        "2026-03-29 06:30 +0200",  # doba zmiany czasu — nadal 6:30, ale inne UTC
        "2026-03-30 06:30 +0200",
        "2026-03-31 06:30 +0200",
    ]
    # Realny odstęp między 28. a 29. to 23 h, nie 24 — właśnie o to chodzi.
    utc = utc_stamps(times)
    assert utc[1] - utc[0] == timedelta(hours=23)
    assert utc[2] - utc[1] == timedelta(hours=24)


def test_cron_630_przezywa_powrot_na_czas_zimowy():
    """25.10.2026 zegary wracają 03:00 -> 02:00."""
    times = fire_times(cron_schedule(), datetime(2026, 10, 23, 12, 0, tzinfo=WARSAW), 4)
    assert stamps(times) == [
        "2026-10-24 06:30 +0200",
        "2026-10-25 06:30 +0100",
        "2026-10-26 06:30 +0100",
        "2026-10-27 06:30 +0100",
    ]
    utc = utc_stamps(times)
    assert utc[1] - utc[0] == timedelta(hours=25)
    assert utc[2] - utc[1] == timedelta(hours=24)


def test_cron_o_nieistniejacej_godzinie_odpala_raz_i_monotonicznie():
    """2:30 nie istnieje 29.03.2026 (zegar skacze 2:00 -> 3:00).

    APScheduler wylicza wtedy 02:30 ze starym przesunięciem (+01:00), czyli faktycznie
    03:30 czasu letniego — raz, bez duplikatu i bez cofnięcia. To akceptowalne;
    wymagamy tylko monotoniczności i trafiania w 2:30 w doby bez zmiany czasu.
    """
    times = fire_times(
        cron_schedule(hour=2, minute=30), datetime(2026, 3, 27, 12, 0, tzinfo=WARSAW), 4
    )
    utc = utc_stamps(times)
    assert utc == sorted(utc)
    assert len(set(utc)) == 4
    assert times[0].strftime("%Y-%m-%d %H:%M") == "2026-03-28 02:30"
    assert times[-1].strftime("%Y-%m-%d %H:%M") == "2026-03-31 02:30"


def test_cron_o_dwuznacznej_godzinie_odpala_dwa_razy_i_to_jest_znane():
    """2:30 istnieje dwa razy 25.10.2026 i APScheduler odpala job w obu instantach.

    Utrwalamy to jako znane zachowanie, a nie jako pożądane: dla polecenia włącz/wyłącz
    powtórka jest nieszkodliwa (idempotentna publikacja na ten sam temat), więc nie
    komplikujemy silnika. Gdyby kiedyś przeszkadzało — tu jest test do zmiany.
    """
    times = fire_times(
        cron_schedule(hour=2, minute=30), datetime(2026, 10, 24, 12, 0, tzinfo=WARSAW), 4
    )
    assert stamps(times) == [
        "2026-10-25 02:30 +0200",
        "2026-10-25 02:30 +0100",  # ta sama godzina lokalna, godzinę później
        "2026-10-26 02:30 +0100",
        "2026-10-27 02:30 +0100",
    ]
    utc = utc_stamps(times)
    assert utc[1] - utc[0] == timedelta(hours=1)
    assert len(set(utc)) == 4  # różne instanty, więc żadna publikacja nie ginie


def test_cron_tylko_dni_robocze_pomija_weekend():
    times = fire_times(
        cron_schedule(days="1,2,3,4,5"), datetime(2026, 7, 3, 12, 0, tzinfo=WARSAW), 3
    )
    # 3.07.2026 to piątek: kolejne odpalenia to poniedziałek, wtorek, środa.
    assert stamps(times) == [
        "2026-07-06 06:30 +0200",
        "2026-07-07 06:30 +0200",
        "2026-07-08 06:30 +0200",
    ]


def test_cron_tylko_niedziela():
    times = fire_times(cron_schedule(days="7"), datetime(2026, 7, 1, 12, 0, tzinfo=WARSAW), 2)
    assert stamps(times) == ["2026-07-05 06:30 +0200", "2026-07-12 06:30 +0200"]


def test_cron_bez_dni_znaczy_codziennie():
    times = fire_times(cron_schedule(days=None), datetime(2026, 7, 1, 12, 0, tzinfo=WARSAW), 3)
    assert [t.strftime("%Y-%m-%d") for t in times] == ["2026-07-02", "2026-07-03", "2026-07-04"]


@freeze_time("2026-07-01 10:00:00")
def test_next_run_uzywa_strefy_lokalnej():
    assert scheduler._next_run_iso(cron_schedule()) == "2026-07-02T06:30:00+02:00"


@freeze_time("2026-07-01 03:00:00")  # 05:00 w Warszawie, przed 6:30
def test_next_run_tego_samego_dnia_gdy_godzina_jeszcze_nie_minela():
    assert scheduler._next_run_iso(cron_schedule()) == "2026-07-01T06:30:00+02:00"


def test_next_run_wylaczonego_zadania_to_none():
    assert scheduler._next_run_iso(cron_schedule(enabled=False)) is None


@freeze_time("2026-07-01 10:00:00")
def test_next_run_przeterminowanego_once_to_none():
    przeszly = Schedule(
        id=1, label="było", dev=DEV, node="switch", action="off", kind="once",
        run_at="2026-06-30T20:00:00+02:00", enabled=True, created_at="2026-06-01T00:00:00+02:00",
    )
    przyszly = Schedule(
        id=2, label="będzie", dev=DEV, node="switch", action="off", kind="once",
        run_at="2026-07-01T20:00:00+02:00", enabled=True, created_at="2026-06-01T00:00:00+02:00",
    )
    assert scheduler._next_run_iso(przeszly) is None
    assert scheduler._next_run_iso(przyszly) == "2026-07-01T20:00:00+02:00"


def test_naiwna_data_jest_czytana_jako_czas_lokalny():
    """Frontend wysyła `datetime-local`, czyli ISO bez strefy."""
    parsed = scheduler._parse_run_at("2026-07-01T20:00")
    assert parsed == datetime(2026, 7, 1, 20, 0, tzinfo=WARSAW)


def test_zla_strefa_w_TZ_spada_na_warszawe(monkeypatch):
    monkeypatch.setenv("TZ", "Kompletnie/Zmyslona")
    assert scheduler._tz().key == "Europe/Warsaw"


# ================================================================== 2. REST + SQLite


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Baza w tmp_path. Scheduler nie jest uruchomiony — sprawdzamy warstwę REST/SQLite."""
    path = str(tmp_path / "gniazdka.db")
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setattr(scheduler, "_db_path", path)
    db.init_db(path)
    return path


@pytest.fixture
def client(db_path):
    hub = FakeHub()
    app.state.hub = hub
    hub.db_path = db_path
    yield TestClient(app), hub
    app.state.hub = None


def test_round_trip_przez_sqlite(db_path):
    zapisany = db.insert_schedule(
        Schedule(
            id=None, label="Rano", dev=DEV, node="switch-1", action="on", kind="cron",
            hour=6, minute=30, days="1,2,3,4,5", enabled=True,
        ),
        db_path,
    )
    assert zapisany.id == 1
    assert zapisany.created_at  # nadany automatycznie

    (wczytany,) = db.list_schedules(db_path)
    assert wczytany == zapisany
    assert wczytany.enabled is True
    assert wczytany.iso_days() == [1, 2, 3, 4, 5]
    assert wczytany.hour == 6 and wczytany.minute == 30
    assert wczytany.run_at is None


def test_enabled_jest_intem_w_bazie_a_boolem_w_api(db_path):
    zapisany = db.insert_schedule(
        Schedule(id=None, label="x", dev=DEV, node="switch", action="off", kind="cron",
                 hour=22, minute=0, enabled=False),
        db_path,
    )
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT enabled FROM schedules WHERE id = ?", (zapisany.id,)).fetchone()
    assert row["enabled"] == 0
    assert zapisany.to_api()["enabled"] is False


def test_post_get_patch_delete_harmonogramu(client):
    http, _ = client

    res = http.post(
        "/api/schedules",
        json={
            "label": "Lampa wieczorem", "dev": DEV, "node": "switch", "action": "on",
            "kind": "cron", "hour": 18, "minute": 45, "days": "1,2,3,4,5",
        },
    )
    assert res.status_code == 201
    created = res.json()
    assert created["id"] == 1
    assert created["enabled"] is True
    assert created["days"] == "1,2,3,4,5"
    assert created["next_run"] is not None

    listed = http.get("/api/schedules").json()["schedules"]
    assert [s["id"] for s in listed] == [1]
    assert listed[0]["label"] == "Lampa wieczorem"

    patched = http.patch(f"/api/schedules/{created['id']}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["next_run"] is None  # wyłączone nie mają najbliższego uruchomienia

    assert http.delete(f"/api/schedules/{created['id']}").status_code == 204
    assert http.get("/api/schedules").json() == {"schedules": []}


def test_operacje_na_nieistniejacym_harmonogramie_daja_404(client):
    http, _ = client
    assert http.patch("/api/schedules/999", json={"enabled": True}).status_code == 404
    assert http.delete("/api/schedules/999").status_code == 404


def test_walidacja_ciala_harmonogramu(client):
    http, _ = client
    baza = {"label": "x", "dev": DEV, "node": "switch", "action": "on"}

    assert http.post("/api/schedules", json={**baza, "kind": "cron"}).status_code == 422
    assert http.post("/api/schedules", json={**baza, "kind": "cron", "hour": 6}).status_code == 422
    assert http.post("/api/schedules", json={**baza, "kind": "once"}).status_code == 422
    assert http.post(
        "/api/schedules", json={**baza, "kind": "once", "run_at": "wcale-nie-data"}
    ).status_code == 422
    assert http.post(
        "/api/schedules", json={**baza, "kind": "cron", "hour": 25, "minute": 0}
    ).status_code == 422
    assert http.post(
        "/api/schedules", json={**baza, "kind": "cron", "hour": 6, "minute": 30, "days": "0,8"}
    ).status_code == 422
    assert http.post(
        "/api/schedules", json={**baza, "kind": "cron", "hour": 6, "minute": 30, "days": "pn,wt"}
    ).status_code == 422
    assert http.post(
        "/api/schedules", json={**baza, "action": "toggle", "kind": "cron", "hour": 6, "minute": 0}
    ).status_code == 422
    assert http.post(
        "/api/schedules", json={**baza, "kind": "cokolwiek", "run_at": "2026-07-01T20:00"}
    ).status_code == 422
    assert http.get("/api/schedules").json() == {"schedules": []}


def test_dni_sa_normalizowane(client):
    http, _ = client
    created = http.post(
        "/api/schedules",
        json={"label": "x", "dev": DEV, "node": "switch", "action": "on", "kind": "cron",
              "hour": 6, "minute": 30, "days": "5, 1,1, 3"},
    ).json()
    assert created["days"] == "1,3,5"


@freeze_time("2026-07-01 10:00:00")
def test_minutnik_publikuje_teraz_i_planuje_akcje_odwrotna(client):
    http, hub = client

    res = http.post(f"/api/devices/{DEV}/switch/timer", json={"minutes": 45, "on": True})

    assert res.status_code == 201
    # 1. akcja natychmiast — dokładny temat i payload
    assert hub.published == [(f"homie/{DEV}/switch/power/set", "true")]

    # 2. jednorazowe zadanie odwrotne dokładnie na +45 minut
    timer = res.json()
    assert timer["kind"] == "timer"
    assert timer["action"] == "off"
    assert timer["dev"] == DEV and timer["node"] == "switch"
    assert timer["enabled"] is True
    oczekiwany = (datetime(2026, 7, 1, 12, 0, tzinfo=WARSAW) + timedelta(minutes=45)).isoformat()
    assert timer["run_at"] == oczekiwany
    assert timer["next_run"] == oczekiwany
    assert "45" in timer["label"]

    # 3. zadanie jest w bazie, więc przeżyje restart kontenera
    (zapisane,) = db.list_schedules(scheduler._db_path)
    assert zapisane.kind == "timer" and zapisane.action == "off"
    assert zapisane.run_at == oczekiwany


@freeze_time("2026-07-01 10:00:00")
def test_minutnik_wylaczajacy_planuje_wlaczenie(client):
    http, hub = client
    timer = http.post(f"/api/devices/{DEV}/switch/timer", json={"minutes": 10, "on": False}).json()
    assert hub.published == [(f"homie/{DEV}/switch/power/set", "false")]
    assert timer["action"] == "on"


def test_minutnik_waliduje_minuty(client):
    http, hub = client
    assert http.post(f"/api/devices/{DEV}/switch/timer", json={"minutes": 0, "on": True}).status_code == 422
    assert http.post(f"/api/devices/{DEV}/switch/timer", json={"minutes": -5, "on": True}).status_code == 422
    assert http.post(f"/api/devices/{DEV}/switch/timer", json={"on": True}).status_code == 422
    assert hub.published == []
    assert db.list_schedules(scheduler._db_path) == []


def test_minutnik_bez_brokera_nie_tworzy_zadania_widmo(client):
    """Skoro „włącz" nie poszło, nie wolno zostawić w bazie samego „wyłącz"."""
    http, hub = client

    async def wybuch(*args, **kwargs):
        raise RuntimeError("broker padł")

    hub.publish_set = wybuch
    res = http.post(f"/api/devices/{DEV}/switch/timer", json={"minutes": 45, "on": True})
    assert res.status_code == 503
    assert db.list_schedules(scheduler._db_path) == []


# ============================================================= 3. silnik APSchedulera


@pytest.fixture
async def uruchomiony(tmp_path, monkeypatch):
    """Wystartowany scheduler nad bazą w tmp_path; po teście czyste zamknięcie."""
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    path = str(tmp_path / "gniazdka.db")
    hub = FakeHub()
    await scheduler.start_scheduler(hub, db_path=path)
    try:
        yield hub, path
    finally:
        await scheduler.stop_scheduler()


def przyszla_data(minutes=30):
    return (datetime.now(WARSAW) + timedelta(minutes=minutes)).isoformat()


async def test_start_tworzy_baze_i_nie_ma_jobow_na_pustej_bazie(uruchomiony):
    _, path = uruchomiony
    assert Path(path).exists()
    assert scheduler.job_count() == 0


async def test_restart_rejestruje_tylko_zadania_wlaczone(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    path = str(tmp_path / "gniazdka.db")
    db.init_db(path)

    wlaczony = db.insert_schedule(
        Schedule(id=None, label="włączony", dev=DEV, node="switch", action="on", kind="cron",
                 hour=6, minute=30, days="1,2,3,4,5", enabled=True),
        path,
    )
    db.insert_schedule(
        Schedule(id=None, label="wyłączony", dev=DEV, node="switch", action="off", kind="cron",
                 hour=22, minute=0, enabled=False),
        path,
    )
    db.insert_schedule(
        Schedule(id=None, label="przeterminowany", dev=DEV, node="switch", action="off",
                 kind="once", run_at="2020-01-01T10:00:00+01:00", enabled=True),
        path,
    )
    przyszly = db.insert_schedule(
        Schedule(id=None, label="przyszły", dev=DEV, node="switch", action="off", kind="once",
                 run_at=przyszla_data(60), enabled=True),
        path,
    )

    hub = FakeHub()
    await scheduler.start_scheduler(hub, db_path=path)
    try:
        assert scheduler.job_count() == 2
        assert {j.id for j in scheduler._scheduler.get_jobs()} == {
            f"schedule-{wlaczony.id}",
            f"schedule-{przyszly.id}",
        }
        # Wiersze zostają w bazie — SQLite jest źródłem prawdy, nie magazyn APSchedulera.
        assert len(db.list_schedules(path)) == 4
    finally:
        await scheduler.stop_scheduler()

    assert scheduler.job_count() == 0


async def test_usuniecie_zadania_kasuje_job_w_schedulerze(uruchomiony):
    _, path = uruchomiony
    zapisany = db.insert_schedule(
        Schedule(id=None, label="do usunięcia", dev=DEV, node="switch", action="on", kind="cron",
                 hour=7, minute=0, enabled=True),
        path,
    )
    assert scheduler._register(zapisany) is True
    assert scheduler.job_count() == 1

    await scheduler.delete_schedule_route(zapisany.id)

    assert scheduler.job_count() == 0
    assert db.list_schedules(path) == []


async def test_wylaczenie_zdejmuje_job_a_wlaczenie_zaklada_go_ponownie(uruchomiony):
    _, path = uruchomiony
    zapisany = db.insert_schedule(
        Schedule(id=None, label="przełączany", dev=DEV, node="switch", action="on", kind="cron",
                 hour=7, minute=0, enabled=True),
        path,
    )
    scheduler._register(zapisany)
    assert scheduler.job_count() == 1

    wylaczony = await scheduler.patch_schedule_route(
        zapisany.id, scheduler.EnabledIn(enabled=False)
    )
    assert wylaczony["enabled"] is False
    assert wylaczony["next_run"] is None
    assert scheduler.job_count() == 0

    wlaczony = await scheduler.patch_schedule_route(zapisany.id, scheduler.EnabledIn(enabled=True))
    assert wlaczony["enabled"] is True
    assert wlaczony["next_run"] is not None
    assert scheduler.job_count() == 1
    # Wiersz cały czas ten sam — wyłączenie nie usuwa zadania.
    assert len(db.list_schedules(path)) == 1


async def test_przeterminowany_once_nie_jest_rejestrowany(uruchomiony):
    _, path = uruchomiony
    zapisany = db.insert_schedule(
        Schedule(id=None, label="wczoraj", dev=DEV, node="switch", action="off", kind="once",
                 run_at="2020-01-01T10:00:00+01:00", enabled=True),
        path,
    )
    assert scheduler._register(zapisany) is False
    assert scheduler.job_count() == 0


async def test_job_faktycznie_publikuje_polecenie(uruchomiony):
    """Pełna ścieżka: wiersz -> job APSchedulera -> publish_set na dokładny temat."""
    hub, path = uruchomiony
    run_at = (datetime.now(WARSAW) + timedelta(milliseconds=400)).isoformat()
    zapisany = db.insert_schedule(
        Schedule(id=None, label="już", dev=DEV, node="switch-1", action="off", kind="once",
                 run_at=run_at, enabled=True),
        path,
    )
    assert scheduler._register(zapisany) is True

    for _ in range(40):
        if hub.published:
            break
        await asyncio.sleep(0.1)

    assert hub.published == [(f"homie/{DEV}/switch-1/power/set", "false")]


async def test_padniety_broker_nie_wywala_schedulera(uruchomiony):
    hub, _ = uruchomiony

    async def wybuch(*args, **kwargs):
        raise RuntimeError("broker padł")

    hub.publish_set = wybuch
    await scheduler._fire(DEV, "switch", "on", 1)  # nie może podnieść wyjątku
    assert scheduler.job_count() == 0


async def test_job_count_bez_uruchomionego_schedulera():
    await scheduler.stop_scheduler()
    assert scheduler.job_count() == 0
