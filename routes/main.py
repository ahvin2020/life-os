"""Today — the hero dashboard: quick-add composer, today's tasks (with subtask
rings), goals rail, captured-today feed. Also hosts POST /capture (the web twin of
the Telegram bot), which delegates to capture.route_capture."""

from __future__ import annotations

import os
import random

from flask import Blueprint, render_template, request, jsonify, send_file, abort

from core.web_core import db, today_iso, task_card_html
from core.db import now_sg, get_setting, set_setting
from domain.capture import (route_capture, route_deterministic, convert_note_to_task,
                     convert_task_to_note, convert_note_to_journal, convert_task_to_journal)
from domain.tasks_core import (today_tasks, week_tasks, day_score, archive_old_done,
                               task_dict, is_pinned)
from domain.goals_core import goal_progress
from domain import vault_store, reminders

bp = Blueprint("main", __name__)


def _task_splice(conn, task_id):
    """A freshly captured task splices into the panel it actually BELONGS to on Today:
    the hero (`today_html`, today_item macro) when it's on-today (is_pinned — due today /
    overdue / ☀-planned), else the This-week pool (`week_html`, week_item macro). The same
    is_pinned predicate the page buckets with, so a spliced card lands where a reload would
    have put it — not always in the week pool. Returns {} if the task is gone."""
    row = conn.execute("SELECT * FROM tasks WHERE id=? AND deleted_at IS NULL",
                       (task_id,)).fetchone()
    if not row:
        return {}
    if is_pinned(task_dict(conn, row), today_iso()):
        return {"today_html": task_card_html(conn, task_id, "today")}
    return {"week_html": task_card_html(conn, task_id, "week")}


def _touched_card(conn, task_id):
    """Re-render a task the router CHANGED, tagged with the panel it NOW belongs to so
    the composer can MOVE it across Today's panels — a plan promotes it into the hero, an
    unplan drops it into the This-week pool — instead of leaving it stranded where it was.
    `panel`: 'today' (hero, open) | 'week' (This-week pool) | 'done' (completed today →
    animate into Today's "done today" fold) | 'keep' (content-only change — swap in place) |
    'gone' (deleted or no longer on Today → remove the node). html is rendered at the surface
    matching the panel."""
    row = conn.execute("SELECT * FROM tasks WHERE id=? AND deleted_at IS NULL",
                       (task_id,)).fetchone()
    if not row:
        return {"id": task_id, "panel": "gone", "html": ""}
    today = today_iso()
    # A subtask row has no top-level card of its own on Today → swap its parent card in place.
    if row["parent_id"] is not None:
        return {"id": task_id, "panel": "keep",
                "html": task_card_html(conn, task_id, "today")}
    if row["done"]:
        # completed TODAY belongs in the hero's "done today" fold (the client animates it
        # there); completed earlier / archived is off the Today page → drop the node.
        if row["completed_at"] == today and row["archived_at"] is None:
            return {"id": task_id, "panel": "done",
                    "html": task_card_html(conn, task_id, "today")}
        return {"id": task_id, "panel": "gone", "html": ""}
    if is_pinned(task_dict(conn, row), today):
        return {"id": task_id, "panel": "today",
                "html": task_card_html(conn, task_id, "today")}
    if row["col"] == "week" and row["archived_at"] is None:
        return {"id": task_id, "panel": "week",
                "html": task_card_html(conn, task_id, "week")}
    # open but off the Today page now (moved to backlog / archived) → drop the node
    return {"id": task_id, "panel": "gone", "html": ""}

