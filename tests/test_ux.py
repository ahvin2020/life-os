"""UX-audit regressions (2026-07-10): sticky-today drift in the outbound surfaces
(digest/brief/bot), editor done-boundary routing, goal soft-delete + undo, and the
lazy New-task flow's server support."""

import os
from datetime import date, timedelta

import db_init  # noqa: F401  (ensures path set up by conftest import order)
from capture import create_task
from db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _days_ago_iso(n):
    return (date.fromisoformat(today_iso()) - timedelta(days=n)).isoformat()


# ── sticky-today drift: the 07:00 surfaces must agree with the Today page ─────
def test_digest_includes_rolled_over_planned_task(client):
    from proactive import _digest_tasks, build_digest
    conn = _db()
    with conn:
        tid = create_task(conn, "Rolled-over plan", col="week")
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?", (_days_ago_iso(2), tid))
    titles = [r["title"] for r in _digest_tasks(conn, today_iso())]
    assert "Rolled-over plan" in titles
    digest = build_digest(conn, day=today_iso())
    conn.close()
    assert "Rolled-over plan · ☀ planned" in digest      # marker survives rollover


def test_brief_context_includes_rolled_over_planned_task(client):
    from proactive import build_brief_context
    conn = _db()
    with conn:
        tid = create_task(conn, "Sticky brief task", col="backlog")
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?", (_days_ago_iso(3), tid))
    ctx = build_brief_context(conn, day=today_iso())
    conn.close()
    titles = [t["title"] for t in ctx["tasks"]]
    assert "Sticky brief task" in titles


def test_bot_task_line_badges_rolled_over_plan(client):
    from queries import _task_line
    today = today_iso()
    line = _task_line({"title": "X", "planned_on": _days_ago_iso(1)}, today)
    assert "☀" in line
    line = _task_line({"title": "X", "planned_on": None}, today)
    assert "☀" not in line


