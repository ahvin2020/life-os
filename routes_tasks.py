"""Tasks: kanban board, subtasks with progress ring, recurrence, planning, drag order.

Also the home of the pure task helpers (task_dict, subtask_progress, complete_task,
archive_old_done, today_tasks, next_due_date) that routes_main imports — keeping the
Today view and the Tasks board reading the same logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

from web_core import db, respond, today_iso
from db import now_iso
from capture import create_task, next_sort_order

bp = Blueprint("tasks", __name__)

CATEGORIES = ("content", "business", "personal")
COLUMNS = ("backlog", "week", "done")
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ── pure helpers (shared with routes_main) ───────────────────────────────────
def _row_to_task(r) -> dict:
    return {
        "id": r["id"], "title": r["title"], "col": r["col"],
        "sort_order": r["sort_order"], "priority": r["priority"],
        "category": r["category"], "due_date": r["due_date"],
        "planned_on": r["planned_on"], "recur_rule": r["recur_rule"],
        "goal_id": r["goal_id"], "parent_id": r["parent_id"],
        "done": bool(r["done"]), "completed_at": r["completed_at"],
        "archived_at": r["archived_at"],
    }


def subtask_progress(conn, task_id) -> dict:
    rows = conn.execute(
        "SELECT done FROM tasks WHERE parent_id = ?", (task_id,)).fetchall()
    total = len(rows)
    done = sum(1 for r in rows if r["done"])
    pct = (done / total * 100) if total else 0
    return {"done": done, "total": total, "pct": pct}


def task_dict(conn, r) -> dict:
    """Full task dict including subtasks + ring progress (for a parent)."""
    t = _row_to_task(r)
    subs = conn.execute(
        "SELECT * FROM tasks WHERE parent_id = ? ORDER BY sort_order, id", (r["id"],)
    ).fetchall()
    t["subtasks"] = [_row_to_task(s) for s in subs]
    prog = subtask_progress(conn, r["id"])
    t["sub_done"], t["sub_total"], t["sub_pct"] = prog["done"], prog["total"], prog["pct"]
    return t


def next_due_date(rule: str, from_date: str = None) -> str:
    """Next occurrence for a recurrence rule. Supports:
    'daily' | 'weekly:<mon..sun>' | 'monthly:<1-28>'. Returns an ISO date."""
    base = datetime.strptime(from_date or today_iso(), "%Y-%m-%d").date()
    rule = (rule or "").strip().lower()
    if rule == "daily":
        return (base + timedelta(days=1)).isoformat()
    if rule.startswith("weekly:"):
        target = rule.split(":", 1)[1].strip()[:3]
        if target in _WEEKDAYS:
            ti = _WEEKDAYS.index(target)
            delta = (ti - base.weekday()) % 7
            delta = delta or 7  # always strictly in the future
            return (base + timedelta(days=delta)).isoformat()
    if rule.startswith("monthly:"):
        try:
            dom = max(1, min(28, int(rule.split(":", 1)[1])))
        except ValueError:
            dom = base.day
        month = base.month + 1
        year = base.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        return base.replace(year=year, month=month, day=dom).isoformat()
    return (base + timedelta(days=1)).isoformat()


def _respawn_recurring(conn, r):
    """Insert a fresh copy of a completed recurring task with the next due date,
    carrying its subtasks over as unchecked."""
    next_due = next_due_date(r["recur_rule"], r["due_date"] or today_iso())
    new_id = create_task(
        conn, r["title"], col=r["col"] if r["col"] != "done" else "week",
        priority=r["priority"], category=r["category"], due_date=next_due,
        recur_rule=r["recur_rule"], goal_id=r["goal_id"])
    subs = conn.execute(
        "SELECT * FROM tasks WHERE parent_id = ? ORDER BY sort_order, id", (r["id"],)
    ).fetchall()
    for s in subs:
        create_task(conn, s["title"], parent_id=new_id)
    return new_id


def complete_task(conn, task_id, done: bool):
    """Mark a task done/undone, respawning recurring tasks and reconciling parents.
    Returns a dict describing side effects (for toast messaging)."""
    r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not r:
        return {"ok": False}
    ts = now_iso()
    result = {"ok": True, "respawned": None, "parent_completed": None}
    if done:
        conn.execute(
            "UPDATE tasks SET done=1, completed_at=?, col=CASE WHEN parent_id IS NULL "
            "THEN 'done' ELSE col END, updated=? WHERE id=?",
            (today_iso(), ts, task_id))
        if r["parent_id"] is None and r["recur_rule"]:
            result["respawned"] = _respawn_recurring(conn, r)
    else:
        conn.execute(
            "UPDATE tasks SET done=0, completed_at=NULL, col=CASE WHEN parent_id IS NULL "
            "AND col='done' THEN 'week' ELSE col END, updated=? WHERE id=?",
            (ts, task_id))
    # Reconcile parent when a subtask changed.
    if r["parent_id"] is not None:
        result["parent_completed"] = _reconcile_parent(conn, r["parent_id"])
    return result


def _reconcile_parent(conn, parent_id):
    """Auto-complete a parent when its last subtask is checked; un-complete it when a
    subtask is unchecked. Returns True (completed), False (un-completed), or None."""
    prog = subtask_progress(conn, parent_id)
    if prog["total"] == 0:
        return None
    p = conn.execute("SELECT * FROM tasks WHERE id=?", (parent_id,)).fetchone()
    ts = now_iso()
    if prog["done"] == prog["total"] and not p["done"]:
        conn.execute(
            "UPDATE tasks SET done=1, completed_at=?, col='done', updated=? WHERE id=?",
            (today_iso(), ts, parent_id))
        if p["recur_rule"]:
            _respawn_recurring(conn, p)
        return True
    if prog["done"] < prog["total"] and p["done"]:
        conn.execute(
            "UPDATE tasks SET done=0, completed_at=NULL, "
            "col=CASE WHEN col='done' THEN 'week' ELSE col END, updated=? WHERE id=?",
            (ts, parent_id))
        return False
    return None


def archive_old_done(conn):
    """Set archived_at on done tasks whose completed_at is older than 7 days.
    Rows stay in the DB (queryable) but drop out of the board."""
    cutoff = (datetime.strptime(today_iso(), "%Y-%m-%d") - timedelta(days=7)).date().isoformat()
    with conn:
        conn.execute(
            "UPDATE tasks SET archived_at=? WHERE done=1 AND archived_at IS NULL "
            "AND parent_id IS NULL AND completed_at IS NOT NULL AND completed_at < ?",
            (now_iso(), cutoff))


def today_tasks(conn) -> list:
    """Tasks that belong on Today: parent tasks (not subtasks, not archived) that are
    due today, overdue-and-open, ☀ planned today, or completed today (dimmed)."""
    today = today_iso()
    rows = conn.execute(
        """SELECT * FROM tasks
             WHERE parent_id IS NULL AND archived_at IS NULL AND (
               due_date = ?
               OR (due_date IS NOT NULL AND due_date < ? AND done = 0)
               OR planned_on = ?
               OR (done = 1 AND completed_at = ?)
             )
           ORDER BY done, sort_order, id""",
        (today, today, today, today)).fetchall()
    return [task_dict(conn, r) for r in rows]


def day_score(tasks) -> dict:
    total = len(tasks)
    done = sum(1 for t in tasks if t["done"])
    pct = (done / total * 100) if total else 0
    return {"done": done, "total": total, "pct": pct}


# ── routes ────────────────────────────────────────────────────────────────────
@bp.route("/tasks")
def tasks_page():
    conn = db()
    archive_old_done(conn)
    board = {c: [] for c in COLUMNS}
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL "
        "ORDER BY sort_order, id").fetchall()
    for r in rows:
        board[r["col"]].append(task_dict(conn, r))
    counts = {c: len(board[c]) for c in COLUMNS}
    goals = conn.execute(
        "SELECT id, title FROM goals WHERE archived_at IS NULL ORDER BY created").fetchall()
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
            recur_rule=f.get("recur_rule") or None,
            goal_id=int(f["goal_id"]) if f.get("goal_id") else None,
            parent_id=int(f["parent_id"]) if f.get("parent_id") else None,
        )
    conn.close()
    return respond(True, "Task added", to="/tasks") if not _wants_json() else \
        jsonify({"status": "ok", "id": tid})


@bp.route("/tasks/<int:task_id>/edit", methods=["POST"])
def task_edit(task_id):
    f = request.form
    conn = db()
    fields, params = [], []
    for col in ("title", "priority", "category", "due_date", "recur_rule"):
        if col in f:
            val = f.get(col) or None
            fields.append(f"{col}=?")
            params.append(val)
    if "col" in f and f.get("col") in COLUMNS:
        fields.append("col=?"); params.append(f.get("col"))
    if "goal_id" in f:
        fields.append("goal_id=?"); params.append(int(f["goal_id"]) if f.get("goal_id") else None)
    if not fields:
        conn.close()
        return respond(False, "Nothing to update", fallback="/tasks")
    fields.append("updated=?"); params.append(now_iso())
    params.append(task_id)
    with conn:
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", params)
    conn.close()
    return respond(True, "Task updated", to="/tasks")


@bp.route("/tasks/<int:task_id>/complete", methods=["POST"])
def task_complete(task_id):
    done = request.form.get("done", "1") not in ("0", "false", "")
    conn = db()
    with conn:
        result = complete_task(conn, task_id, done)
    conn.close()
    if not result.get("ok"):
        return respond(False, "Task not found", fallback="/tasks")
    return jsonify({"status": "ok", **{k: v for k, v in result.items() if k != "ok"}})


@bp.route("/tasks/<int:task_id>/plan", methods=["POST"])
def task_plan(task_id):
    """Toggle ☀ planned-for-today."""
    conn = db()
    r = conn.execute("SELECT planned_on FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not r:
        conn.close()
        return respond(False, "Task not found", fallback="/tasks")
    new_val = None if r["planned_on"] == today_iso() else today_iso()
    with conn:
        conn.execute("UPDATE tasks SET planned_on=?, updated=? WHERE id=?",
                     (new_val, now_iso(), task_id))
    conn.close()
    return jsonify({"status": "ok", "planned": bool(new_val)})


@bp.route("/tasks/<int:task_id>/delete", methods=["POST"])
def task_delete(task_id):
    conn = db()
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.close()
    return respond(True, "Task deleted", to="/tasks")


@bp.route("/tasks/reorder", methods=["POST"])
def task_reorder():
    """Persist SortableJS order. Body: {col, ids:[...]} — ids in display order."""
    data = request.get_json(silent=True) or {}
    col = data.get("col")
    ids = data.get("ids") or []
    conn = db()
    with conn:
        for i, tid in enumerate(ids):
            if col in COLUMNS:
                conn.execute("UPDATE tasks SET sort_order=?, col=?, updated=? WHERE id=?",
                             (i, col, now_iso(), int(tid)))
            else:
                conn.execute("UPDATE tasks SET sort_order=?, updated=? WHERE id=?",
                             (i, now_iso(), int(tid)))
    conn.close()
    return jsonify({"status": "ok"})


def _wants_json():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"