# A warm one-liner after the greeting, rotated per load so opening Today feels like
# arriving somewhere rather than facing a pile. Kept short (one line), time-of-day
# aware, and never cheesy — the joy is in the words, not a loud colour.
GREETING_PROMPTS = {
    "morning": ["Ready to make it a good one?", "What's first today?",
                "Let's make today count.", "Fresh page — where do we start?",
                "Ready when you are.", "What matters most today?", "Let's ease in."],
    "afternoon": ["How's it going so far?", "Still plenty of runway.",
                  "What's next?", "Keep it rolling.", "Halfway there — nice.",
                  "What's left to land today?"],
    "evening": ["Winding down?", "How did today treat you?", "Time to wrap up?",
                "What's left before you rest?", "Ease off when you're ready."],
    "late": ["Burning the midnight oil?", "Don't forget to rest.",
             "Still going strong?", "Late one tonight?"],
}


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
    pending_reminders = reminders.strip_reminders(conn)
    conn.close()
    first_run = not any_task and not any_goal and not vault_store.list_notes() \
        and not vault_store.list_journal_days()

    journal_empty = not vault_store.read_journal(today)
    now = now_sg()

    # Greeting: opening the app should feel like arriving somewhere, not facing a pile.
    hour = now.hour
    if 5 <= hour < 12:
        greeting, band = "Good morning", "morning"
    elif 12 <= hour < 18:
        greeting, band = "Good afternoon", "afternoon"
    elif 18 <= hour < 23:
        greeting, band = "Good evening", "evening"
    else:
        greeting, band = "Still up", "late"
    greeting_prompt = random.choice(GREETING_PROMPTS[band])
    conn2 = db()
    # Greeting name: the user-set display name (Settings) wins, else derive from the profile
    # identity, else nameless — nothing hardcoded, so it's right for whoever runs the app.
    owner_name = get_setting(conn2, "display_name") or vault_store.owner_display_name()
    # Web onboarding: if we don't know who they are (no display name AND no profile identity)
    # and they haven't dismissed it, show a one-line banner nudging them to set up — the web
    # counterpart of the bot's first-run nudge, so a web-first user isn't left nameless.
    show_onboarding = bool(not owner_name and not get_setting(conn2, "web_onboard_dismissed"))
    conn2.close()
    # All-done ending (peak-end): the day's list is empty or fully checked off.
    all_done = bool(tasks) and score["done"] == score["total"] and score["total"] > 0

    return render_template(
        "today.html", active="home",
        weekday=now.strftime("%A"), greeting=greeting,
        greeting_prompt=greeting_prompt, owner_name=owner_name,
        all_done=all_done,
        # short date (no year) for the header's quiet meta line; the current year is
        # implied, and a static render-time clock/tz name would only go stale on screen
        date_short=now.strftime("%-d %b"),
        tasks=tasks, week=week, score=score, goals=goals, goals_list=goals_list,
        reminders=pending_reminders,
        first_run=first_run, journal_empty=journal_empty, show_onboarding=show_onboarding)


@bp.route("/onboarding/name", methods=["POST"])
def onboarding_name():
    """Save the greeting name straight from the Today first-run banner (inline onboarding)."""
    name = " ".join((request.form.get("name") or "").split())[:40]
    conn = db()
    if name:
        set_setting(conn, "display_name", name)
    conn.close()
    return jsonify({"ok": bool(name), "name": name})


@bp.route("/onboarding/dismiss", methods=["POST"])
def onboarding_dismiss():
    """Dismiss the Today first-run profile banner for good (a per-user settings flag)."""
    conn = db()
    set_setting(conn, "web_onboard_dismissed", "1")
    conn.close()
    return jsonify({"ok": True})


@bp.route("/reminders", methods=["POST"])
def reminder_add():
    """Add a timed reminder from the dashboard (the web twin of the bot's set_reminder).
    `at` is a local wall-clock datetime ('YYYY-MM-DDTHH:MM' from a datetime-local input)."""
    text = (request.form.get("text") or "").strip()
    at = (request.form.get("at") or "").strip()
    if not text or not at:
        return jsonify({"status": "error", "message": "need text and time"}), 400
    conn = db()
    try:
        r = reminders.create_reminder(conn, text, at)
    except ValueError:
        conn.close()
        return jsonify({"status": "error", "message": "couldn't read that time"}), 400
    conn.close()
    return jsonify({"status": "ok", **r})


@bp.route("/reminders/<int:rid>/fire", methods=["POST"])
def reminder_fire(rid):
    """Mark a reminder delivered by the open browser tab (the firing path when there's no
    Telegram daemon). `fired` is False if it was already fired, so the tab knows not to
    re-notify — mirrors scheduler.maybe_fire_reminders' fired_at stamp."""
    conn = db()
    fired = reminders.fire_reminder(conn, rid)
    conn.close()
    return jsonify({"status": "ok", "fired": fired})


@bp.route("/reminders/<int:rid>/dismiss", methods=["POST"])
def reminder_dismiss(rid):
    """Cancel a pending reminder; returns its text+fire_at so the toast can undo."""
    conn = db()
    gone = reminders.dismiss_reminder(conn, rid)
    conn.close()
    if not gone:
        return jsonify({"status": "error", "message": "not found"}), 404
    return jsonify({"status": "ok", **gone})


@bp.route("/reminders/restore", methods=["POST"])
def reminder_restore():
    """Undo a dismiss: re-insert the reminder verbatim (fire_at is UTC ISO). fired_at, if
    present, comes back so a dismissed-fired reminder undoes to fired, not re-armed."""
    text = (request.form.get("text") or "").strip()
    fire_at = (request.form.get("fire_at") or "").strip()
    fired_at = (request.form.get("fired_at") or "").strip() or None
    if not text or not fire_at:
        return jsonify({"status": "error", "message": "bad restore"}), 400
    conn = db()
    r = reminders.restore_reminder(conn, text, fire_at, fired_at)
    conn.close()
    return jsonify({"status": "ok", **r})


