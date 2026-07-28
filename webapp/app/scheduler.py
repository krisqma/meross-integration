"""Harmonogramy: trasy REST + APScheduler nad tabelą `schedules` w SQLite.

CONTRACT.md §5 zamraża publiczny styk tego modułu na dokładnie cztery nazwy:
`router`, `start_scheduler`, `stop_scheduler`, `job_count`. Wszystko poniżej z
przedrostkiem `_` jest wewnętrzne (część i tak jest wołana z testów jednostkowych).

Źródłem prawdy jest SQLite, nie magazyn APSchedulera: przy starcie czytamy tabelę i
rejestrujemy joby dla wierszy `enabled = 1`, których termin jeszcze nie minął. Dzięki
temu harmonogramy przeżywają restart kontenera i podmianę kodu, a przeterminowany
jednorazowy job nie odpala się z opóźnieniem po tygodniu.

Czas liczymy w strefie z `TZ` (domyślnie `Europe/Warsaw`) — to istotne dla crona
„codziennie 6:30” w dobę zmiany czasu.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

try:  # pragma: no cover - w obrazie zawsze jest 3.12
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from . import db
from .db import Schedule

log = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Warsaw"

#: ISO 1..7 (poniedziałek..niedziela) -> skróty rozumiane przez CronTrigger.
_ISO_TO_CRON_DAY = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}

#: Ile spóźnienia tolerujemy, gdy kontener był chwilę zatrzymany (sekundy).
MISFIRE_GRACE_TIME = 300

router = APIRouter()

_scheduler: Optional[AsyncIOScheduler] = None
_hub: Any = None
_db_path: Optional[str] = None


# --------------------------------------------------------------------- pomocnicze


def _tz() -> ZoneInfo:
    """Strefa z `TZ`; przy śmieciowej wartości spadamy na Europe/Warsaw."""
    name = os.environ.get("TZ") or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("Nieznana strefa czasowa %r, używam %s", name, DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)


def _now() -> datetime:
    return datetime.now(_tz())


def _job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


def _parse_run_at(raw: Optional[str]) -> Optional[datetime]:
    """ISO8601 -> aware datetime w strefie lokalnej (naiwne uznajemy za lokalne)."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz())
    return parsed


def _build_trigger(schedule: Schedule):
    """Trigger APSchedulera dla wiersza; `None`, gdy termin już minął albo dane są złe."""
    tz = _tz()
    if schedule.kind == "cron":
        if schedule.hour is None or schedule.minute is None:
            return None
        days = schedule.iso_days()
        day_of_week = ",".join(_ISO_TO_CRON_DAY[d] for d in days) if days else "*"
        return CronTrigger(
            day_of_week=day_of_week,
            hour=schedule.hour,
            minute=schedule.minute,
            second=0,
            timezone=tz,
        )
    if schedule.kind in ("once", "timer"):
        run_at = _parse_run_at(schedule.run_at)
        if run_at is None:
            return None
        return DateTrigger(run_date=run_at, timezone=tz)
    return None


def _next_run_iso(schedule: Schedule, now: Optional[datetime] = None) -> Optional[str]:
    """`next_run` z kontraktu §4: ISO8601 albo `None` (wyłączone, przeterminowane).

    Liczone wprost z triggera, a nie z uruchomionego APSchedulera — ta sama funkcja
    działa więc w testach bez startowania schedulera.
    """
    if not schedule.enabled:
        return None
    trigger = _build_trigger(schedule)
    if trigger is None:
        return None
    now = now or _now()
    if isinstance(trigger, DateTrigger):
        return trigger.run_date.isoformat() if trigger.run_date > now else None
    fire = trigger.get_next_fire_time(None, now)
    return fire.isoformat() if fire else None


def _to_api(schedule: Schedule) -> dict:
    return schedule.to_api(next_run=_next_run_iso(schedule))


def _payload_for(action: str) -> str:
    """`HomieBooleanProperty` uznaje za prawdę wyłącznie dosłowne `true`."""
    return "true" if action == "on" else "false"


def _opposite(action: str) -> str:
    return "off" if action == "on" else "on"


