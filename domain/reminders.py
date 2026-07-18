"""Timed reminders — the single source for creating/reading them. A reminder is a
one-shot Telegram push at a clock time (fired by scheduler.maybe_fire_reminders);
now also addable + dismissible from the Today dashboard. Storage is the v9 `reminders`
table; fire_at is UTC ISO. Both the bot router and the web routes go through here so the
local→UTC conversion and the display label agree."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

from core.db import now_iso, now_sg, get_tz, time_format
from core.dates import parse_iso_utc, fmt_clock


# ── deterministic parsing ─────────────────────────────────────────────────────
# Reminders are the most common TIMED capture and were the slowest thing in the app: the
# router resolves the clock time itself, which measured 3.4-8.3s. These patterns do it in
# ~5ms. Shared by BOTH surfaces (web route_capture + the Telegram daemon) so the phone and
# the web bar behave identically.
#
# The rule is deliberately strict — fire ONLY when an explicit trigger AND a real CLOCK
# time are both present. A miss costs ~5s and still lands correctly via the router; a false
# positive puts a wrong time (or mangled text) on Sam's actual phone.
_TRIGGER_RE = re.compile(
    r"^\s*(?:please\s+)?(?:add\s+(?:a\s+)?reminder|set\s+(?:a\s+)?reminder"
    r"|reminder|remind\s+me|remind|ping\s+me)\b[:,]?\s*", re.I)
# "in 10 minutes" anywhere, or a LEADING "1 min test" — but never a bare "5 hour" mid-
# sentence ("remind me the 5 hour meeting is tomorrow" is not a 5-hour timer).
_REL_IN_RE = re.compile(r"\bin\s+(\d{1,3})\s*(min|mins|minute|minutes|hr|hrs|hour|hours)\b", re.I)
_REL_LEAD_RE = re.compile(r"^(\d{1,3})\s*(min|mins|minute|minutes|hr|hrs|hour|hours)\b", re.I)
_AMPM_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_H24_RE = re.compile(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\b")
_TOMORROW_RE = re.compile(r"\btomorrow\b|\btmr\w*\b", re.I)
_TODAY_RE = re.compile(r"\b(?:today|tonight)\b", re.I)
_LEAD_JOIN_RE = re.compile(r"^\s*(?:to|about|that|for|:|-)\s*", re.I)
_TAIL_JOIN_RE = re.compile(r"\s*\b(?:to|about|at|in|for)\s*$", re.I)


def parse_reminder(text: str, now=None) -> dict | None:
    """A reminder deterministically parsed out of `text` → {"text", "fire_local"}, else None.

    Returns None (→ let the AI router handle it) unless BOTH are present:
      1. an explicit trigger ("remind me…", "add reminder…", "ping me…"), and
      2. a real CLOCK time — "in N min/hours", "at 3pm", "9:30", "tomorrow 9am".

    A date WITHOUT a clock ("remind me on friday to renew the domain") deliberately returns
    None: that's a task with a due date, not a timed push, and the router already gets that
    split right — this must not break it. `now` is injectable for tests.
    """
    raw = (text or "").strip()
    m = _TRIGGER_RE.match(raw)
    if not m:
        return None
    rest = raw[m.end():].strip()
    if not rest:
        return None
    now = now or now_sg()

    rel = _REL_IN_RE.search(rest) or _REL_LEAD_RE.match(rest)
    if rel:
        n, unit = int(rel.group(1)), rel.group(2).lower()
        if n <= 0:
            return None
        fire = now + (timedelta(minutes=n) if unit.startswith("min") else timedelta(hours=n))
        span = rel.span()
    else:
        cm = _AMPM_RE.search(rest)
        if cm:
            hh, mm = int(cm.group(1)), int(cm.group(2) or 0)
            if not (1 <= hh <= 12 and mm <= 59):
                return None
            hh = hh % 12 + (12 if cm.group(3).lower() == "pm" else 0)
        else:
            cm = _H24_RE.search(rest)
            if not cm:
                return None          # no clock time → the router's call (date-only → task)
            hh, mm = int(cm.group(1)), int(cm.group(2))
            if hh > 23 or mm > 59:
                return None
        day = now.date()
        tomorrow = bool(_TOMORROW_RE.search(rest))
        if tomorrow:
            day += timedelta(days=1)
        fire = datetime.combine(day, time(hh, mm), tzinfo=now.tzinfo)
        if fire <= now and not tomorrow:
            fire += timedelta(days=1)    # "at 9am" once it's past 9am means tomorrow
        span = cm.span()

    body = (rest[:span[0]] + " " + rest[span[1]:])
    body = _TOMORROW_RE.sub(" ", body)
    body = _TODAY_RE.sub(" ", body)
    body = _LEAD_JOIN_RE.sub("", re.sub(r"\s+", " ", body).strip())
    body = _TAIL_JOIN_RE.sub("", body)
    body = re.sub(r"\s+", " ", body).strip(" -:,")
    if not body:
        return None                      # a reminder with no text is the router's to clarify
    return {"text": body, "fire_local": fire.strftime("%Y-%m-%dT%H:%M")}


def to_utc(fire_local: str) -> str:
    """A local wall-clock ISO ('YYYY-MM-DDTHH:MM', naive = app tz) → UTC ISO for storage.
    Raises ValueError if unparseable, so callers can give the user a clear retry."""
    dt = datetime.fromisoformat(fire_local.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_tz())
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reminder_label(fire_utc: str) -> str:
    """UTC ISO → compact local label in the user's clock format (settings `time_format`):
    bare '13:55'/'1:55pm' when it's today, else '6 Jul 15:00'/'6 Jul 3pm'."""
    dt = parse_iso_utc(fire_utc)
    if not dt:
        return ""
    local = dt.astimezone(get_tz())
    clock = fmt_clock(local.strftime("%H:%M"), time_format())
    return clock if local.date() == now_sg().date() else f"{local.strftime('%-d %b')} {clock}"


