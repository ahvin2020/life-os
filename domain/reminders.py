"""Timed reminders — the single source for creating/reading them. A reminder is a
one-shot Telegram push at a clock time (fired by scheduler.maybe_fire_reminders);
now also addable + dismissible from the Today dashboard. Storage is the v9 `reminders`
table; fire_at is UTC ISO. Both the bot router and the web routes go through here so the
local→UTC conversion and the display label agree."""

from __future__ import annotations

from datetime import datetime, timezone

from core.db import now_iso, now_sg, get_tz
from core.dates import parse_iso_utc


def to_utc(fire_local: str) -> str:
    """A local wall-clock ISO ('YYYY-MM-DDTHH:MM', naive = app tz) → UTC ISO for storage.
    Raises ValueError if unparseable, so callers can give the user a clear retry."""
    dt = datetime.fromisoformat(fire_local.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_tz())
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reminder_label(fire_utc: str) -> str:
    """UTC ISO → compact local label: bare 'HH:MM' when it's today, else '6 Jul 15:00'."""
    dt = parse_iso_utc(fire_utc)
    if not dt:
        return ""
    local = dt.astimezone(get_tz())
    return local.strftime("%H:%M") if local.date() == now_sg().date() \
        else local.strftime("%-d %b %H:%M")


def create_reminder(conn, text: str, fire_local: str) -> dict:
    """Insert a reminder from a local wall-clock time. Returns {id, text, label, fire_at}."""
    fire_utc = to_utc(fire_local)
    cur = conn.execute(
        "INSERT INTO reminders (text, fire_at, created, fired_at) VALUES (?,?,?,NULL)",
        (text, fire_utc, now_iso()))
    conn.commit()
    return {"id": cur.lastrowid, "text": text,
            "fire_at": fire_utc, "label": reminder_label(fire_utc)}


def restore_reminder(conn, text: str, fire_utc: str) -> dict:
    """Re-insert a dismissed reminder verbatim (undo). fire_utc is already UTC ISO."""
    cur = conn.execute(
        "INSERT INTO reminders (text, fire_at, created, fired_at) VALUES (?,?,?,NULL)",
        (text, fire_utc, now_iso()))
    conn.commit()
    return {"id": cur.lastrowid, "text": text,
            "fire_at": fire_utc, "label": reminder_label(fire_utc)}


def pending_reminders(conn) -> list[dict]:
    """Unfired reminders, soonest first — for the Today strip. Each carries a local label."""
    rows = conn.execute(
        "SELECT id, text, fire_at FROM reminders WHERE fired_at IS NULL ORDER BY fire_at"
    ).fetchall()
    return [{"id": r["id"], "text": r["text"], "fire_at": r["fire_at"],
             "label": reminder_label(r["fire_at"])} for r in rows]


def dismiss_reminder(conn, rid: int) -> dict | None:
    """Cancel a pending reminder. Returns its {text, fire_at} for undo, or None if gone/fired."""
    row = conn.execute(
        "SELECT text, fire_at FROM reminders WHERE id=? AND fired_at IS NULL", (rid,)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()
    return {"text": row["text"], "fire_at": row["fire_at"]}
