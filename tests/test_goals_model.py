"""Goals v3 — flexible-goal model tests.

A goal is a TITLE; progress DERIVES from which fields exist (measure / rollup /
milestone / both), not from the deprecated `kind`. Covers the v2→v3 migration,
progress derivation for every shape, per-timeframe archive rollover, the achieved
toggle route, and the router goal actions. Uses the throwaway DB via conftest.
"""

import os
import sqlite3

import router
from capture import create_task
from db import connect, now_iso, today_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _mkgoal(conn, title, timeframe="week", period="week", period_start=None,
            kind="rollup", target=None, current=0, end_date=None, unit=None,
            achieved_at=None):
    """Insert a goal with the v3 columns and return its id."""
    from routes_goals import current_period_start
    ps = period_start or current_period_start(timeframe)
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, "
            "current_num, timeframe, end_date, unit, achieved_at, created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (title, period, ps, kind, target, current, timeframe, end_date, unit,
             achieved_at, now_iso()))
    return cur.lastrowid


def _fn(obj):
    import json
    return lambda prompt: json.dumps(obj)


# ── v2 → v3 migration ─────────────────────────────────────────────────────────
def test_v2_to_v3_migration_backfills_timeframe(tmp_path):
    """A legacy v2 goals row (period + kind, no new columns) migrates in place:
    the four new columns are added and timeframe is backfilled from period, with the
    measure fields preserved."""
    import db_init
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    raw.execute(
        "CREATE TABLE goals (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
        "period TEXT NOT NULL, period_start TEXT NOT NULL, kind TEXT NOT NULL, "
        "target_num REAL, current_num REAL DEFAULT 0, archived_at TEXT, created TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
        "VALUES ('Newsletter', 'week', '2026-07-06', 'number', 500, 438, 'x')")
    raw.commit()
    raw.close()

    db_init.init_db(path)

    conn = connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(goals)").fetchall()]
    row = conn.execute("SELECT * FROM goals WHERE title='Newsletter'").fetchone()
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    for c in ("timeframe", "end_date", "unit", "achieved_at"):
        assert c in cols, c
    assert row["timeframe"] == "week"                      # backfilled from period
    assert row["target_num"] == 500 and row["current_num"] == 438   # measure preserved
    from db import SCHEMA_VERSION
    assert ver == str(SCHEMA_VERSION)                      # stamped to the current version


# ── progress derivation — one per shape ───────────────────────────────────────
def test_shape_measure(client):
    conn = _db()
    from routes_goals import goal_progress
    gid = _mkgoal(conn, "Subs", timeframe="month", target=500, current=460, unit="subs")
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    p = goal_progress(conn, g)
    conn.close()
    assert p["shape"] == "measure"
    assert p["current"] == 460 and p["target"] == 500 and p["unit"] == "subs"
    assert p["pct"] == 92


def test_shape_rollup(client):
    conn = _db()
    from routes_goals import goal_progress
    gid = _mkgoal(conn, "Videos", timeframe="month")
    t1 = create_task(conn, "A", col="week", goal_id=gid)
    create_task(conn, "B", col="week", goal_id=gid)
    with conn:
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (t1,))
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    p = goal_progress(conn, g)
    conn.close()
    assert p["shape"] == "rollup"
    assert p["done"] == 1 and p["total"] == 2 and p["pct"] == 50


def test_shape_milestone_toggle_pct(client):
    conn = _db()
    from routes_goals import goal_progress
    gid = _mkgoal(conn, "Launch community", timeframe="quarter")
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    p = goal_progress(conn, g)
    assert p["shape"] == "milestone" and p["achieved"] is False and p["pct"] == 0
    with conn:
        conn.execute("UPDATE goals SET achieved_at=? WHERE id=?", (now_iso(), gid))
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    p = goal_progress(conn, g)
    conn.close()
    assert p["shape"] == "milestone" and p["achieved"] is True and p["pct"] == 100


def test_shape_both(client):
    conn = _db()
    from routes_goals import goal_progress
    gid = _mkgoal(conn, "Publish + measure", timeframe="month", target=2, current=1)
    t1 = create_task(conn, "A", col="week", goal_id=gid)
    create_task(conn, "B", col="week", goal_id=gid)
    with conn:
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (t1,))
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    p = goal_progress(conn, g)
    conn.close()
    assert p["shape"] == "both"
    assert p["pct"] == 50                                  # bar comes from the measure (1/2)
    assert p["done"] == 1 and p["total"] == 2              # task fraction still exposed


# ── archive rollover per timeframe ────────────────────────────────────────────
def test_rollover_per_timeframe(client):
    from routes_goals import archive_expired_goals
    conn = _db()
    today = today_iso()
    # expired periods
    wk = _mkgoal(conn, "wk", timeframe="week", period_start="2026-06-29")       # >7d ago
    mo = _mkgoal(conn, "mo", timeframe="month", period="month", period_start="2026-05-01")
    qt = _mkgoal(conn, "qt", timeframe="quarter", period="month", period_start="2026-01-01")
    yr = _mkgoal(conn, "yr", timeframe="year", period="month", period_start="2024-01-01")
    bd = _mkgoal(conn, "bd", timeframe="by_date", period="month", end_date="2026-01-01")
    # must stay active
    og = _mkgoal(conn, "og", timeframe="ongoing", period="month", period_start="2020-01-01")
    bd_future = _mkgoal(conn, "bdf", timeframe="by_date", period="month", end_date="2099-01-01")
    fresh_wk = _mkgoal(conn, "freshwk", timeframe="week")

    archive_expired_goals(conn)

    def arch(gid):
        return conn.execute("SELECT archived_at FROM goals WHERE id=?", (gid,)).fetchone()["archived_at"]

    for gid in (wk, mo, qt, yr, bd):
        assert arch(gid) is not None, gid
    for gid in (og, bd_future, fresh_wk):
        assert arch(gid) is None, gid
    conn.close()