def create_reminder(conn, text: str, fire_local: str) -> dict:
    """Insert a reminder from a local wall-clock time. Returns {id, text, label, fire_at}."""
    fire_utc = to_utc(fire_local)
    cur = conn.execute(
        "INSERT INTO reminders (text, fire_at, created, fired_at) VALUES (?,?,?,NULL)",
        (text, fire_utc, now_iso()))
    conn.commit()
    return {"id": cur.lastrowid, "text": text,
            "fire_at": fire_utc, "label": reminder_label(fire_utc)}


def restore_reminder(conn, text: str, fire_utc: str, fired_utc: str | None = None) -> dict:
    """Re-insert a dismissed reminder verbatim (undo). fire_utc is already UTC ISO. Preserve
    `fired_utc` so undoing a dismissed-fired reminder comes back fired (not re-armed to alarm
    again on a past time)."""
    cur = conn.execute(
        "INSERT INTO reminders (text, fire_at, created, fired_at) VALUES (?,?,?,?)",
        (text, fire_utc, now_iso(), fired_utc or None))
    conn.commit()
    return {"id": cur.lastrowid, "text": text, "fire_at": fire_utc,
            "fired_at": fired_utc or None, "label": reminder_label(fire_utc)}


# A fired reminder used to vanish the instant it rang — miss the alarm and it was gone with
# no trace (the #1 complaint about ephemeral-alert apps; Apple Reminders/Outlook keep overdue
# ones visible until you clear them). It now LINGERS in a fired/overdue state on the strip so
# you can act on it, and auto-clears after this window as a backstop so they don't pile up.
_FIRED_LINGER = timedelta(hours=24)


def _fired_cutoff() -> str:
    """UTC ISO floor: reminders fired before this drop off the strip (and get purged)."""
    return (datetime.now(timezone.utc) - _FIRED_LINGER).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_reminders(conn) -> list[dict]:
    """Reminders for the Today strip: every unfired one, PLUS any fired in the last 24h kept
    in an overdue state (dismissable, not auto-vanishing). Fired/overdue sort on TOP (most
    recent first — that's the needs-attention-now item you may have missed), then unfired
    soonest-first. Each dict carries `fired` so the template can style it and the browser
    alarm loop can skip an already-rung row instead of re-alarming it."""
    rows = conn.execute(
        "SELECT id, text, fire_at, fired_at FROM reminders "
        "WHERE fired_at IS NULL OR fired_at >= ? ORDER BY fire_at", (_fired_cutoff(),)
    ).fetchall()
    pending, fired = [], []
    for r in rows:
        d = {"id": r["id"], "text": r["text"], "fire_at": r["fire_at"],
             "label": reminder_label(r["fire_at"]), "fired": r["fired_at"] is not None}
        (fired if d["fired"] else pending).append(d)
    fired.reverse()                       # rows were soonest-first → most-recently-fired first
    return fired + pending


def purge_fired_reminders(conn) -> int:
    """Hard-delete reminders fired more than the linger window ago — the strip already hides
    them (see strip_reminders); this just stops the table growing. Returns rows removed."""
    cur = conn.execute(
        "DELETE FROM reminders WHERE fired_at IS NOT NULL AND fired_at < ?", (_fired_cutoff(),))
    conn.commit()
    return cur.rowcount


def fire_reminder(conn, rid: int) -> bool:
    """Mark a reminder delivered by the browser (the no-Telegram firing path). Returns
    True if it was still pending, False if already fired/gone — so a second tab, a page
    reload mid-request, or the daemon beating us to it can't double-notify. Idempotent."""
    cur = conn.execute(
        "UPDATE reminders SET fired_at=? WHERE id=? AND fired_at IS NULL", (now_iso(), rid))
    conn.commit()
    return cur.rowcount > 0


def dismiss_reminder(conn, rid: int) -> dict | None:
    """Clear a reminder off the strip — a pending one (cancel before it fires) OR a lingering
    fired one (acknowledge it). Returns its {text, fire_at, fired_at} for a faithful undo, or
    None if already gone."""
    row = conn.execute(
        "SELECT text, fire_at, fired_at FROM reminders WHERE id=?", (rid,)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()
    return {"text": row["text"], "fire_at": row["fire_at"], "fired_at": row["fired_at"]}
