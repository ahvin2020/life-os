"""Journal: one free-form markdown page per day (vault/journal/YYYY-MM-DD.md).

Today's page is editable in-browser; j:/voice captures append timestamped entries
(via capture.route_capture). Right rail: 'On this day' flashbacks + 'Today so far'.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

from web_core import db, respond, today_iso
import vault_store

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
        "AND parent_id IS NULL ORDER BY completed_at", (today,)).fetchall()
    # imports (#imported notes) are backfill, not something captured today
    cap_count = sum(1 for n in vault_store.list_notes()
                    if (n["created"] or "")[:10] == today and "imported" not in (n["tags"] or []))
    return {
        "completed": [{"title": r["title"]} for r in completed],
        "captures": cap_count,
    }


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
        today_so_far=tsf, active="journal")


@bp.route("/journal/entry", methods=["POST"])
def journal_entry():
    text = (request.form.get("text") or "").strip()
    if not text:
        return respond(False, "Nothing to add", fallback="/journal")
    day = request.form.get("day") or today_iso()
    source = request.form.get("source") or ""
    vault_store.append_journal_entry(day, text, source)
    if _ajax():
        return jsonify({"status": "ok", "day": day})
    return respond(True, "Entry added", to="/journal")


@bp.route("/journal/<day>")
def journal_day(day):
    page = vault_store.read_journal(day)
    if _ajax():
        return jsonify({"status": "ok", "day": day, "raw": page["raw"] if page else ""})
    try:
        pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%A %-d %B %Y")
    except ValueError:
        pretty = day
    return render_template("journal_day.html", day=day, pretty=pretty,
                           page=page, active="journal")


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


def _ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"