@bp.route("/capture", methods=["POST"])
def capture():
    """Quick-add: composer, FAB, and (later) the Telegram daemon all land here."""
    text = (request.form.get("text") or "").strip()
    forced = request.form.get("type") or "auto"
    media = (request.form.get("media") or "").strip() or None
    if not text and not media:
        return jsonify({"status": "error", "message": "empty"}), 400
    conn = db()
    # Everything claude-free runs through the SHARED ladder (capture.route_deterministic) —
    # the same one the Telegram daemon uses, so the phone and the web bar can never drift.
    # It owns: explicit captures (URL / "add a task X" / a parseable timed reminder), instant
    # list answers, and document-fact answers.
    result = None
    if forced == "auto" and text and not media:
        result = route_deterministic(conn, text, source="web")
        if result and result.get("kind") == "answer":
            # record it like the daemon does, so an ordinal follow-up ("complete the second
            # one") still sees the list this tier answered
            from ai.router import record_exchange
            record_exchange(conn, text, result["reply"], reply_cap=1200)
            conn.close()
            return jsonify({"status": "ok", "ai": True, "reply": result["reply"],
                            "applied": ["answer"], "reload": False})
    # Nothing deterministic matched → the SAME agentic router the bot uses, so the web bar is
    # as smart as the phone for genuine prose. Guarded by has_claude() so a host WITHOUT the
    # CLI (e.g. the NAS container) files an #unsorted note instantly rather than eating a
    # claude timeout on every capture.
    if result is None and forced == "auto" and text and not media:
        from ai.claude_cli import has_claude
        if has_claude():
            from ai import router
            res = router.route(conn, text, source="web")
            applied = res.get("applied") or []
            extra = {}
            # A brand-new task splices into This-week in place (same card the deterministic
            # path renders), so a simple add never full-reloads the page.
            new_tid = res.get("created_task_id")
            if applied == ["create_task"] and new_tid:
                extra.update(_task_splice(conn, new_tid))
            # Same idea for a new reminder: hand back the row so the composer drops it into
            # the reminders strip in place — no toast, no reload.
            if applied == ["set_reminder"] and res.get("created_reminder"):
                extra["reminder"] = res["created_reminder"]
            # Tasks the router CHANGED (completed / planned / renamed / deleted) come back as
            # re-rendered cards the page swaps in place. An empty html means the row is gone
            # (deleted) → the caller removes that node.
            touched = [t for t in (res.get("touched_task_ids") or []) if t != new_tid]
            if touched:
                extra["cards"] = [_touched_card(conn, t) for t in touched]
            conn.close()
            # Reload only for what still can't be patched: a goal change (the goals rail owns
            # its own markup), a router fallback, or a multi-create we can't splice one-by-one.
            # Task edits ride `cards`; a new task/reminder splices; notes/journal/answers toast.
            unpatchable = {"update_goal_number", "mark_goal_achieved"}
            reload_needed = (res.get("fell_back")
                             or any(a in unpatchable for a in applied)
                             or (applied.count("create_task")
                                 and not (extra.get("week_html") or extra.get("today_html"))))
            return jsonify({"status": "ok", "ai": True, "reply": res.get("reply", ""),
                            "applied": applied, "reload": bool(reload_needed), **extra})
        # no claude → fall through to route_capture below, which files an #unsorted note
        # instantly; the daily triage sweep refiles it later, with no timeout penalty here.
    # A forced kind (photo caption → note), an attachment, or the no-claude fallback still
    # files here. `result` is already set when the shared ladder handled it above.
    if result is None:
        result = route_capture(conn, text, source="web", forced=forced, media=media)
    # A task captured on Today splices into This-week in place (captures land at the top
    # of the week pool) — same macro the home page uses, so the markup matches a reload.
    # Notes/journal have no card on Today; the toast is their confirmation.
    extra = {}
    if result.get("kind") == "task" and result.get("id"):
        extra.update(_task_splice(conn, result["id"]))
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


@bp.route("/calendar")
def calendar():
    """Full calendar as a destination page (the peek stays in Today's 'Up next' card).
    Read-only; the day/week/month grid loads events via /calendar/events after render,
    reusing the exact same fetch + renderer as the Today widget."""
    return render_template("calendar.html", active="calendar")


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
