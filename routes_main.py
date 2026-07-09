"""Today — the hero dashboard: quick-add composer, today's tasks (with subtask
rings), goals rail, captured-today feed. Also hosts POST /capture (the web twin of
the Telegram bot), which delegates to capture.route_capture."""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, render_template, request, jsonify

from web_core import db, today_iso
from db import now_sg
from capture import (route_capture, convert_note_to_task, convert_task_to_note,
                     convert_note_to_journal, convert_task_to_journal)
from routes_tasks import today_tasks, day_score, archive_old_done
from routes_goals import goal_progress
import vault_store

bp = Blueprint("main", __name__)


def captured_today(conn, today: str) -> list:
    """Feed of what was captured today: notes and top-level tasks created today,
    newest first, each showing where it was filed."""
    feed = []
    for n in vault_store.list_notes():
        if (n["created"] or "")[:10] == today:
            tag_str = " ".join("#" + t for t in n["tags"]) if n["tags"] else ""
            feed.append({"source": "NOTE", "kind": "note", "ref": n["slug"],
                         "text": n["title"],
                         "dest": "→ Notes" + (f" · {tag_str}" if tag_str else ""),
                         "ts": n["created"]})
    rows = conn.execute(
        "SELECT id, title, created FROM tasks WHERE parent_id IS NULL AND deleted_at IS NULL "
        "AND substr(created,1,10)=? ORDER BY created DESC", (today,)).fetchall()
    # created is UTC ISO; compare its date loosely (SG date match is close enough for a feed)
    for r in rows:
        feed.append({"source": "TASK", "kind": "task", "ref": r["id"],
                     "text": r["title"], "dest": "→ Tasks", "ts": r["created"]})
    feed.sort(key=lambda x: x["ts"], reverse=True)
    return feed[:8]


@bp.route("/")
def home():
    today = today_iso()
    conn = db()
    archive_old_done(conn)
    tasks = today_tasks(conn)
    score = day_score(tasks)

    goal_rows = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL ORDER BY period, created LIMIT 4"
    ).fetchall()
    goals = []
    for g in goal_rows:
        goals.append({"title": g["title"], "kind": g["kind"],
                      "progress": goal_progress(conn, g)})

    feed = captured_today(conn, today)

    # First-run onboarding: nothing in the DB and nothing in the vault yet.
    any_task = conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
    any_goal = conn.execute("SELECT 1 FROM goals LIMIT 1").fetchone()
    goals_list = [dict(g) for g in conn.execute(
        "SELECT id, title FROM goals WHERE archived_at IS NULL ORDER BY created").fetchall()]
    conn.close()
    first_run = not any_task and not any_goal and not vault_store.list_notes() \
        and not vault_store.list_journal_days()

    journal_empty = not vault_store.read_journal(today)
    now = now_sg()
    return render_template(
        "today.html", active="home",
        weekday=now.strftime("%A"),
        date_str=now.strftime("%-d %b %Y · %H:%M · Asia/Singapore"),
        tasks=tasks, score=score, goals=goals, feed=feed, goals_list=goals_list,
        first_run=first_run, journal_empty=journal_empty)


@bp.route("/capture", methods=["POST"])
def capture():
    """Quick-add: composer, FAB, and (later) the Telegram daemon all land here."""
    text = (request.form.get("text") or "").strip()
    forced = request.form.get("type") or "auto"
    if not text:
        return jsonify({"status": "error", "message": "empty"}), 400
    conn = db()
    result = route_capture(conn, text, source="web", forced=forced)
    conn.close()
    return jsonify({"status": "ok", **result})


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
    conn.close()
    if not result:
        return jsonify({"status": "error", "message": "not found or no-op"}), 400
    return jsonify({"status": "ok", **result})
