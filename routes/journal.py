"""Journal: one free-form markdown page per day (vault/journal/YYYY-MM-DD.md).

Today's page is editable in-browser; j:/voice captures append timestamped entries
(via capture.route_capture). Right rail: 'On this day' flashbacks + 'Today so far'.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, render_template_string, request, jsonify,
                   send_file, abort)

from core.web_core import db, respond, today_iso, is_ajax
from domain import vault_store

bp = Blueprint("journal", __name__)


def _flashbacks(today: str) -> list:
    """Previous entries from a week / month / year ago, if pages exist."""
    base = datetime.strptime(today, "%Y-%m-%d").date()
    specs = [
        ("a week ago", base - timedelta(days=7)),
        ("a month ago", base - timedelta(days=30)),
        ("a year ago", base - timedelta(days=365)),
    ]
    out = []
    for label, d in specs:
        page = vault_store.read_journal(d.isoformat())
        if page and page["entries"]:
            text = page["entries"][0]["text"]
        elif page and page["raw"].strip():
            text = next((l for l in page["raw"].splitlines()
                         if l.strip() and not l.startswith("#")), "")
        else:
            continue
        out.append({"label": label, "date": d.strftime("%a %-d %b"),
                    "text": text.strip()[:160]})
    return out


def today_so_far(conn, today: str) -> dict:
    """Material for tonight's entry: tasks completed today + captures made today."""
    completed = conn.execute(
        "SELECT title, completed_at FROM tasks WHERE done=1 AND completed_at=? "
        "AND parent_id IS NULL AND deleted_at IS NULL ORDER BY completed_at", (today,)).fetchall()
    # imports (#imported notes) are backfill, not something captured today
    cap_count = sum(1 for n in vault_store.list_notes()
                    if (n["created"] or "")[:10] == today and "imported" not in (n["tags"] or []))
    return {
        "completed": [{"title": r["title"]} for r in completed],
        "captures": cap_count,
    }


def _month_cadence(today: str) -> dict:
    """This month's writing rhythm as a dot per day — habit made visible WITHOUT a
    daily-streak nag (research: streak cadence, not streaks, is what breeds guilt)."""
    import calendar
    base = datetime.strptime(today, "%Y-%m-%d").date()
    written = {d["day"] for d in vault_store.list_journal_days()}
    ndays = calendar.monthrange(base.year, base.month)[1]
    # GitHub-style contribution graph: 7 weekday rows (Sun→Sat) × week columns.
    lead = (base.replace(day=1).weekday() + 1) % 7        # Sunday=0
    cells = [None] * lead
    count = 0
    for dd in range(1, ndays + 1):
        d = base.replace(day=dd)
        iso = d.isoformat()
        w = iso in written
        count += 1 if w else 0
        cells.append({"iso": iso, "day": dd, "written": w, "today": iso == today,
                      "future": dd > base.day, "label": d.strftime("%a %-d %b")})
    while len(cells) % 7:                                  # pad the trailing week
        cells.append(None)
    start_sun = base.replace(day=1) - timedelta(days=lead)
    weeks = [{"tick": (start_sun + timedelta(days=i)).day, "cells": cells[i:i + 7]}
             for i in range(0, len(cells), 7)]
    return {"weeks": weeks, "count": count, "month": base.strftime("%B"),
            "weekdays": ["S", "M", "T", "W", "T", "F", "S"]}


def _annotate_occurrences(page):
    """Give each entry an `idx` — its occurrence among same-HH:MM entries — so the
    per-entry edit/delete API can disambiguate duplicate timestamps."""
    if not page:
        return page
    seen = {}
    for e in page["entries"]:
        e["idx"] = seen.get(e["time"], 0)
        seen[e["time"]] = e["idx"] + 1
    return page


@bp.route("/journal")
def journal_page():
    today = today_iso()
    conn = db()
    page = _annotate_occurrences(vault_store.read_journal(today))
    days = [d for d in vault_store.list_journal_days() if d["day"] != today]
    tsf = today_so_far(conn, today)
    conn.close()
    return render_template(
        "journal.html", today=today,
        today_pretty=datetime.strptime(today, "%Y-%m-%d").strftime("%A %-d %B"),
        page=page, prev_days=days, flashbacks=_flashbacks(today),
        today_so_far=tsf, cadence=_month_cadence(today), active="journal")


def _last_entry_html(page, day: str) -> str:
    """The day's NEWEST entry, rendered from the same _macros.journal_entry the page uses
    (so a spliced entry can't drift from a page-load one). "" when there's nothing yet."""
    if not page or not page["entries"]:
        return ""
    return render_template_string(
        "{% import '_macros.html' as m %}{{ m.journal_entry(e, day) }}",
        e=page["entries"][-1], day=day)


@bp.route("/journal/entry", methods=["POST"])
def journal_entry():
    text = (request.form.get("text") or "").strip()
    media = request.form.get("media") or None
    if not text and not media:
        return respond(False, "Nothing to add", fallback="/journal")
    day = request.form.get("day") or today_iso()
    source = request.form.get("source") or ""
    page = _annotate_occurrences(
        vault_store.append_journal_entry(day, text, source, media=media))
    if is_ajax():
        # entry_html lets the page splice the new entry above the composer in place —
        # occurrences are annotated first so its data-idx matches the file.
        return jsonify({"status": "ok", "day": day,
                        "entry_html": _last_entry_html(page, day)})
    return respond(True, "Entry added", to="/journal")


@bp.route("/journal/<day>/entry/<ts>/audio")
def journal_entry_audio(day, ts):
    """Serve the original recording for ONE voice journal entry, keyed by day + HH:MM
    + occurrence (?i=idx, matching e.idx). Pointer trusted only for its basename (no
    traversal); the file always lives in vault/.audio/. 404 when the entry has none."""
    page = _annotate_occurrences(vault_store.read_journal(day))
    if not page:
        abort(404)
    try:
        idx = max(0, int(request.args.get("i") or 0))
    except (TypeError, ValueError):
        idx = 0
    entry = next((e for e in page["entries"]
                  if e["time"] == ts and e.get("idx") == idx and e.get("audio")), None)
    if not entry:
        abort(404)
    path = os.path.join(vault_store.audio_dir(), os.path.basename(entry["audio"]))
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype="audio/ogg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


def _relative_day(day: str, today: str) -> str:
    """A friendly 'yesterday'/'3 days ago' badge; empty for anything over a week out
    (the full date in the header already says it)."""
    try:
        delta = (datetime.strptime(today, "%Y-%m-%d").date()
                 - datetime.strptime(day, "%Y-%m-%d").date()).days
    except ValueError:
        return ""
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if 2 <= delta <= 6:
        return f"{delta} days ago"
    return ""


@bp.route("/journal/<day>")
def journal_day(day):
    page = _annotate_occurrences(vault_store.read_journal(day))
    if is_ajax():
        return jsonify({"status": "ok", "day": day, "raw": page["raw"] if page else ""})
    try:
        pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%A %-d %B %Y")
    except ValueError:
        pretty = day
    voice_count = sum(1 for e in page["entries"] if e.get("audio")) if page else 0
    return render_template("journal_day.html", day=day, pretty=pretty, page=page,
                           rel=_relative_day(day, today_iso()),
                           voice_count=voice_count, active="journal")


@bp.route("/journal/<day>/save", methods=["POST"])
def journal_save(day):
    raw = request.form.get("raw", "")
    vault_store.save_journal_raw(day, raw)
    return jsonify({"status": "ok", "day": day})


def _entry_index() -> int:
    """Occurrence index within duplicate HH:MM headings (0-based). Safe on garbage."""
    try:
        return max(0, int(request.form.get("idx") or 0))
    except (TypeError, ValueError):
        return 0


@bp.route("/journal/<day>/entry/<ts>/save", methods=["POST"])
def journal_entry_save(day, ts):
    """Rewrite ONE '## HH:MM' entry (today OR a past day), preserving every other
    section byte-for-byte. Returns prev_raw so the client can offer an Undo (restore
    via /journal/<day>/save)."""
    prev = vault_store.read_journal(day)
    prev_raw = prev["raw"] if prev else ""
    page = vault_store.edit_journal_entry(day, ts, _entry_index(),
                                          request.form.get("text", ""))
    if page is None:
        return jsonify({"status": "error", "message": "entry not found"}), 404
    return jsonify({"status": "ok", "day": day, "raw": page["raw"], "prev_raw": prev_raw})


@bp.route("/journal/<day>/entry/<ts>/delete", methods=["POST"])
def journal_entry_delete(day, ts):
    """Remove ONE '## HH:MM' entry, preserving all others. Returns prev_raw for Undo."""
    prev = vault_store.read_journal(day)
    prev_raw = prev["raw"] if prev else ""
    page = vault_store.delete_journal_entry(day, ts, _entry_index())
    if page is None:
        return jsonify({"status": "error", "message": "entry not found"}), 404
    return jsonify({"status": "ok", "day": day, "raw": page["raw"], "prev_raw": prev_raw})
