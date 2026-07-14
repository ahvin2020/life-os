"""Pure date-string utilities — no Flask, no DB, no timezone lookups.

These operate on strings that other layers already produced (audit ISO stamps from
`db.now_iso()`, ISO dates, and a caller-supplied `today`). Kept Flask-free so the
daemon, router, and web layer can all share ONE implementation:
- `parse_iso_utc` is the single parser for audit timestamps (was copy-pasted 5×).
- `due_label` is the single server-side due-date vocabulary; `web/static/core.js`
  `dueLabel()` is its deliberate JS mirror — keep the two in lockstep.
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
