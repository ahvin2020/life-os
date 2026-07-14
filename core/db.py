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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# All "today" calculations are pinned to the configured app timezone, never UTC — a
# task due "today" must mean today where Sam is, and the NAS may run in any
# container TZ. The zone is user-settable (settings key `app_tz`); when unset it
# defaults to the machine's timezone. Everything flows through get_tz()/now_sg()/
# today_iso() so a single setting change moves every date in the app.
DEFAULT_TZ = "Asia/Singapore"          # last-resort fallback if detection fails
_TZ_CACHE = None                        # (name, ZoneInfo); cleared by reload_tz()

# Default is data/app.db next to this file. Set LIFEOS_DB_PATH to override (the
# test suite points it at a throwaway file; the NAS points it at the data volume).
DB_PATH = os.environ.get("LIFEOS_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db"
)

SCHEMA_VERSION = 9


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


def machine_tz_name() -> str:
    """Best-effort IANA name of the host's timezone, for the first-run default.
    Honours the TZ env var, then the /etc/localtime symlink (works on macOS + the
    Docker/NAS Linux), then falls back to DEFAULT_TZ."""
    env = os.environ.get("TZ")
    if env:
        try:
            ZoneInfo(env)
            return env
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
            ZoneInfo(name)                       # validate
            return name
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    return DEFAULT_TZ


def _read_tz_name() -> str:
    """The configured `app_tz` setting, or the machine timezone when unset. Reads
    the DB directly (today_iso/now_sg take no conn); the result is cached."""
    try:
        conn = connect()
        row = conn.execute("SELECT value FROM settings WHERE key='app_tz'").fetchone()
        conn.close()
        if row and row["value"]:
            ZoneInfo(row["value"])               # validate before trusting it
            return row["value"]
    except (sqlite3.Error, ZoneInfoNotFoundError, ValueError, OSError):
        pass
    return machine_tz_name()


def get_tz() -> ZoneInfo:
    """The active app timezone (cached). Call reload_tz() after changing app_tz."""
    global _TZ_CACHE
    if _TZ_CACHE is None:
        name = _read_tz_name()
        try:
            _TZ_CACHE = (name, ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError):
            _TZ_CACHE = (DEFAULT_TZ, ZoneInfo(DEFAULT_TZ))
    return _TZ_CACHE[1]


def reload_tz() -> None:
    """Drop the cached zone so the next call re-reads app_tz (call after a save)."""
    global _TZ_CACHE
    _TZ_CACHE = None


_TIME_FMT_CACHE = None                  # "24h"/"12h"; cleared by reload_time_format()


def time_format() -> str:
    """Preferred clock format for display, '24h' (default) or '12h' (settings key
    `time_format`). Cached like the timezone; call reload_time_format() after a save."""
    global _TIME_FMT_CACHE
    if _TIME_FMT_CACHE is None:
        val = "24h"
        try:
            conn = connect()
            row = conn.execute("SELECT value FROM settings WHERE key='time_format'").fetchone()
            conn.close()
            if row and row["value"] in ("12h", "24h"):
                val = row["value"]
        except sqlite3.Error:
            pass
        _TIME_FMT_CACHE = val
    return _TIME_FMT_CACHE


def reload_time_format() -> None:
    """Drop the cached clock format so the next call re-reads it (call after a save)."""
    global _TIME_FMT_CACHE
    _TIME_FMT_CACHE = None


def now_iso() -> str:
    """UTC timestamp for created/updated audit columns (sortable, unambiguous)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    """Today's date in the app timezone — the app's notion of 'today'."""
    return datetime.now(get_tz()).date().isoformat()


def now_sg() -> datetime:
    """Current wall-clock datetime in the app timezone. (Name kept for callers.)"""
    return datetime.now(get_tz())


def days_ago_iso(days: int) -> str:
    """The date `days` before today (app timezone), ISO 'YYYY-MM-DD' — the shared
    cutoff for archive/purge windows."""
    return (datetime.now(get_tz()).date() - timedelta(days=days)).isoformat()


def get_setting(conn, key, default=None):
    """Single settings accessor shared by web + daemon (key/value TEXT table)."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    with conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def delete_setting(conn, key):
    with conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


def record_correction(conn, kind, detail, cap=50):
    """Log one 'the AI got it wrong and Sam fixed it' signal (a refile, a quick recat/
    rename of a just-created task, a repeated clarify). Feeds the weekly profile-rule
    suggestion. Best-effort — must never break the action it rides on."""
    import json as _json
    try:
        raw = get_setting(conn, "correction_signals", "") or "[]"
        signals = _json.loads(raw)
        if not isinstance(signals, list):
            signals = []
        signals.append({"ts": now_iso(), "kind": str(kind), "detail": str(detail)[:80]})
        set_setting(conn, "correction_signals", _json.dumps(signals[-cap:]))
    except Exception:
        pass


def recent_corrections(conn, days=7):
    """Correction signals from the last `days`, newest last. [] if none/unparseable."""
    import json as _json
    from datetime import datetime, timedelta, timezone
    try:
        signals = _json.loads(get_setting(conn, "correction_signals", "") or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(signals, list):
        return []
    floor = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    return [s for s in signals if isinstance(s, dict) and str(s.get("ts", "")) >= floor]
