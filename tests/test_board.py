"""Board redesign (2026-07-10): sticky Today, on-today pinning in the week column,
week_since staleness clock (schema v5), and drag-to-Done completing properly."""

import os
from datetime import date, timedelta

from core import db_init  # noqa: F401  (ensures path set up by conftest import order)
from domain.capture import create_task
from domain.tasks_core import complete_task, today_tasks, week_tasks
from core.db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _days_ago_iso(n):
    return (date.fromisoformat(today_iso()) - timedelta(days=n)).isoformat()


# ── schema v5 migration ───────────────────────────────────────────────────────
def test_v5_migration_adds_and_backfills_week_since(client, tmp_path):
    """A v4 DB (no week_since) gains the column, and open week tasks get today."""
    import sqlite3
    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
           INSERT INTO meta VALUES ('schema_version', '4');
           CREATE TABLE tasks (
             id INTEGER PRIMARY KEY, title TEXT, col TEXT DEFAULT 'backlog',
             sort_order INTEGER DEFAULT 0, priority TEXT, category TEXT,
             due_date TEXT, planned_on TEXT, recur_rule TEXT, goal_id INTEGER,
             parent_id INTEGER, done INTEGER DEFAULT 0, completed_at TEXT,
             archived_at TEXT, deleted_at TEXT,
             reschedule_count INTEGER NOT NULL DEFAULT 0,
             created TEXT, updated TEXT);
           INSERT INTO tasks (title, col, done) VALUES
             ('parked', 'week', 0), ('finished', 'week', 1), ('later', 'backlog', 0);
        """)
    conn.commit()
    conn.close()
    result = db_init.init_db(p)
    assert any("week_since" in m for m in result["migrated"])
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    rows = {r["title"]: r["week_since"] for r in
            conn.execute("SELECT title, week_since FROM tasks")}
    conn.close()
    assert rows["parked"] == today_iso()      # open week task backfilled
    assert rows["finished"] is None           # done tasks get no clock
    assert rows["later"] is None              # backlog untouched


def test_v11_migration_adds_description(tmp_path):
    """A v10 DB (no description) gains a nullable tasks.description column."""
    import sqlite3
    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
           INSERT INTO meta VALUES ('schema_version', '10');
           CREATE TABLE tasks (
             id INTEGER PRIMARY KEY, title TEXT, col TEXT DEFAULT 'backlog',
             sort_order INTEGER DEFAULT 0, priority TEXT, category TEXT,
             due_date TEXT, planned_on TEXT, recur_rule TEXT, goal_id INTEGER,
             parent_id INTEGER, done INTEGER DEFAULT 0, completed_at TEXT,
             archived_at TEXT, deleted_at TEXT,
             reschedule_count INTEGER NOT NULL DEFAULT 0, week_since TEXT,
             media TEXT, link TEXT, created TEXT, updated TEXT);
           INSERT INTO tasks (title, col) VALUES ('a task', 'week');
        """)
    conn.commit()
    conn.close()
    result = db_init.init_db(p)
    assert any("description" in m for m in result["migrated"])
    conn = sqlite3.connect(p)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    conn.close()
    assert "description" in cols


