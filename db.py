"""SQLite helpers for Life OS.

Lifted from youtube-assistant/executors/invoicing/db.py — connect() keeps the
deliberate WAL + busy_timeout settings for a cloud-synced folder, plus foreign
keys ON and sqlite3.Row. All invoicing/money helpers stripped.

DB location: data/app.db by default. On the NAS the app.db lives OUTSIDE the
synced tree (/volume1/docker/life-os/data/) and is pointed at via LIFEOS_DB_PATH
or the server's --db flag. The NAS is the single writer.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# All "today" calculations are pinned to Singapore, never UTC — a task due "today"
# must mean today where Kelvin is, and the NAS may run in any container TZ.
TZ = ZoneInfo("Asia/Singapore")

# Default is data/app.db next to this file. Set LIFEOS_DB_PATH to override (the
# test suite points it at a throwaway file; the NAS points it at the data volume).
DB_PATH = os.environ.get("LIFEOS_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "app.db"
)

SCHEMA_VERSION = 1


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Wait out transient write contention instead of failing instantly with
    # "database is locked" — the DB lives in a cloud-synced folder and the web UI
    # fires overlapping requests, so a 0ms busy timeout turns a brief lock into a 500.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def now_iso() -> str:
    """UTC timestamp for created/updated audit columns (sortable, unambiguous)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    """Today's date in Asia/Singapore — the app's notion of 'today'."""
    return datetime.now(TZ).date().isoformat()


def now_sg() -> datetime:
    """Current wall-clock datetime in Asia/Singapore."""
    return datetime.now(TZ)
