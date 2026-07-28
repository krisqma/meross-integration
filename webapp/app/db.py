"""SQLite jako źródło prawdy dla harmonogramów (CONTRACT.md §4).

APScheduler jest tylko silnikiem czasu — jego wewnętrzny magazyn jest ulotny i celowo
nie jest używany do persystencji. Joby odtwarzamy z tej tabeli przy każdym starcie,
dzięki czemu przeżywają restart kontenera i podmianę kodu.

Każda operacja otwiera własne połączenie: zapisów jest garść na dzień, a dzięki temu
moduł jest bezpieczny niezależnie od tego, z którego wątku (pętli) go wołamy.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

#: Schemat 1:1 z CONTRACT.md §4 — nie zmieniać bez zmiany kontraktu.
SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY,
    label      TEXT    NOT NULL,
    dev        TEXT    NOT NULL,
    node       TEXT    NOT NULL,
    action     TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    hour       INTEGER,
    minute     INTEGER,
    days       TEXT,
    run_at     TEXT,
    enabled    INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);
"""

KINDS = ("cron", "once", "timer")
ACTIONS = ("on", "off")

_COLUMNS = (
    "id",
    "label",
    "dev",
    "node",
    "action",
    "kind",
    "hour",
    "minute",
    "days",
    "run_at",
    "enabled",
    "created_at",
)


def default_db_path() -> str:
    """`DB_PATH` z otoczenia, z domyślną wartością z kontraktu §7."""
    return os.environ.get("DB_PATH", "/data/gniazdka.db")


@dataclass
class Schedule:
    """Wiersz tabeli `schedules`."""

    id: Optional[int]
    label: str
    dev: str
    node: str
    action: str
    kind: str
    hour: Optional[int] = None
    minute: Optional[int] = None
    days: Optional[str] = None
    run_at: Optional[str] = None
    enabled: bool = True
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Schedule":
        return cls(
            id=row["id"],
            label=row["label"],
            dev=row["dev"],
            node=row["node"],
            action=row["action"],
            kind=row["kind"],
            hour=row["hour"],
            minute=row["minute"],
            days=row["days"],
            run_at=row["run_at"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )

    def iso_days(self) -> List[int]:
        """Dni tygodnia jako liczby ISO 1..7 (1 = poniedziałek)."""
        if not self.days:
            return []
        out = []
        for chunk in self.days.split(","):
            chunk = chunk.strip()
            if chunk.isdigit() and 1 <= int(chunk) <= 7:
                out.append(int(chunk))
        return sorted(set(out))

    def to_api(self, next_run: Optional[str] = None) -> dict:
        """Reprezentacja z kontraktu §4: pola tabeli + wyliczane `next_run`."""
        return {
            "id": self.id,
            "label": self.label,
            "dev": self.dev,
            "node": self.node,
            "action": self.action,
            "kind": self.kind,
            "hour": self.hour,
            "minute": self.minute,
            "days": self.days,
            "run_at": self.run_at,
            "enabled": bool(self.enabled),
            "created_at": self.created_at,
            "next_run": next_run,
        }


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    parent = Path(path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def list_schedules(db_path: Optional[str] = None) -> List[Schedule]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
    return [Schedule.from_row(row) for row in rows]


def get_schedule(schedule_id: int, db_path: Optional[str] = None) -> Optional[Schedule]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return Schedule.from_row(row) if row else None


def insert_schedule(schedule: Schedule, db_path: Optional[str] = None) -> Schedule:
    """Zapisuje harmonogram i zwraca go z nadanym `id` oraz `created_at`."""
    created_at = schedule.created_at or datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO schedules
                (label, dev, node, action, kind, hour, minute, days, run_at, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schedule.label,
                schedule.dev,
                schedule.node,
                schedule.action,
                schedule.kind,
                schedule.hour,
                schedule.minute,
                schedule.days,
                schedule.run_at,
                1 if schedule.enabled else 0,
                created_at,
            ),
        )
        schedule_id = cur.lastrowid
    stored = get_schedule(int(schedule_id), db_path)
    assert stored is not None  # dopiero co wstawiony
    return stored


def set_enabled(
    schedule_id: int, enabled: bool, db_path: Optional[str] = None
) -> Optional[Schedule]:
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, schedule_id),
        )
        if cur.rowcount == 0:
            return None
    return get_schedule(schedule_id, db_path)


def delete_schedule(schedule_id: int, db_path: Optional[str] = None) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return cur.rowcount > 0