def test_task_description_round_trips_web_and_shows_marker(client):
    """Web new + edit persist a description; the card wears the muted 'has notes' marker,
    and clearing it removes both the value and the marker (detail lives in the editor,
    the card just flags that it exists)."""
    r = client.post("/tasks/new", data={"title": "Prep deck", "description": "cover churn",
                                         "surface": "kcard"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    tid = r.get_json()["id"]
    card = r.get_json()["card_html"]
    assert 'class="kdesc"' in card and 'data-description="cover churn"' in card
    conn = _db()
    assert conn.execute("SELECT description FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "cover churn"
    conn.close()
    # clearing wipes the value and drops the marker
    r2 = client.post(f"/tasks/{tid}/edit", data={"description": "", "surface": "kcard"},
                     headers={"X-Requested-With": "XMLHttpRequest"})
    assert 'class="kdesc"' not in r2.get_json()["card_html"]
    conn = _db()
    assert conn.execute("SELECT description FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None
    conn.close()


# ── sticky today ──────────────────────────────────────────────────────────────
def test_planned_task_rolls_over_until_done(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Lingering plan", col="backlog")
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?",
                     (_days_ago_iso(3), tid))
    ids = [t["id"] for t in today_tasks(conn)]
    assert tid in ids                          # planned 3 days ago, still on Today
    with conn:
        complete_task(conn, tid, True)
    ids = [t["id"] for t in today_tasks(conn)]
    assert tid in ids                          # completed today → dimmed, still shown
    conn.close()


def test_plan_toggle_clears_a_rolled_over_plan(client):
    """Tapping ☀ on a task planned days ago must CLEAR the plan (and count a
    postpone), not silently re-stamp it to today."""
    conn = _db()
    with conn:
        tid = create_task(conn, "Rolled over", col="week")
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?",
                     (_days_ago_iso(2), tid))
    conn.close()
    r = client.post(f"/tasks/{tid}/plan")
    assert r.get_json()["planned"] is False
    conn = _db()
    row = conn.execute("SELECT planned_on, reschedule_count FROM tasks WHERE id=?",
                       (tid,)).fetchone()
    conn.close()
    assert row["planned_on"] is None and row["reschedule_count"] == 1


def test_plan_promotes_backlog_task_into_week(client):
    """On-today ⊆ this-week: ☀ on a backlog task moves it to col='week'."""
    conn = _db()
    with conn:
        tid = create_task(conn, "Promoted by plan", col="backlog")
    conn.close()
    r = client.post(f"/tasks/{tid}/plan")
    assert r.get_json()["planned"] is True
    conn = _db()
    row = conn.execute("SELECT col, week_since, planned_on FROM tasks WHERE id=?",
                       (tid,)).fetchone()
    conn.close()
    assert row["col"] == "week" and row["week_since"] == today_iso()
    assert row["planned_on"] == today_iso()


def test_unplan_keeps_task_in_week_at_top(client):
    """Un-ticking ☀ must NOT demote to backlog — not-today ≠ not-this-week: the
    task lands in the week column, at the top of its order."""
    conn = _db()
    with conn:
        create_task(conn, "Existing week task", col="week")
        tid = create_task(conn, "Was on today", col="backlog",
                          planned_on=today_iso())
    conn.close()
    r = client.post(f"/tasks/{tid}/plan")            # untick ☀
    assert r.get_json()["planned"] is False
    conn = _db()
    row = conn.execute("SELECT col, week_since, planned_on FROM tasks WHERE id=?",
                       (tid,)).fetchone()
    assert row["col"] == "week" and row["week_since"] == today_iso()
    assert row["planned_on"] is None
    ids = [x["id"] for x in conn.execute(
        "SELECT id FROM tasks WHERE col='week' AND parent_id IS NULL "
        "ORDER BY sort_order, id")]
    conn.close()
    assert ids[0] == tid                              # top of This week


def test_week_pool_excludes_sticky_planned(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Planned yesterday", col="week")
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?",
                     (_days_ago_iso(1), tid))
        other = create_task(conn, "Just parked", col="week")
    ids = [t["id"] for t in week_tasks(conn)]
    conn.close()
    assert tid not in ids and other in ids


# ── board pinning ─────────────────────────────────────────────────────────────
def test_on_today_backlog_task_pins_into_week_column(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Backlog but planned", col="backlog",
                          planned_on=today_iso())
        create_task(conn, "Plain backlog", col="backlog")
        create_task(conn, "Plain week", col="week")
    conn.close()
    html = client.get("/tasks").data.decode()
    week_col = html.split('data-col="week"')[1].split('data-col="done"')[0]
    backlog_col = html.split('data-col="backlog"')[1].split('data-col="week"')[0]
    assert f'data-task-id="{tid}"' in week_col          # renders in This week…
    assert f'data-task-id="{tid}"' not in backlog_col   # …not in Backlog
    assert "pinned" in week_col and "☀ On today ✓" in week_col
    conn = _db()
    col = conn.execute("SELECT col FROM tasks WHERE id=?", (tid,)).fetchone()["col"]
    conn.close()
    assert col == "backlog"                              # stored col untouched


def test_pill_plan_order_persists_across_reload(client):
    """Planning a backlog task via the ☀ pill pins it to the TOP of This week. The pill
    path now persists that placement (the col-less reorder the JS posts after the move),
    so a refresh keeps it on top instead of letting its stale backlog sort_order sink it
    below the other pinned cards. Regression: only drags persisted order before, so the
    on-today order was lost on reload."""
    import re
    conn = _db()
    with conn:
        a = create_task(conn, "Already on today", col="week", planned_on=today_iso())
        b = create_task(conn, "Also on today", col="week", planned_on=today_iso())
        newbie = create_task(conn, "Just planned", col="backlog")  # lands at a high sort_order
    conn.close()
    client.post(f"/tasks/{newbie}/plan")                       # promotes + pins by rule
    # the pill's follow-up: persist the week stack with the just-planned card on top
    client.post("/tasks/reorder", json={"ids": [newbie, a, b]})
    html = client.get("/tasks").data.decode()
    # the week region spans from its column header to the Done column's ('data-col="week"'
    # recurs — the kstack carries it too — so slice by index, not split()[1])
    w = html.find('data-col="week"')
    week_col = html[w:html.find('data-col="done"', w)]
    order = [int(m) for m in re.findall(r'<div class="kcard[^>]*?data-task-id="(\d+)"', week_col, re.S)]
    assert order[:3] == [newbie, a, b], f"pinned order not persisted across reload: {order}"


def test_on_today_badge_lights_for_due_and_overdue_not_only_planned(client):
    """A card floats to the top of This week for a due date / overdue too, not only a
    ☀ plan (is_pinned). So the persistent "On today ✓" badge must light for ALL of
    them — otherwise a due/overdue card sits at the top with its status hidden behind
    hover (the "☀ Do today" pill), which reads as "not on today" (2026-07-17)."""
    conn = _db()
    with conn:
        due = create_task(conn, "Due today", col="backlog")
        conn.execute("UPDATE tasks SET due_date=? WHERE id=?", (today_iso(), due))
        od = create_task(conn, "Overdue", col="backlog")
        conn.execute("UPDATE tasks SET due_date=? WHERE id=?", (_days_ago_iso(3), od))
    conn.close()
    html = client.get("/tasks").data.decode()
    for tid in (due, od):
        lit = (f'<button class="planbtn kplan on" data-task-id="{tid}" '
               f'title="plan for today" type="button">☀ On today ✓</button>')
        assert lit in html, f"due/overdue card {tid} must wear a lit On today badge"


# ── week_since staleness clock ────────────────────────────────────────────────
def test_week_since_stamped_and_cleared_by_moves(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Clocked", col="week")
    row = conn.execute("SELECT week_since FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["week_since"] == today_iso()              # stamped at creation
    conn.close()
    client.post("/tasks/reorder", json={"col": "backlog", "ids": [tid]})
    conn = _db()
    assert conn.execute("SELECT week_since FROM tasks WHERE id=?",
                        (tid,)).fetchone()["week_since"] is None   # cleared on leave
    conn.close()
    client.post("/tasks/reorder", json={"col": "week", "ids": [tid]})
    conn = _db()
    assert conn.execute("SELECT week_since FROM tasks WHERE id=?",
                        (tid,)).fetchone()["week_since"] == today_iso()  # re-stamped
    conn.close()


def test_week_since_survives_reorder_within_week(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Old timer", col="week")
        conn.execute("UPDATE tasks SET week_since=? WHERE id=?",
                     (_days_ago_iso(9), tid))
    conn.close()
    client.post("/tasks/reorder", json={"col": "week", "ids": [tid]})
    conn = _db()
    assert conn.execute("SELECT week_since FROM tasks WHERE id=?",
                        (tid,)).fetchone()["week_since"] == _days_ago_iso(9)
    conn.close()


def test_complete_clears_and_uncomplete_restarts_clock(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Cycled", col="week")
        conn.execute("UPDATE tasks SET week_since=? WHERE id=?",
                     (_days_ago_iso(5), tid))
        complete_task(conn, tid, True)
    row = conn.execute("SELECT col, week_since FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["col"] == "done" and row["week_since"] is None
    with conn:
        complete_task(conn, tid, False)
    row = conn.execute("SELECT col, week_since FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["col"] == "week" and row["week_since"] == today_iso()  # fresh clock


def test_stale_badge_renders_only_past_thresholds(client):
    conn = _db()
    with conn:
        stale = create_task(conn, "Ancient week task", col="week")
        conn.execute("UPDATE tasks SET week_since=? WHERE id=?",
                     (_days_ago_iso(10), stale))
        create_task(conn, "Fresh week task", col="week")
    conn.close()
    html = client.get("/tasks").data.decode()
    assert '<span class="kstale">10d</span>' in html
    assert html.count("kstale") == 1                     # fresh card stays clean


# ── drag across the done boundary ─────────────────────────────────────────────
def test_drag_into_done_completes_and_respawns_recurring(client):
    conn = _db()
    with conn:
        plain = create_task(conn, "Dragged done", col="week")
        recur = create_task(conn, "Water plants", col="week",
                            due_date=today_iso(), recur_rule="daily")
    conn.close()
    client.post("/tasks/reorder", json={"col": "done", "ids": [plain, recur]})
    conn = _db()
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, done, completed_at, col FROM tasks WHERE id IN (?, ?)",
        (plain, recur))}
    assert all(r["done"] == 1 and r["completed_at"] == today_iso()
               and r["col"] == "done" for r in rows.values())
    respawn = conn.execute(
        "SELECT * FROM tasks WHERE title='Water plants' AND done=0").fetchone()
    conn.close()
    assert respawn is not None and respawn["col"] == "week"   # recurrence respawned


def test_drag_out_of_done_uncompletes(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Oops not done", col="week")
        complete_task(conn, tid, True)
    conn.close()
    client.post("/tasks/reorder", json={"col": "backlog", "ids": [tid]})
    conn = _db()
    row = conn.execute("SELECT done, completed_at, col, week_since FROM tasks WHERE id=?",
                       (tid,)).fetchone()
    conn.close()
    assert row["done"] == 0 and row["completed_at"] is None
    assert row["col"] == "backlog" and row["week_since"] is None
