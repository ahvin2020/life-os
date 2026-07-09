"""Goals: weekly + monthly, two kinds — 'rollup' (counts linked ◎ tasks) and
'number' (a figure you tap to update, e.g. newsletter 438/500)."""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

from web_core import db, respond, today_iso
from db import now_iso

bp = Blueprint("goals", __name__)


def current_period_start(period: str, today: str = None) -> str:
    d = datetime.strptime(today or today_iso(), "%Y-%m-%d").date()
    if period == "week":
        return (d - timedelta(days=d.weekday())).isoformat()   # Monday
    return d.replace(day=1).isoformat()                        # 1st of month


def goal_progress(conn, g) -> dict:
    """Compute progress for a goal. rollup → done/total linked tasks (a linked
    parent counts once; subtasks are not counted separately). number → current/target."""
    out = {"linked": []}
    if g["kind"] == "rollup":
        rows = conn.execute(
            "SELECT id, title, done FROM tasks WHERE goal_id=? AND archived_at IS NULL "
            "AND deleted_at IS NULL ORDER BY done, sort_order, id", (g["id"],)).fetchall()
        total = len(rows)
        done = sum(1 for r in rows if r["done"])
        out.update(done=done, total=total,
                   pct=(done / total * 100) if total else 0,
                   linked=[{"title": r["title"], "done": bool(r["done"])} for r in rows])
    else:
        cur = g["current_num"] or 0
        tgt = g["target_num"] or 0
        out.update(current=cur, target=tgt,
                   pct=(cur / tgt * 100) if tgt else 0)
    return out


def archive_expired_goals(conn):
    """Auto-archive goals whose period has ended (week goals 7 days after their
    Monday start; month goals once the following month begins). Archived goals stay
    queryable but drop out of the active This-week / This-month sections. Same
    pattern as the tasks-board done-archive."""
    today = today_iso()
    with conn:
        conn.execute(
            "UPDATE goals SET archived_at=? WHERE archived_at IS NULL AND period='week' "
            "AND date(period_start, '+7 day') <= ?", (now_iso(), today))
        conn.execute(
            "UPDATE goals SET archived_at=? WHERE archived_at IS NULL AND period='month' "
            "AND date(period_start, 'start of month', '+1 month') <= ?", (now_iso(), today))


@bp.route("/goals")
def goals_page():
    conn = db()
    archive_expired_goals(conn)
    rows = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL ORDER BY period, created").fetchall()
    month, week = [], []
    for g in rows:
        item = dict(g)
        item["progress"] = goal_progress(conn, g)
        (month if g["period"] == "month" else week).append(item)
    conn.close()
    now = datetime.strptime(today_iso(), "%Y-%m-%d").date()
    week_start = now - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6)
    return render_template(
        "goals.html", month_goals=month, week_goals=week,
        month_label=now.strftime("%B"),
        week_label=f"{week_start.strftime('%b %-d')} – {week_end.strftime('%-d')}",
        active="goals")


@bp.route("/goals/new", methods=["POST"])
def goal_new():
    f = request.form
    title = (f.get("title") or "").strip()
    period = f.get("period") if f.get("period") in ("week", "month") else "week"
    kind = f.get("kind") if f.get("kind") in ("rollup", "number") else "rollup"
    if not title:
        return respond(False, "Title required", fallback="/goals")
    target = None
    if kind == "number" and f.get("target"):
        try:
            target = float(f.get("target"))
        except ValueError:
            target = None
    conn = db()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, "
            "current_num, created) VALUES (?,?,?,?,?,0,?)",
            (title, period, current_period_start(period), kind, target, now_iso()))
    gid = cur.lastrowid
    conn.close()
    if _ajax():
        return jsonify({"status": "ok", "id": gid})
    return respond(True, "Goal created", to="/goals")


@bp.route("/goals/<int:goal_id>/update", methods=["POST"])
def goal_update(goal_id):
    """Update the manual number on a 'number' goal (tap-to-edit)."""
    try:
        val = float(request.form.get("current"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "bad number"}), 400
    conn = db()
    with conn:
        conn.execute("UPDATE goals SET current_num=? WHERE id=?", (val, goal_id))
    conn.close()
    return jsonify({"status": "ok", "current": val})


@bp.route("/goals/<int:goal_id>/delete", methods=["POST"])
def goal_delete(goal_id):
    conn = db()
    with conn:
        conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
    conn.close()
    return respond(True, "Goal deleted", to="/goals")


def _ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"
