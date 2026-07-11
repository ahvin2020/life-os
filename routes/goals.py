"""Goals: a goal is a TITLE — everything else is optional.

`timeframe` (week|month|quarter|year|by_date|ongoing) supersedes the legacy
`period`/`kind` pair. Progress DERIVES from which fields exist, not from `kind`:
  measure  — a number you tap (current/target, optional unit "460 / 500 subs")
  rollup   — counts linked ◎ tasks (a linked parent counts once, not subtasks)
  milestone— no bar; an "achieved ✓" toggle
  both     — a measure bar with the task fraction as secondary text
"""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

from core.web_core import db, respond, today_iso, is_ajax
from core.db import now_iso

# Pure goal-domain helpers now live in goals_core (Blueprint-free so the bot
# daemon / proactive AI can import them). Re-exported here for back-compat.
from domain.goals_core import (
    TIMEFRAMES, current_period_start, goal_progress, format_goal_progress,
    archive_expired_goals, purge_deleted_goals,
)

bp = Blueprint("goals", __name__)


def _section_labels(today: str) -> dict:
    d = datetime.strptime(today, "%Y-%m-%d").date()
    week_start = d - timedelta(days=d.weekday())
    week_end = week_start + timedelta(days=6)
    quarter = (d.month - 1) // 3 + 1
    return {
        "week": f"This week · {week_start.strftime('%b %-d')} – {week_end.strftime('%-d')}",
        "month": f"This month · {d.strftime('%B')}",
        "quarter": f"This quarter · Q{quarter} {d.year}",
        "year": f"This year · {d.year}",
        "by_date": "By date",
        "ongoing": "Ongoing",
    }


@bp.route("/goals")
def goals_page():
    conn = db()
    archive_expired_goals(conn)
    purge_deleted_goals(conn)
    rows = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL AND deleted_at IS NULL ORDER BY created").fetchall()
    buckets = {k: [] for k in TIMEFRAMES}
    for g in rows:
        tf = g["timeframe"] or g["period"]
        if tf not in buckets:
            tf = "week"
        item = dict(g)
        item["progress"] = goal_progress(conn, g)
        buckets[tf].append(item)
    conn.close()
    buckets["by_date"].sort(key=lambda x: x["end_date"] or "9999-12-31")
    labels = _section_labels(today_iso())
    sections = [{"key": k, "label": labels[k], "goals": buckets[k]}
                for k in TIMEFRAMES if buckets[k]]
    return render_template("goals.html", sections=sections, active="goals")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@bp.route("/goals/new", methods=["POST"])
def goal_new():
    """Create a goal. Only `title` is required; timeframe/end_date/unit/current/target
    are all optional. `period`/`kind` are still written for non-destructive back-compat."""
    f = request.form
    title = (f.get("title") or "").strip()
    if not title:
        return respond(False, "Title required", fallback="/goals")
    timeframe = f.get("timeframe") if f.get("timeframe") in TIMEFRAMES else "week"
    end_date = (f.get("end_date") or "").strip() or None
    if timeframe != "by_date":
        end_date = None
    unit = (f.get("unit") or "").strip() or None
    target = _num(f.get("target"))
    current = _num(f.get("current")) or 0
    period = "week" if timeframe == "week" else "month"        # legacy CHECK-compatible
    kind = "number" if (target is not None or unit) else "rollup"
    conn = db()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, "
            "current_num, timeframe, end_date, unit, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (title, period, current_period_start(timeframe), kind, target, current,
             timeframe, end_date, unit, now_iso()))
    gid = cur.lastrowid
    conn.close()
    if is_ajax():
        return jsonify({"status": "ok", "id": gid})
    return respond(True, "Goal created", to="/goals")


@bp.route("/goals/<int:goal_id>/update", methods=["POST"])
def goal_update(goal_id):
    """Update the manual number on a measurable goal (tap-to-edit)."""
    try:
        val = float(request.form.get("current"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "bad number"}), 400
    conn = db()
    with conn:
        conn.execute("UPDATE goals SET current_num=? WHERE id=?", (val, goal_id))
    conn.close()
    return jsonify({"status": "ok", "current": val})


@bp.route("/goals/<int:goal_id>/achieve", methods=["POST"])
def goal_achieve(goal_id):
    """Toggle a milestone goal's completion (achieved_at null = open, set = done)."""
    conn = db()
    with conn:
        row = conn.execute("SELECT achieved_at FROM goals WHERE id=?", (goal_id,)).fetchone()
        if row is None:
            conn.close()
            return jsonify({"status": "error", "message": "not found"}), 404
        new = None if row["achieved_at"] else now_iso()
        conn.execute("UPDATE goals SET achieved_at=? WHERE id=?", (new, goal_id))
    conn.close()
    return jsonify({"status": "ok", "achieved": new is not None})


@bp.route("/goals/<int:goal_id>/delete", methods=["POST"])
def goal_delete(goal_id):
    """Soft-delete (undo, not confirmation — parity with tasks/notes): stamp
    deleted_at so the goal drops out of every view but stays restorable. Task links
    survive (goal_id's ON DELETE SET NULL never fires). Purged after 30 days."""
    conn = db()
    with conn:
        conn.execute("UPDATE goals SET deleted_at=? WHERE id=?", (now_iso(), goal_id))
    conn.close()
    return jsonify({"status": "ok", "id": goal_id})


@bp.route("/goals/<int:goal_id>/restore", methods=["POST"])
def goal_restore(goal_id):
    """Undo a goal soft-delete."""
    conn = db()
    with conn:
        conn.execute("UPDATE goals SET deleted_at=NULL WHERE id=?", (goal_id,))
    conn.close()
    return jsonify({"status": "ok", "id": goal_id})
