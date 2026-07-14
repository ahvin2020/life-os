"""Pure task-domain helpers shared by the Tasks board, the Today view, and the
non-Flask surfaces (bot router, proactive AI, queries, capture daemon).

This module is deliberately Blueprint-free: it holds the task logic (task_dict,
subtask_progress, complete_task, recurrence respawn, archive/purge, today/week
membership, the week_since staleness clock) so a bot daemon can import it without
pulling in Flask. `routes/tasks.py` re-exports these for back-compat.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core.db import now_iso, days_ago_iso, today_iso, get_setting
from domain.capture import create_task

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
        "archived_at": r["archived_at"], "week_since": r["week_since"],
        "reschedule_count": r["reschedule_count"],
        "media": (r["media"] if "media" in r.keys() else "") or "",
    }


# week_since (schema v5) is the "This week" staleness clock: stamped when a task
# ENTERS the week column, cleared when it leaves, kept while it stays. Every col
# write goes through this fragment so the clock can't drift. Params: (col, today).
_WEEK_SINCE_SQL = ("week_since=CASE WHEN ?='week' THEN "
                   "(CASE WHEN col='week' THEN week_since ELSE ? END) ELSE NULL END")


def set_task_col(conn, task_id, col):
    """Move a task between columns, maintaining week_since. Shared by the task
    editor, the drag endpoint, and the bot router."""
    conn.execute(
        f"UPDATE tasks SET {_WEEK_SINCE_SQL}, col=?, updated=? WHERE id=?",
        (col, today_iso(), col, now_iso(), task_id))


def promote_planned_to_week(conn, task_id):
    """Enforce the on-today ⊆ this-week rule: a planned (or unplanned) backlog PARENT
    task moves into 'week'. No-op for subtasks, done, or non-backlog rows. Call right
    after writing planned_on (re-reads col so a mid-transaction reopen settles too).
    The ONE source of this promotion — used by the task editor's ☀ toggle and the bot
    router's plan/unplan paths."""
    cur = conn.execute("SELECT col, done, parent_id FROM tasks WHERE id=?",
                       (task_id,)).fetchone()
    if cur and cur["parent_id"] is None and not cur["done"] and cur["col"] == "backlog":
        set_task_col(conn, task_id, "week")


def _progress(done, total) -> dict:
    """The shared {done, total, pct} shape used for subtask rings and the day score."""
    return {"done": done, "total": total, "pct": (done / total * 100) if total else 0}


def subtask_progress(conn, task_id) -> dict:
    rows = conn.execute(
        "SELECT done FROM tasks WHERE parent_id = ? AND deleted_at IS NULL", (task_id,)).fetchall()
    return _progress(sum(1 for r in rows if r["done"]), len(rows))


def task_dict(conn, r) -> dict:
    """Full task dict including subtasks + ring progress (for a parent)."""
    t = _row_to_task(r)
    subs = conn.execute(
        "SELECT * FROM tasks WHERE parent_id = ? AND deleted_at IS NULL ORDER BY sort_order, id",
        (r["id"],)).fetchall()
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
    # Base the next occurrence off the LATER of the old due date and today, so
    # completing a long-overdue recurring task lands the respawn in the future
    # instead of another already-overdue copy.
    today = today_iso()
    from_date = r["due_date"] or today
    if from_date < today:
        from_date = today
    next_due = next_due_date(r["recur_rule"], from_date)
    new_id = create_task(
        conn, r["title"], col=r["col"] if r["col"] != "done" else "week",
        priority=r["priority"], category=r["category"], due_date=next_due,
        recur_rule=r["recur_rule"], goal_id=r["goal_id"])
    subs = conn.execute(
        "SELECT * FROM tasks WHERE parent_id = ? AND deleted_at IS NULL ORDER BY sort_order, id",
        (r["id"],)).fetchall()
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
            "THEN 'done' ELSE col END, week_since=CASE WHEN parent_id IS NULL "
            "THEN NULL ELSE week_since END, updated=? WHERE id=?",
            (today_iso(), ts, task_id))
        if r["parent_id"] is None and r["recur_rule"]:
            result["respawned"] = _respawn_recurring(conn, r)
    else:
        # Un-completing sends a top-level task back to 'week' — restart its clock.
        conn.execute(
            "UPDATE tasks SET done=0, completed_at=NULL, week_since=CASE WHEN "
            "parent_id IS NULL AND col='done' THEN ? ELSE week_since END, "
            "col=CASE WHEN parent_id IS NULL "
            "AND col='done' THEN 'week' ELSE col END, updated=? WHERE id=?",
            (today_iso(), ts, task_id))
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
            "UPDATE tasks SET done=1, completed_at=?, col='done', week_since=NULL, "
            "updated=? WHERE id=?",
            (today_iso(), ts, parent_id))
        if p["recur_rule"]:
            _respawn_recurring(conn, p)
        return True
    if prog["done"] < prog["total"] and p["done"]:
        conn.execute(
            "UPDATE tasks SET done=0, completed_at=NULL, "
            "week_since=CASE WHEN col='done' THEN ? ELSE week_since END, "
            "col=CASE WHEN col='done' THEN 'week' ELSE col END, updated=? WHERE id=?",
            (today_iso(), ts, parent_id))
        return False
    return None


