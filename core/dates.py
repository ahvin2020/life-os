"""Pure date-string utilities — no Flask, no DB, no timezone lookups.

These operate on strings that other layers already produced (audit ISO stamps from
`db.now_iso()`, ISO dates, and a caller-supplied `today`). Kept Flask-free so the
daemon, router, and web layer can all share ONE implementation:
- `parse_iso_utc` is the single parser for audit timestamps (was copy-pasted 5×).
- `due_label` is the ONLY due-date vocabulary, full stop. The JS mirror that used to
  live in the static bundle is gone: the server renders every due chip (spliced cards
  included), so there is no second implementation left to drift out of lockstep.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso_utc(value):
    """A UTC audit ISO string (as produced by `db.now_iso`, e.g. '2026-07-14T21:07:03Z')
    → an aware UTC datetime, or None if empty/unparseable."""
    if not value:
        return None
    try:
        return (datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")
                .replace(tzinfo=timezone.utc))
    except Exception:
        return None


def fmt_date(value):
    """ISO date → '9 Jul 2026'. Empty → em dash; unparseable → passthrough."""
    if not value:
        return "—"
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        return f"{d.day} {d.strftime('%b')} {d.year}"
    except Exception:
        return value


def due_label(iso, today):
    """Due date relative to `today` (both ISO 'YYYY-MM-DD'), compact + glanceable:
    today / yesterday / 'Nd over' / tomorrow / weekday inside a week / '13 Jul'
    (the year only when it isn't `today`'s year). Unparseable → `fmt_date`."""
    if not iso:
        return "—"
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
        ref = datetime.strptime(str(today)[:10], "%Y-%m-%d").date()
    except Exception:
        return fmt_date(iso)
    n = (ref - d).days
    if n == 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n > 1:
        return f"{n}d over"
    if n == -1:
        return "tomorrow"
    if n >= -6:
        return d.strftime("%a")
    if d.year == ref.year:
        return f"{d.day} {d.strftime('%b')}"
    return fmt_date(iso)


def fmt_clock(value, fmt: str = "24h"):
    """A stored 'HH:MM' clock time → display form: 24h '13:35' (default) or 12h '1:35pm'
    (dropping ':00'). Pure: the caller passes the preferred format (core.db.time_format()),
    so this leaf stays free of settings/db. The single source for am/pm — web_core's
    _fmt_time filter and the reminder labels both come through here."""
    s = str(value or "")
    if ":" not in s or len(s) < 4 or fmt != "12h":
        return value
    try:
        hh, mm = s[:5].split(":")
        h = int(hh)
        int(mm)
    except ValueError:
        return value
    ap = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{ap}" if mm == "00" else f"{h12}:{mm}{ap}"