# ── achieved toggle route ─────────────────────────────────────────────────────
def test_achieve_route_toggles(client):
    conn = _db()
    gid = _mkgoal(conn, "Milestone", timeframe="quarter")
    conn.close()
    r = client.post(f"/goals/{gid}/achieve")
    assert r.status_code == 200 and r.get_json()["achieved"] is True
    conn = _db()
    assert conn.execute("SELECT achieved_at FROM goals WHERE id=?", (gid,)).fetchone()["achieved_at"]
    conn.close()
    r = client.post(f"/goals/{gid}/achieve")
    assert r.get_json()["achieved"] is False
    conn = _db()
    assert conn.execute("SELECT achieved_at FROM goals WHERE id=?", (gid,)).fetchone()["achieved_at"] is None
    conn.close()


# ── goal creation via the web form (all-optional-except-title) ────────────────
def test_goal_new_title_only(client):
    r = client.post("/goals/new", data={"title": "Just a title"})
    assert r.status_code in (200, 302)
    conn = _db()
    row = conn.execute("SELECT * FROM goals WHERE title='Just a title'").fetchone()
    conn.close()
    assert row["timeframe"] == "week" and row["target_num"] is None and row["unit"] is None


def test_goal_new_by_date_with_measure(client):
    r = client.post("/goals/new", data={
        "title": "Grow subs", "timeframe": "by_date", "end_date": "2026-12-31",
        "current": "460", "target": "500", "unit": "subs"})
    assert r.status_code in (200, 302)
    conn = _db()
    row = conn.execute("SELECT * FROM goals WHERE title='Grow subs'").fetchone()
    conn.close()
    assert row["timeframe"] == "by_date" and row["end_date"] == "2026-12-31"
    assert row["target_num"] == 500 and row["current_num"] == 460 and row["unit"] == "subs"


# ── goals page renders every shape + section ──────────────────────────────────
def test_goals_page_renders_all_shapes(client):
    conn = _db()
    _mkgoal(conn, "Subs measure", timeframe="month", target=500, current=460, unit="subs")
    gid = _mkgoal(conn, "Videos rollup", timeframe="week")
    create_task(conn, "A", col="week", goal_id=gid)
    _mkgoal(conn, "Launch milestone", timeframe="quarter")
    _mkgoal(conn, "By date goal", timeframe="by_date", period="month", end_date="2099-01-01")
    _mkgoal(conn, "Ongoing goal", timeframe="ongoing", period="month")
    conn.close()
    html = client.get("/goals").data.decode()
    assert html and "Subs measure" in html and "460 / 500 subs" in html
    assert "Videos rollup" in html and "Launch milestone" in html
    assert "mark achieved" in html                          # milestone toggle
    assert "This quarter" in html and "By date" in html and "Ongoing" in html
    assert "gform" in html and 'id="gtimeframe"' in html          # styled new-goal form


# ── router goal actions ───────────────────────────────────────────────────────
def test_router_mark_goal_achieved(client):
    conn = _db()
    gid = _mkgoal(conn, "Ship v1", timeframe="quarter")
    out = router.route(conn, "I shipped v1!",
                       claude_fn=_fn({"action": "mark_goal_achieved", "id": gid}))
    row = conn.execute("SELECT achieved_at FROM goals WHERE id=?", (gid,)).fetchone()
    conn.close()
    assert row["achieved_at"] is not None
    assert "Achieved" in out["reply"]


def test_router_mark_goal_achieved_bad_id_clarifies(client):
    conn = _db()
    out = router.route(conn, "mark it achieved",
                       claude_fn=_fn({"action": "mark_goal_achieved", "id": 999}))
    conn.close()
    assert "couldn't find" in out["reply"].lower()


def test_router_update_goal_number_still_works(client):
    conn = _db()
    gid = _mkgoal(conn, "News", timeframe="month", kind="number", target=500, current=400, unit="subs")
    out = router.route(conn, "newsletter is at 460",
                       claude_fn=_fn({"action": "update_goal_number", "id": gid, "value": 460}))
    cur = conn.execute("SELECT current_num FROM goals WHERE id=?", (gid,)).fetchone()["current_num"]
    conn.close()
    assert cur == 460 and "460/500" in out["reply"]


def test_router_create_goal_timeframe(client):
    conn = _db()
    router.route(conn, "goal: read 12 books this year", claude_fn=_fn({
        "action": "create_goal", "title": "Read 12 books", "timeframe": "year",
        "target": 12, "unit": "books"}))
    row = conn.execute("SELECT * FROM goals WHERE title='Read 12 books'").fetchone()
    conn.close()
    assert row and row["timeframe"] == "year" and row["target_num"] == 12 and row["unit"] == "books"
