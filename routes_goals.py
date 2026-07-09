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

from web_core import db, respond, today_iso
from db import now_iso

bp = Blueprint("goals", __name__)

TIMEFRAMES = ("week", "month", "quarter", "year", "by_date", "ongoing")


def current_period_start(timeframe: str, today: str = None) -> str:
    """Anchor date for a timeframe's current period. by_date/ongoing return today
    as a harmless anchor (their rollover keys off end_date / never)."""
    d = datetime.strptime(today or today_iso(), "%Y-%m-%d").date()
    if timeframe == "week":
        return (d - timedelta(days=d.weekday())).isoformat()       # Monday
    if timeframe == "quarter":
        q_month = ((d.month - 1) // 3) * 3 + 1
        return d.replace(month=q_month, day=1).isoformat()         # 1st of quarter
    if timeframe == "year":
        return d.replace(month=1, day=1).isoformat()               # Jan 1
    if timeframe == "month":
        return d.replace(day=1).isoformat()                        # 1st of month
    return d.isoformat()


def goal_progress(conn, g) -> dict:
    """Derive progress from which fields exist (not from deprecated `kind`). Returns a
    dict the template switches on via `shape` ('measure'|'rollup'|'milestone'|'both'),
    keeping the legacy keys (current/target/done/total/pct/linked) so nothing breaks."""
    rows = conn.execute(
        "SELECT id, title, done FROM tasks WHERE goal_id=? AND archived_at IS NULL "
        "AND deleted_at IS NULL ORDER BY done, sort_order, id", (g["id"],)).fetchall()
    total = len(rows)
    done = sum(1 for r in rows if r["done"])
    linked = [{"title": r["title"], "done": bool(r["done"])} for r in rows]

    cur = g["current_num"] or 0
    tgt = g["target_num"]
    unit = g["unit"]
    achieved = g["achieved_at"] is not None

    # measure present: an explicit target, OR a current number carrying a unit.
    has_measure = (tgt is not None) or (bool(unit) and cur)
    has_tasks = total > 0

    if has_measure and has_tasks:
        shape = "both"
    elif has_measure:
        shape = "measure"
    elif has_tasks:
        shape = "rollup"
    else:
        shape = "milestone"

    if shape in ("measure", "both"):
        pct = (cur / tgt * 100) if tgt else 0
    elif shape == "rollup":
        pct = (done / total * 100) if total else 0
    else:
        pct = 100 if achieved else 0

    return {"shape": shape, "linked": linked, "done": done, "total": total,
            "current": cur, "target": tgt or 0, "unit": unit or "",
            "achieved": achieved, "pct": pct}


def archive_expired_goals(conn):
    """Auto-archive goals whose period has ended. Applies to week/month/quarter/year
    (computed period ends) and by_date (after end_date passes); ongoing NEVER archives.
    Legacy rows with a NULL timeframe fall back to their `period`."""
    today = today_iso()
    ts = now_iso()
    with conn:
        conn.execute(
            "UPDATE goals SET archived_at=? WHERE archived_at IS NULL "
            "AND COALESCE(timeframe, period)='week' "
            "AND date(period_start, '+7 day') <= ?", (ts, today))
        conn.execute(
            "UPDATE goals SET archived_at=? WHERE archived_at IS NULL "
            "AND COALESCE(timeframe, period)='month' "
            "AND date(period_start, 'start of month', '+1 month') <= ?", (ts, today))
        conn.execute(
            "UPDATE goals SET archived_at=? WHERE archived_at IS NULL "
            "AND COALESCE(timeframe, period)='quarter' "
            "AND date(period_start, '+3 months') <= ?", (ts, today))
        conn.execute(
            "UPDATE goals SET archived_at=? WHERE archived_at IS NULL "
            "AND COALESCE(timeframe, period)='year' "
            "AND date(period_start, '+1 year') <= ?", (ts, today))
        conn.execute(
            "UPDATE goals SET archived_at=? WHERE archived_at IS NULL "
            "AND COALESCE(timeframe, period)='by_date' "
            "AND end_date IS NOT NULL AND end_date < ?", (ts, today))


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
    rows = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL ORDER BY created").fetchall()
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
    if _ajax():
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
    conn = db()
    with conn:
        conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
    conn.close()
    return respond(True, "Goal deleted", to="/goals")


def _ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"