def _setting_days(conn, key, default):
    try:
        return max(1, int(get_setting(conn, key, default)))
    except (TypeError, ValueError):
        return default


def archive_old_done(conn):
    """Set archived_at on done tasks whose completed_at is older than
    archive_done_days (default 7). Rows stay in the DB (queryable) but drop out
    of the board."""
    days = _setting_days(conn, "archive_done_days", 7)
    cutoff = days_ago_iso(days)
    with conn:
        conn.execute(
            "UPDATE tasks SET archived_at=? WHERE done=1 AND archived_at IS NULL "
            "AND parent_id IS NULL AND completed_at IS NOT NULL AND completed_at < ?",
            (now_iso(), cutoff))


def purge_deleted(conn):
    """Hard-delete tasks soft-deleted more than purge_deleted_days (default 30)
    ago (undo window elapsed). Same pattern as archive_old_done; subtasks cascade
    via the FK."""
    days = _setting_days(conn, "purge_deleted_days", 30)
    cutoff = days_ago_iso(days)
    with conn:
        conn.execute(
            "DELETE FROM tasks WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))


def today_tasks(conn) -> list:
    """Tasks that belong on Today: parent tasks (not subtasks, not archived) that are
    due today, overdue-and-open, ☀ planned (STICKY: a planned task rolls over day
    after day until done or unplanned — it never quietly retreats), or completed
    today (dimmed)."""
    today = today_iso()
    rows = conn.execute(
        """SELECT * FROM tasks
             WHERE parent_id IS NULL AND archived_at IS NULL AND deleted_at IS NULL AND (
               due_date = ?
               OR (due_date IS NOT NULL AND due_date < ? AND done = 0)
               OR (planned_on IS NOT NULL AND planned_on <= ? AND done = 0)
               OR (done = 1 AND completed_at = ?)
             )
           ORDER BY done, sort_order, id""",
        (today, today, today, today)).fetchall()
    return [task_dict(conn, r) for r in rows]


def today_task_rows(conn, day):
    """Raw open tasks that count as 'today' — due today, overdue, or ☀ planned<=today
    (no done-today, unlike today_tasks). The single SQL source the morning digest and
    the AI brief both read, ordered due-first."""
    return conn.execute(
        """SELECT * FROM tasks
             WHERE parent_id IS NULL AND archived_at IS NULL AND deleted_at IS NULL AND done = 0 AND (
               due_date = ? OR (due_date IS NOT NULL AND due_date < ?)
               OR (planned_on IS NOT NULL AND planned_on <= ?))
           ORDER BY (due_date IS NULL), due_date, sort_order""",
        (day, day, day)).fetchall()


def week_tasks(conn) -> list:
    """Open top-level tasks parked in the 'week' column that are NOT already on Today —
    a view-only pool shown under the Today list so the week's pending work is visible and
    one tap ('Do today') promotes it. Excludes anything due today, overdue, planned today,
    or done (those already belong to Today); this does NOT change Today membership."""
    today = today_iso()
    rows = conn.execute(
        """SELECT * FROM tasks
             WHERE parent_id IS NULL AND archived_at IS NULL AND deleted_at IS NULL
               AND col = 'week' AND done = 0
               AND (due_date IS NULL OR due_date > ?)
               AND (planned_on IS NULL OR planned_on > ?)
           ORDER BY sort_order, id""",
        (today, today)).fetchall()
    return [task_dict(conn, r) for r in rows]


def bump_reschedule(conn, task_id):
    """Increment a task's postpone counter — called when a due_date moves later or a
    previously-set planned_on is cleared. Feeds the backlog-intelligence 'postponed N×'
    signal. Best-effort: never the primary effect of an edit, so it never raises."""
    try:
        conn.execute(
            "UPDATE tasks SET reschedule_count = COALESCE(reschedule_count, 0) + 1 WHERE id=?",
            (task_id,))
    except Exception:
        pass


def day_score(tasks) -> dict:
    return _progress(sum(1 for t in tasks if t["done"]), len(tasks))