async def _fire(dev: str, node: str, action: str, schedule_id: Optional[int] = None) -> None:
    """Ciało joba: publikacja na `homie/<dev>/<node>/power/set`."""
    hub = _hub
    if hub is None:
        log.error("Harmonogram %s odpalił bez huba MQTT", schedule_id)
        return
    try:
        await hub.publish_set(dev, node, "power", _payload_for(action))
        log.info("Harmonogram %s: %s/%s -> %s", schedule_id, dev, node, action)
    except Exception:
        # Broker mógł właśnie odpaść — nie wywalamy schedulera z powodu jednego joba.
        log.exception("Harmonogram %s nie mógł opublikować polecenia", schedule_id)


def _register(schedule: Schedule) -> bool:
    """Zakłada job w APSchedulerze. `False`, gdy nie ma czego rejestrować."""
    if _scheduler is None or schedule.id is None or not schedule.enabled:
        return False
    trigger = _build_trigger(schedule)
    if trigger is None:
        return False
    if isinstance(trigger, DateTrigger) and trigger.run_date <= _now():
        # Przeterminowany jednorazowy — wiersz zostaje w bazie jako historia.
        return False
    _scheduler.add_job(
        _fire,
        trigger=trigger,
        args=[schedule.dev, schedule.node, schedule.action, schedule.id],
        id=_job_id(schedule.id),
        name=schedule.label,
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_TIME,
        coalesce=True,
    )
    return True


def _unregister(schedule_id: int) -> bool:
    if _scheduler is None:
        return False
    try:
        _scheduler.remove_job(_job_id(schedule_id))
        return True
    except JobLookupError:
        return False


def _hub_from(request: Request):
    """Hub bierzemy z `app.state` (kontrakt §5), a globalny jest tylko fallbackiem."""
    hub = getattr(request.app.state, "hub", None)
    return hub if hub is not None else _hub


# ------------------------------------------------------------------- cykl życia


async def start_scheduler(hub: Any, db_path: Optional[str] = None) -> None:
    """Tworzy bazę, startuje APScheduler i rejestruje włączone joby z SQLite.

    `db_path` jest dodatkiem ponad kontrakt (domyślnie `DB_PATH` z otoczenia) —
    pozwala testom jednostkowym pracować na bazie w `tmp_path`.
    """
    global _scheduler, _hub, _db_path
    _hub = hub
    _db_path = db_path or db.default_db_path()
    db.init_db(_db_path)

    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=_tz())
    if not _scheduler.running:
        _scheduler.start()

    registered = 0
    for schedule in db.list_schedules(_db_path):
        try:
            if _register(schedule):
                registered += 1
        except Exception:
            log.exception("Nie udało się zarejestrować harmonogramu %s", schedule.id)
    log.info("Harmonogramy: wczytano %s aktywnych zadań z %s", registered, _db_path)


async def stop_scheduler() -> None:
    """Czyste zamknięcie — po nim `job_count()` to 0."""
    global _scheduler, _hub
    scheduler, _scheduler = _scheduler, None
    _hub = None
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)


def job_count() -> int:
    """Liczba zarejestrowanych jobów (dla `/api/health`)."""
    if _scheduler is None or not _scheduler.running:
        return 0
    return len(_scheduler.get_jobs())


# ------------------------------------------------------------------------ modele


