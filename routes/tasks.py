"""Tasks: kanban board, subtasks with progress ring, recurrence, planning, drag order.

The pure task helpers (task_dict, subtask_progress, complete_task, archive_old_done,
today_tasks, next_due_date) live in domain/tasks_core; routes/main imports them there —
keeping the Today view and the Tasks board reading the same logic.
"""

from __future__ import annotations

from flask import Blueprint, render_template, request, jsonify

from core.web_core import db, respond, today_iso, task_card_html
from core.db import now_iso
from domain.capture import create_task

# Pure task-domain helpers live in domain/tasks_core (Blueprint-free so the bot
# daemon / proactive AI can import them). Re-exported here for back-compat.
from domain.tasks_core import (
    _WEEKDAYS, _row_to_task, _WEEK_SINCE_SQL, set_task_col, promote_planned_to_week,
    _progress, subtask_progress, task_dict, next_due_date, _respawn_recurring,
    complete_task, _reconcile_parent, _setting_days, archive_old_done, purge_deleted,
    today_tasks, today_task_rows, week_tasks, bump_reschedule, day_score, is_pinned,
)

bp = Blueprint("tasks", __name__)

CATEGORIES = ("content", "business", "personal")
COLUMNS = ("backlog", "week", "done")


# ── routes ────────────────────────────────────────────────────────────────────
@bp.route("/tasks")
def tasks_page():
    conn = db()
    archive_old_done(conn)
    purge_deleted(conn)
    today = today_iso()
    board = {c: [] for c in COLUMNS}
    pinned = []
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL "
        "AND deleted_at IS NULL ORDER BY sort_order, id").fetchall()
    for r in rows:
        t = task_dict(conn, r)
        # On-today tasks (due today / overdue / ☀-planned, sticky) PIN to the top
        # of the week column wherever their col says they live — on the board,
        # today-ness is a place, not a badge. Their stored col is untouched.
        if is_pinned(t, today):
            t["pinned"] = True
            pinned.append(t)
        else:
            board[r["col"]].append(t)
    board["week"] = pinned + board["week"]
    counts = {c: len(board[c]) for c in COLUMNS}
    goals = conn.execute(
        "SELECT id, title FROM goals WHERE archived_at IS NULL AND deleted_at IS NULL ORDER BY created").fetchall()
    conn.close()
    return render_template("tasks.html", board=board, counts=counts,
                           categories=CATEGORIES, goals=goals, active="tasks")


@bp.route("/tasks/new", methods=["POST"])
def task_new():
    f = request.form
    title = (f.get("title") or "").strip()
    if not title:
        return respond(False, "Title required", fallback="/tasks")
    conn = db()
    with conn:
        tid = create_task(
            conn, title,
            col=f.get("col") if f.get("col") in COLUMNS else "backlog",
            priority=f.get("priority") or None,
            category=f.get("category") or None,
            due_date=f.get("due_date") or None,
            planned_on=f.get("planned_on") or None,
            recur_rule=f.get("recur_rule") or None,
            goal_id=int(f["goal_id"]) if f.get("goal_id") else None,
            parent_id=int(f["parent_id"]) if f.get("parent_id") else None,
            media=f.get("media") or None,
        )
    # hand back the rendered card so the caller splices it in place instead of reloading
    card = task_card_html(conn, tid, f.get("surface") or "week")
    conn.close()
    return respond(True, "Task added", to="/tasks",
                   extra={"id": tid, "card_html": card})