# ── editor done boundary = real completion ────────────────────────────────────
def test_editor_col_done_runs_completion(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Editor done", col="week")
        recur = create_task(conn, "Editor recurring", col="week",
                            due_date=today_iso(), recur_rule="daily")
    conn.close()
    client.post(f"/tasks/{tid}/edit", data={"col": "done"})
    client.post(f"/tasks/{recur}/edit", data={"col": "done"})
    conn = _db()
    row = conn.execute("SELECT done, completed_at, col FROM tasks WHERE id=?",
                       (tid,)).fetchone()
    assert row["done"] == 1 and row["completed_at"] == today_iso() and row["col"] == "done"
    respawn = conn.execute(
        "SELECT 1 FROM tasks WHERE title='Editor recurring' AND done=0").fetchone()
    conn.close()
    assert respawn is not None                            # recurrence respawned


def test_editor_col_out_of_done_uncompletes(client):
    from tasks_core import complete_task
    conn = _db()
    with conn:
        tid = create_task(conn, "Editor reopen", col="week")
        complete_task(conn, tid, True)
    conn.close()
    client.post(f"/tasks/{tid}/edit", data={"col": "backlog"})
    conn = _db()
    row = conn.execute("SELECT done, completed_at, col FROM tasks WHERE id=?",
                       (tid,)).fetchone()
    conn.close()
    assert row["done"] == 0 and row["completed_at"] is None and row["col"] == "backlog"


# ── goal soft-delete + undo ───────────────────────────────────────────────────
def test_goal_delete_is_soft_and_restorable(client):
    r = client.post("/goals/new", data={"title": "Fragile goal", "timeframe": "ongoing"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    gid = r.get_json()["id"]
    client.post(f"/goals/{gid}/delete")
    conn = _db()
    row = conn.execute("SELECT deleted_at FROM goals WHERE id=?", (gid,)).fetchone()
    conn.close()
    assert row is not None and row["deleted_at"] is not None   # soft, not gone
    assert "Fragile goal" not in client.get("/goals").data.decode()
    client.post(f"/goals/{gid}/restore")
    assert "Fragile goal" in client.get("/goals").data.decode()


def test_goal_soft_delete_keeps_task_links(client):
    r = client.post("/goals/new", data={"title": "Linked goal", "timeframe": "ongoing"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    gid = r.get_json()["id"]
    conn = _db()
    with conn:
        tid = create_task(conn, "Linked task", col="week", goal_id=gid)
    conn.close()
    client.post(f"/goals/{gid}/delete")
    client.post(f"/goals/{gid}/restore")
    conn = _db()
    row = conn.execute("SELECT goal_id FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["goal_id"] == gid                # ON DELETE SET NULL never fired


def test_v6_migration_adds_goals_deleted_at(client, tmp_path):
    import sqlite3
    p = str(tmp_path / "old-goals.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
           INSERT INTO meta VALUES ('schema_version', '5');
           CREATE TABLE goals (
             id INTEGER PRIMARY KEY, title TEXT NOT NULL,
             period TEXT NOT NULL, period_start TEXT NOT NULL, kind TEXT NOT NULL,
             target_num REAL, current_num REAL DEFAULT 0, timeframe TEXT,
             end_date TEXT, unit TEXT, achieved_at TEXT, archived_at TEXT,
             created TEXT NOT NULL);
        """)
    conn.commit()
    conn.close()
    result = db_init.init_db(p)
    assert any("goals.deleted_at" in m for m in result["migrated"])


# ── lazy New-task flow: server accepts planned_on at creation ─────────────────
def test_task_new_accepts_planned_on(client):
    r = client.post("/tasks/new",
                    data={"title": "Planned at birth", "col": "week",
                          "planned_on": today_iso()},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    tid = r.get_json()["id"]
    conn = _db()
    row = conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["planned_on"] == today_iso()


# ── ☀ on a done task = reopen + plan (no struck-through zombie on Today) ──────
def test_plan_on_done_task_reopens_it(client):
    from tasks_core import complete_task
    conn = _db()
    with conn:
        tid = create_task(conn, "Do it again", col="week")
        complete_task(conn, tid, True)
    conn.close()
    r = client.post(f"/tasks/{tid}/plan")
    j = r.get_json()
    assert j["planned"] is True and j["reopened"] is True
    conn = _db()
    row = conn.execute(
        "SELECT done, completed_at, col, planned_on FROM tasks WHERE id=?",
        (tid,)).fetchone()
    conn.close()
    assert row["done"] == 0 and row["completed_at"] is None
    assert row["col"] == "week" and row["planned_on"] == today_iso()


# ── router plan undo restores a prior rolled-over plan ────────────────────────
def test_router_plan_undo_restores_previous_plan(client):
    from router import handle_callback
    prev = _days_ago_iso(2)
    conn = _db()
    with conn:
        tid = create_task(conn, "Replanned", col="week")
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?", (prev, tid))
    handle_callback(conn, f"u|plan|{tid}|{prev}")
    row = conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["planned_on"] == prev             # restored, not nulled
    handle_callback(conn, f"u|plan|{tid}")       # legacy token → clears
    row = conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["planned_on"] is None


# ── signal-to-noise pass (2026-07-10): quiet Today panel + compact due labels ──
def test_due_label_is_relative_and_compact(client):
    from web_core import _due_label
    def day(n):                                   # n days from today
        return (date.fromisoformat(today_iso()) + timedelta(days=n)).isoformat()
    assert _due_label(day(0)) == "today"
    assert _due_label(day(-1)) == "yesterday"
    assert _due_label(day(-5)) == "5d over"       # severity without date math
    assert _due_label(day(1)) == "tomorrow"
    assert _due_label(day(3)) == date.fromisoformat(day(3)).strftime("%a")
    far = day(20)                                 # beyond a week, same year → no year
    if far[:4] == today_iso()[:4]:
        d = date.fromisoformat(far)
        assert _due_label(far) == f"{d.day} {d.strftime('%b')}"
    assert _due_label("2031-01-05") == "5 Jan 2031"   # other year keeps the year


def test_done_today_row_has_no_pill_and_no_done_text(client):
    """A completed row already says done three ways (check + strike + dim) — it must
    NOT also wear the ☀ pill or a 'done' label (pills overlay on hover and take no
    layout space, so no placeholder is needed either)."""
    conn = _db()
    with conn:
        tid = create_task(conn, "Finished thing", col="week", planned_on=today_iso())
    conn.close()
    client.post(f"/tasks/{tid}/complete", data={"done": "1"})
    html = client.get("/").data.decode()
    hero = html.split('class="card hero"')[1].split('class="card weekpool"')[0] \
        if 'class="card weekpool"' in html else html.split('class="card hero"')[1].split('class="card sub2"')[0]
    # maxsplit=1 — the id appears twice per row (the task div AND its checkbox)
    row = hero.split(f'data-task-id="{tid}"', 1)[1].split("</div>")[0]
    assert "planbtn" not in row                   # no pill, no placeholder
    assert "On today" not in row and "Do today" not in row
    assert ">done</span>" not in row
    assert '<span class="tt">Finished thing</span>' in row   # strike target wraps text


def test_sidebar_health_silent_when_all_ok(client):
    """Healthy systems are SILENT: fresh heartbeats → nothing in the footer;
    a missing/stale heartbeat → the per-job detail rows appear."""
    html = client.get("/").data.decode()          # no heartbeats seeded → detail rows
    assert "capture ·" in html
    conn = _db()
    with conn:
        for key in ("capture_last_ran", "triage_last_ran", "backup_last_ran"):
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         (key, now_iso()))
    conn.close()
    html = client.get("/").data.decode()
    assert "capture ·" not in html and "all systems ok" not in html   # fully silent


def test_done_board_card_has_no_lit_pill_and_no_empty_meta_row(client):
    """A done card's plan/due are history: the pill must read as the reopen action
    ("Do today", hover-revealed), never a lit amber badge, and the meta row must
    collapse (ghost) instead of leaving a blank band under the struck title."""
    from tasks_core import complete_task
    conn = _db()
    with conn:
        tid = create_task(conn, "Wrapped up", col="week",
                          planned_on=today_iso(), due_date=today_iso())
        complete_task(conn, tid, True)
    conn.close()
    html = client.get("/tasks").data.decode()
    # maxsplit=1 everywhere — the card's own data-col="done" attr repeats the marker
    done_col = html.split('<div class="col" data-col="done"', 1)[1]
    # card region = from the task id through its pill (the krow's last element)
    card = done_col.split(f'data-task-id="{tid}"', 1)[1].split("</button>", 1)[0]
    assert "On today ✓" not in card            # pill reads "☀ Do today" (reopen)
    assert 'class="krow ghost"' in card        # meta row hidden until hover