class ScheduleIn(BaseModel):
    """Ciało `POST /api/schedules` — harmonogram bez `id`."""

    label: str = Field(min_length=1, max_length=120)
    dev: str = Field(min_length=1)
    node: str = Field(min_length=1)
    action: str
    kind: str
    hour: Optional[int] = Field(default=None, ge=0, le=23)
    minute: Optional[int] = Field(default=None, ge=0, le=59)
    days: Optional[str] = None
    run_at: Optional[str] = None
    enabled: bool = True

    @field_validator("action")
    @classmethod
    def _check_action(cls, value: str) -> str:
        if value not in db.ACTIONS:
            raise ValueError("action musi być 'on' albo 'off'")
        return value

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if value not in db.KINDS:
            raise ValueError("kind musi być 'cron', 'once' albo 'timer'")
        return value

    @field_validator("days")
    @classmethod
    def _check_days(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value.strip() == "":
            return None
        days: List[int] = []
        for chunk in value.split(","):
            chunk = chunk.strip()
            if not chunk.isdigit() or not 1 <= int(chunk) <= 7:
                raise ValueError("days to dni ISO 1..7 po przecinku, np. '1,2,3,4,5'")
            days.append(int(chunk))
        return ",".join(str(d) for d in sorted(set(days)))

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "ScheduleIn":
        if self.kind == "cron":
            if self.hour is None or self.minute is None:
                raise ValueError("kind='cron' wymaga pól hour i minute")
        else:
            if not self.run_at:
                raise ValueError(f"kind='{self.kind}' wymaga pola run_at")
            if _parse_run_at(self.run_at) is None:
                raise ValueError("run_at musi być datą ISO8601")
        return self

    def to_schedule(self) -> Schedule:
        cron = self.kind == "cron"
        return Schedule(
            id=None,
            label=self.label,
            dev=self.dev,
            node=self.node,
            action=self.action,
            kind=self.kind,
            hour=self.hour if cron else None,
            minute=self.minute if cron else None,
            days=self.days if cron else None,
            run_at=None if cron else self.run_at,
            enabled=self.enabled,
        )


class EnabledIn(BaseModel):
    """Ciało `PATCH /api/schedules/{id}`."""

    enabled: bool


class TimerIn(BaseModel):
    """Ciało `POST /api/devices/{dev}/{node}/timer` — „na N minut w stan on”."""

    minutes: int = Field(ge=1, le=7 * 24 * 60)
    on: bool
    label: Optional[str] = Field(default=None, max_length=120)


# ------------------------------------------------------------------------ trasy


@router.get("/api/schedules")
async def list_schedules_route() -> Dict[str, List[dict]]:
    return {"schedules": [_to_api(s) for s in db.list_schedules(_db_path)]}


@router.post("/api/schedules", status_code=201)
async def create_schedule_route(payload: ScheduleIn) -> dict:
    stored = db.insert_schedule(payload.to_schedule(), _db_path)
    _register(stored)
    return _to_api(stored)


@router.patch("/api/schedules/{schedule_id}")
async def patch_schedule_route(schedule_id: int, payload: EnabledIn) -> dict:
    updated = db.set_enabled(schedule_id, payload.enabled, _db_path)
    if updated is None:
        raise HTTPException(status_code=404, detail="Nie ma takiego harmonogramu")
    # Wyłączenie zdejmuje job, włączenie zakłada go z powrotem — bez usuwania wiersza.
    _unregister(schedule_id)
    if updated.enabled:
        _register(updated)
    return _to_api(updated)


@router.delete("/api/schedules/{schedule_id}", status_code=204)
async def delete_schedule_route(schedule_id: int) -> None:
    if not db.delete_schedule(schedule_id, _db_path):
        raise HTTPException(status_code=404, detail="Nie ma takiego harmonogramu")
    _unregister(schedule_id)


@router.post("/api/devices/{dev}/{node}/timer", status_code=201)
async def create_timer_route(dev: str, node: str, payload: TimerIn, request: Request) -> dict:
    """Akcja natychmiast + jednorazowe zadanie odwrotne za `minutes` minut."""
    hub = _hub_from(request)
    if hub is None:
        raise HTTPException(status_code=503, detail="Brak połączenia z brokerem MQTT")

    action = "on" if payload.on else "off"
    try:
        await hub.publish_set(dev, node, "power", _payload_for(action))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Publikacja nieudana: {exc}") from exc

    run_at = _now() + timedelta(minutes=payload.minutes)
    reverse = _opposite(action)
    label = payload.label or (
        f"Minutnik: {'włącz' if payload.on else 'wyłącz'} na {payload.minutes} min"
    )
    stored = db.insert_schedule(
        Schedule(
            id=None,
            label=label,
            dev=dev,
            node=node,
            action=reverse,
            kind="timer",
            run_at=run_at.isoformat(),
            enabled=True,
        ),
        _db_path,
    )
    _register(stored)
    return _to_api(stored)