@bp.route("/tasks/<int:task_id>/edit", methods=["POST"])
def task_edit(task_id):
    f = request.form
    conn = db()
    # A due_date moved strictly later counts as a postpone (compare against the old value).
    postponed = False
    if "due_date" in f:
        old = conn.execute("SELECT due_date FROM tasks WHERE id=?", (task_id,)).fetchone()
        new_due = f.get("due_date") or None
        if old and old["due_date"] and new_due and new_due > old["due_date"]:
            postponed = True
    fields, params = [], []
    for col in ("title", "priority", "category", "due_date", "recur_rule", "media"):
        if col in f:
            val = f.get(col) or None
            fields.append(f"{col}=?")
            params.append(val)
    new_col = f.get("col") if ("col" in f and f.get("col") in COLUMNS) else None
    if new_col:
        fields.append(_WEEK_SINCE_SQL); params.extend([new_col, today_iso()])
        fields.append("col=?"); params.append(new_col)
    if "goal_id" in f:
        fields.append("goal_id=?"); params.append(int(f["goal_id"]) if f.get("goal_id") else None)
    if not fields:
        conn.close()
        return respond(False, "Nothing to update", fallback="/tasks")
    fields.append("updated=?"); params.append(now_iso())
    params.append(task_id)
    with conn:
        # The editor's column select crossing the done boundary IS completion /
        # un-completion — route through complete_task (completed_at, recurrence
        # respawn) exactly like the checkbox and /tasks/reorder, so no affordance
        # can produce a "done" task that never ran completion logic.
        if new_col:
            cur = conn.execute("SELECT done, parent_id FROM tasks WHERE id=?",
                               (task_id,)).fetchone()
            if cur and cur["parent_id"] is None:
                if new_col == "done" and not cur["done"]:
                    complete_task(conn, task_id, True)
                elif new_col != "done" and cur["done"]:
                    complete_task(conn, task_id, False)
            if new_col == "backlog":
                # Moving to Backlog takes a task off Today (same semantics as the
                # board's drag): a ☀ plan is cleared and counted as a postpone.
                # (A due date can never be cleared implicitly.)
                p = conn.execute("SELECT planned_on, done FROM tasks WHERE id=?",
                                 (task_id,)).fetchone()
                if p and not p["done"] and p["planned_on"] and p["planned_on"] <= today_iso():
                    conn.execute("UPDATE tasks SET planned_on=NULL WHERE id=?", (task_id,))
                    bump_reschedule(conn, task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", params)
        if postponed:
            bump_reschedule(conn, task_id)
    # the re-rendered card carries every structural change (priority/category/recurrence/
    # due chip), so the editor can swap the node instead of reloading the page
    card = task_card_html(conn, task_id, f.get("surface") or "week")
    conn.close()
    return respond(True, "Task updated", to="/tasks", extra={"card_html": card})


@bp.route("/tasks/<int:task_id>/complete", methods=["POST"])
def task_complete(task_id):
    done = request.form.get("done", "1") not in ("0", "false", "")
    conn = db()
    with conn:
        result = complete_task(conn, task_id, done)
    if not result.get("ok"):
        conn.close()
        return respond(False, "Task not found", fallback="/tasks")
    surface = request.form.get("surface") or "week"
    card = task_card_html(conn, task_id, surface)
    # a recurrence respawn is a NEW card the page hasn't got yet — send it too
    respawn_card = (task_card_html(conn, result["respawned"], surface)
                    if result.get("respawned") else "")
    conn.close()
    return jsonify({"status": "ok", "card_html": card, "respawn_html": respawn_card,
                    **{k: v for k, v in result.items() if k != "ok"}})


@bp.route("/tasks/<int:task_id>/plan", methods=["POST"])
def task_plan(task_id):
    """Toggle ☀ planned-for-today. Sticky: a plan from a past day still counts as
    'on today' (it rolled over), so toggling it CLEARS it rather than re-stamping.
    ☀ on a DONE task means "I need to do this (again)" — it reopens the task first
    (un-complete → back to 'week'), never producing a struck-through card pinned
    on Today."""
    conn = db()
    r = conn.execute("SELECT planned_on, done, parent_id FROM tasks WHERE id=?",
                     (task_id,)).fetchone()
    if not r:
        conn.close()
        return respond(False, "Task not found", fallback="/tasks")
    today = today_iso()
    on_today = bool(r["planned_on"]) and r["planned_on"] <= today
    new_val = None if on_today else today
    reopened = False
    with conn:
        if new_val is not None and r["done"] and r["parent_id"] is None:
            complete_task(conn, task_id, False)      # reopen: done → open, col='week'
            reopened = True
        conn.execute("UPDATE tasks SET planned_on=?, updated=? WHERE id=?",
                     (new_val, now_iso(), task_id))
        # "On today" is a SUBSET of "This week" (user rule 2026-07-10): planning
        # promotes a backlog task into the week column, and un-planning leaves it
        # there — not-today does NOT mean not-this-week. Runs on both toggles so
        # legacy planned-while-backlog rows also settle into week on untick.
        promote_planned_to_week(conn, task_id)
        if new_val is None and r["planned_on"]:      # a set plan was cleared → a postpone
            bump_reschedule(conn, task_id)
            # Un-planning surfaces the task at the TOP of its home column (it was
            # just on Today — it shouldn't sink to the bottom of the backlog).
            conn.execute(
                "UPDATE tasks SET sort_order = (SELECT COALESCE(MIN(sort_order), 0) - 1 "
                "FROM tasks WHERE col = (SELECT col FROM tasks WHERE id=?) "
                "AND parent_id IS NULL) WHERE id=?",
                (task_id, task_id))
    card = task_card_html(conn, task_id, request.form.get("surface") or "week")
    conn.close()
    return jsonify({"status": "ok", "planned": bool(new_val), "reopened": reopened,
                    "card_html": card})


@bp.route("/tasks/<int:task_id>/delete", methods=["POST"])
def task_delete(task_id):
    """Soft-delete (undo, not confirmation): stamp deleted_at on the task and its
    subtasks so they drop out of every view but stay restorable for 30 days."""
    conn = db()
    ts = now_iso()
    with conn:
        conn.execute("UPDATE tasks SET deleted_at=?, updated=? WHERE id=? OR parent_id=?",
                     (ts, ts, task_id, task_id))
    conn.close()
    return jsonify({"status": "ok", "id": task_id})


@bp.route("/tasks/<int:task_id>/restore", methods=["POST"])
def task_restore(task_id):
    """Undo a soft-delete: clear deleted_at on the task and its subtasks."""
    conn = db()
    with conn:
        conn.execute("UPDATE tasks SET deleted_at=NULL, updated=? WHERE id=? OR parent_id=?",
                     (now_iso(), task_id, task_id))
    # the restored card goes straight back on the page — an Undo shouldn't cost a reload
    card = task_card_html(conn, task_id, request.form.get("surface") or "week")
    conn.close()
    return jsonify({"status": "ok", "id": task_id, "card_html": card})


@bp.route("/tasks/reorder", methods=["POST"])
def task_reorder():
    """Persist SortableJS order. Body: {col, ids:[...]} — ids in display order.
    Dragging across the done boundary IS completion/un-completion — routed through
    complete_task so recurrence respawn + completed_at happen exactly like the
    checkbox path (previously a drag into Done left done=0)."""
    data = request.get_json(silent=True) or {}
    col = data.get("col")
    ids = data.get("ids") or []
    today = today_iso()
    conn = db()
    with conn:
        for i, tid in enumerate(ids):
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                continue                     # hostile/garbage id — skip, never 500
            if col in COLUMNS:
                r = conn.execute("SELECT done, parent_id FROM tasks WHERE id=?",
                                 (tid,)).fetchone()
                if r and r["parent_id"] is None:
                    if col == "done" and not r["done"]:
                        complete_task(conn, tid, True)
                    elif col != "done" and r["done"]:
                        complete_task(conn, tid, False)
                if col == "backlog":
                    # landing in Backlog takes a task off Today (the board's JS
                    # unplans first — this keeps the raw API equally coherent)
                    conn.execute(
                        "UPDATE tasks SET planned_on=NULL WHERE id=? AND done=0 "
                        "AND planned_on IS NOT NULL AND planned_on <= ?", (tid, today))
                conn.execute(
                    f"UPDATE tasks SET sort_order=?, {_WEEK_SINCE_SQL}, col=?, "
                    "updated=? WHERE id=?",
                    (i, col, today, col, now_iso(), tid))
            else:
                conn.execute("UPDATE tasks SET sort_order=?, updated=? WHERE id=?",
                             (i, now_iso(), tid))
    conn.close()
    return jsonify({"status": "ok"})
