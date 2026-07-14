"""Today — the hero dashboard: quick-add composer, today's tasks (with subtask
rings), goals rail, captured-today feed. Also hosts POST /capture (the web twin of
the Telegram bot), which delegates to capture.route_capture."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, render_template_string, request, jsonify, send_file, abort

from core.web_core import db, today_iso
from core.db import now_sg
from domain.capture import (route_capture, convert_note_to_task, convert_task_to_note,
                     convert_note_to_journal, convert_task_to_journal)
from domain.tasks_core import today_tasks, week_tasks, day_score, archive_old_done, task_dict
from domain.goals_core import goal_progress
from domain import vault_store

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    today = today_iso()
    conn = db()
    archive_old_done(conn)
    tasks = today_tasks(conn)
    week = week_tasks(conn)
    score = day_score(tasks)

    goal_rows = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL AND deleted_at IS NULL ORDER BY period, created LIMIT 4"
    ).fetchall()
    goals = []
    for g in goal_rows:
        goals.append({"title": g["title"], "kind": g["kind"],
                      "progress": goal_progress(conn, g)})

    # First-run onboarding: nothing in the DB and nothing in the vault yet.
    any_task = conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
    any_goal = conn.execute("SELECT 1 FROM goals LIMIT 1").fetchone()
    goals_list = [dict(g) for g in conn.execute(
        "SELECT id, title FROM goals WHERE archived_at IS NULL AND deleted_at IS NULL ORDER BY created").fetchall()]
    conn.close()
    first_run = not any_task and not any_goal and not vault_store.list_notes() \
        and not vault_store.list_journal_days()

    journal_empty = not vault_store.read_journal(today)
    now = now_sg()

    # Greeting: opening the app should feel like arriving somewhere, not facing a pile.
    hour = now.hour
    greeting = ("Good morning" if 5 <= hour < 12 else
                "Good afternoon" if 12 <= hour < 18 else
                "Good evening" if 18 <= hour < 23 else "Still up")
    # Lead with yesterday's wins (Sunsama's ritual): start from a win, not a to-do list.
    conn2 = db()
    sg_mid = datetime.fromisoformat(today).replace(tzinfo=now.tzinfo)
    y_start = (sg_mid - timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    y_end = sg_mid.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    yesterday_done = conn2.execute(
        "SELECT COUNT(*) c FROM tasks WHERE parent_id IS NULL AND deleted_at IS NULL "
        "AND completed_at >= ? AND completed_at < ?", (y_start, y_end)).fetchone()["c"]
    conn2.close()
    # All-done ending (peak-end): the day's list is empty or fully checked off.
    all_done = bool(tasks) and score["done"] == score["total"] and score["total"] > 0

    return render_template(
        "today.html", active="home",
        weekday=now.strftime("%A"), greeting=greeting,
        yesterday_done=yesterday_done, all_done=all_done,
        # date only — a static render-time clock goes stale on screen, and the tz
        # name is settings info, not something to re-read every morning
        date_str=now.strftime("%-d %b %Y"),
        tasks=tasks, week=week, score=score, goals=goals, goals_list=goals_list,
        first_run=first_run, journal_empty=journal_empty)


@bp.route("/capture", methods=["POST"])
def capture():
    """Quick-add: composer, FAB, and (later) the Telegram daemon all land here."""
    text = (request.form.get("text") or "").strip()
    forced = request.form.get("type") or "auto"
    media = (request.form.get("media") or "").strip() or None
    if not text and not media:
        return jsonify({"status": "error", "message": "empty"}), 400
    conn = db()
    result = route_capture(conn, text, source="web", forced=forced, media=media)
    # A task captured on Today splices into This-week in place (captures land at the top
    # of the week pool) — same macro the home page uses, so the markup matches a reload.
    # Notes/journal have no card on Today; the toast is their confirmation.
    extra = {}
    kind = result.get("kind")
    if kind == "task" and result.get("id"):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (result["id"],)).fetchone()
        if row:
            extra["week_html"] = render_template_string(
                "{% import '_macros.html' as m %}{{ m.week_item(t, today) }}",
                t=task_dict(conn, row), today=today_iso())
    conn.close()
    return jsonify({"status": "ok", **result, **extra})


@bp.route("/capture/refile", methods=["POST"])
def capture_refile():
    """Change button on the captured-today feed: move an item between the three
    destinations (task / note / journal). Uses the shared capture helpers so the
    web refile and the Claude triage never duplicate mutation logic."""
    f = request.form
    kind = f.get("kind")          # current kind: 'note' | 'task'
    ref = f.get("ref")            # slug for a note, id for a task
    to = f.get("to")              # target: 'task' | 'note' | 'journal'
    if not kind or not ref or to not in ("task", "note", "journal"):
        return jsonify({"status": "error", "message": "bad refile"}), 400
    conn = db()
    result = None
    if kind == "note":
        if to == "task":
            result = convert_note_to_task(conn, ref)
        elif to == "journal":
            result = convert_note_to_journal(ref)
    elif kind == "task":
        try:
            tid = int(ref)
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"status": "error", "message": "bad task id"}), 400
        if to == "note":
            result = convert_task_to_note(conn, tid)
        elif to == "journal":
            result = convert_task_to_journal(conn, tid)
    if result:
        # A manual refile means the auto-filing was wrong — a signal for the profile loop.
        from core.db import record_correction
        record_correction(conn, "refile", f"{kind}->{to}")
    conn.close()
    if not result:
        return jsonify({"status": "error", "message": "not found or no-op"}), 400
    return jsonify({"status": "ok", **result})


@bp.route("/calendar/events")
def calendar_events():
    """Lazy feed for the Today-page calendar: primary-calendar events between two ISO
    dates. Loaded by JS AFTER the page renders, so a slow/absent Google never blocks
    Today. Read-only — creating events stays with the bot's confirm-first flow."""
    start = (request.args.get("start") or today_iso())[:10]
    end = (request.args.get("end") or start)[:10]
    events, connected = [], False
    try:
        from ai import google_client
        connected = google_client.is_configured()
        if connected:
            events = google_client.calendar_range(start, end)
    except Exception:
        events = []
    return jsonify({"connected": connected, "events": events})


@bp.route("/media/upload", methods=["POST"])
def media_upload():
    """Shared image-attachment upload (notes, journal, tasks). Saves to vault/.media/ and
    returns the pointer; the editor then submits it with the note/task/journal on save."""
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "error", "message": "no file"}), 400
    pointer = vault_store.save_media_file(f)
    if not pointer:
        return jsonify({"status": "error", "message": "unsupported file type"}), 400
    return jsonify({"status": "ok", "pointer": pointer,
                    "url": "/media/" + os.path.basename(pointer)})


@bp.route("/media/<path:name>")
def media_serve(name):
    """Serve an attached file by basename (traversal-guarded; Tailscale is the perimeter).
    `?download=1` forces a download with the original filename; otherwise the browser
    decides (images/PDFs render inline)."""
    path = vault_store.media_file_path(name)
    if not path:
        abort(404)
    if request.args.get("download"):
        return send_file(path, as_attachment=True,
                         download_name=vault_store.media_display_name(name))
    resp = send_file(path)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp
