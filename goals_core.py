"""Pure goal-domain helpers shared by the Goals page and the non-Flask surfaces
(bot router, proactive AI, queries).

Blueprint-free (like tasks_core): period math, progress derivation + formatting,
and the archive/purge sweeps live here so a bot daemon can import them without
Flask. `routes_goals.py` re-exports these for back-compat.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from db import now_iso, days_ago_iso, today_iso

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


def format_goal_progress(p: dict) -> str:
    """Compact one-line progress string for a goal_progress() dict, used everywhere a
    goal is shown as text (bot router context, morning digest, AI brief, backlog)."""
    shape = p.get("shape")
    if shape in ("measure", "both"):
        unit = (" " + p["unit"]) if p.get("unit") else ""
        return f"{int(p.get('current', 0))}/{int(p.get('target', 0))}{unit}"
    if shape == "rollup":
        return f"{p.get('done', 0)}/{p.get('total', 0)} tasks"
    return "✓ achieved" if p.get("achieved") else "in progress"


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


def purge_deleted_goals(conn, days: int = 30):
    """Hard-delete goals soft-deleted more than `days` ago (undo window elapsed).
    Same pattern as tasks_core.purge_deleted."""
    cutoff = days_ago_iso(days)
    with conn:
        conn.execute("DELETE FROM goals WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                     (cutoff,))
