"""Life OS test suite — exercises the product logic through the real routes and the
real markdown vault (verification checklist item 1)."""

import os

import db_init  # noqa: F401  (ensures path set up by conftest import order)
import web_core
import vault_store
from capture import create_task, route_capture
from tasks_core import (next_due_date, complete_task, today_tasks, week_tasks,
                          archive_old_done)
from db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


# ── pages render ──────────────────────────────────────────────────────────────
def test_pages_200(client):
    for path in ("/", "/tasks", "/notes", "/journal", "/goals"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_mockup_structure_present(client):
    """Spot-check the approved mockup's classes/structure made it into the render."""
    home = client.get("/").data.decode()
    assert 'class="qcap"' in home and 'id="qin"' in home        # composer
    assert 'data-t="journal"' in home                            # type chips
    tasks = client.get("/tasks").data.decode()
    assert 'class="board"' in tasks and 'data-cat="content"' in tasks  # kanban + chips


# ── tasks ─────────────────────────────────────────────────────────────────────
def test_create_and_complete_sets_completed_at(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Pay invoice", col="week")
    conn.close()
    r = client.post(f"/tasks/{tid}/complete", data={"done": "1"})
    assert r.status_code == 200
    conn = _db()
    row = conn.execute("SELECT done, completed_at, col FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["done"] == 1 and row["completed_at"] == today_iso() and row["col"] == "done"


def test_subtask_auto_completes_parent(client):
    conn = _db()
    with conn:
        pid = create_task(conn, "Publish video", col="week")
        s1 = create_task(conn, "Record", parent_id=pid)
        s2 = create_task(conn, "Edit", parent_id=pid)
    conn.close()
    client.post(f"/tasks/{s1}/complete", data={"done": "1"})
    r = client.post(f"/tasks/{s2}/complete", data={"done": "1"})
    assert r.get_json().get("parent_completed") is True
    conn = _db()
    p = conn.execute("SELECT done FROM tasks WHERE id=?", (pid,)).fetchone()
    conn.close()
    assert p["done"] == 1
    # unchecking a subtask un-completes the parent
    client.post(f"/tasks/{s2}/complete", data={"done": "0"})
    conn = _db()
    p = conn.execute("SELECT done FROM tasks WHERE id=?", (pid,)).fetchone()
    conn.close()
    assert p["done"] == 0


def test_reorder_persists(client):
    conn = _db()
    with conn:
        a = create_task(conn, "A", col="week")
        b = create_task(conn, "B", col="week")
        c = create_task(conn, "C", col="week")
    conn.close()
    r = client.post("/tasks/reorder", json={"col": "week", "ids": [c, a, b]})
    assert r.status_code == 200
    conn = _db()
    rows = conn.execute(
        "SELECT id FROM tasks WHERE col='week' AND parent_id IS NULL ORDER BY sort_order").fetchall()
    conn.close()
    assert [x["id"] for x in rows] == [c, a, b]


def test_recurring_respawns_with_next_due(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Weekly review", col="week",
                          due_date=today_iso(), recur_rule="daily")
    conn.close()
    client.post(f"/tasks/{tid}/complete", data={"done": "1"})
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE title='Weekly review' AND parent_id IS NULL").fetchall()
    conn.close()
    assert len(rows) == 2                       # original + respawn
    dones = [r for r in rows if r["done"]]
    opens = [r for r in rows if not r["done"]]
    assert len(dones) == 1 and len(opens) == 1
    assert opens[0]["due_date"] == next_due_date("daily", today_iso())


def test_next_due_date_forms():
    assert next_due_date("daily", "2026-07-09") == "2026-07-10"
    # 2026-07-09 is a Thursday; next Sunday is the 12th
    assert next_due_date("weekly:sun", "2026-07-09") == "2026-07-12"
    assert next_due_date("monthly:1", "2026-07-09") == "2026-08-01"


def test_archive_after_7_days(client):
    conn = _db()
    old = "2026-06-01"
    with conn:
        tid = create_task(conn, "Ancient", col="done")
        conn.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?", (old, tid))
    archive_old_done(conn)
    row = conn.execute("SELECT archived_at FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["archived_at"] is not None


def test_today_membership(client):
    conn = _db()
    today = today_iso()
    with conn:
        due = create_task(conn, "Due today", col="week", due_date=today)
        over = create_task(conn, "Overdue", col="week", due_date="2020-01-01")
        planned = create_task(conn, "Planned", col="backlog", planned_on=today)
        create_task(conn, "Someday", col="backlog")            # must NOT appear
    ids = {t["id"] for t in today_tasks(conn)}
    conn.close()
    assert due in ids and over in ids and planned in ids
    assert len(ids) == 3


def test_week_pool_excludes_today_overlap(client):
    """The Today 'This week' pool = open col='week' tasks NOT already on Today:
    future-due or undated week tasks appear; anything due today / overdue / planned
    today / done, plus backlog-column tasks, are excluded (no duplicate sections)."""
    conn = _db()
    today = today_iso()
    future = "2099-01-01"
    with conn:
        undated = create_task(conn, "Undated week", col="week")
        futured = create_task(conn, "Future week", col="week", due_date=future)
        parent = create_task(conn, "Parent week", col="week")
        create_task(conn, "sub", parent_id=parent)
        # overlaps with Today — must be excluded from the pool
        due = create_task(conn, "Due today", col="week", due_date=today)
        over = create_task(conn, "Overdue", col="week", due_date="2020-01-01")
        planned = create_task(conn, "Planned", col="week", planned_on=today)
        done = create_task(conn, "Done", col="week")
        conn.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?", (today, done))
        backlog = create_task(conn, "Backlog", col="backlog")  # wrong column
    ids = {t["id"] for t in week_tasks(conn)}
    conn.close()
    assert {undated, futured, parent} <= ids            # the week pool
    assert not ({due, over, planned, done, backlog} & ids)  # no Today/backlog overlap


def test_week_pool_hidden_when_empty(client):
    """Section is view-only and hidden when there's nothing to show: no `week` rows
    render, and today.html omits the 'This week' card entirely."""
    conn = _db()
    today = today_iso()
    with conn:
        # a single week task that IS on Today (planned) — pool should be empty
        create_task(conn, "On today", col="week", planned_on=today)
    assert week_tasks(conn) == []
    conn.close()
    assert 'class="card weekpool"' not in client.get("/").data.decode()


# ── capture routing ───────────────────────────────────────────────────────────
def test_capture_routing(client):
    conn = _db()
    assert route_capture(conn, "t: buy milk")["kind"] == "task"
    assert route_capture(conn, "n: a thought")["kind"] == "note"
    idea = route_capture(conn, "i: video angle")
    assert idea["kind"] == "note" and "idea" in idea["tags"]
    assert route_capture(conn, "j: felt good today")["kind"] == "journal"
    url = route_capture(conn, "https://instagram.com/reel/abc")
    assert url["kind"] == "note" and "link" in url["tags"] and "idea" in url["tags"]
    hi = route_capture(conn, "t: urgent thing!")
    conn.close()
    row = connect(os.environ["LIFEOS_DB_PATH"]).execute(
        "SELECT priority FROM tasks WHERE id=?", (hi["id"],)).fetchone()
    assert row["priority"] == "high"


def test_capture_endpoint(client):
    r = client.post("/capture", data={"text": "t: from web", "type": "auto"})
    assert r.status_code == 200 and r.get_json()["kind"] == "task"


# ── notes round-trip to disk ──────────────────────────────────────────────────
def test_note_file_roundtrip_edit_delete(client):
    r = client.post("/notes/new", data={"title": "Rate card", "body": "hello", "tags": "business, idea"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    slug = r.get_json()["slug"]
    path = os.path.join(vault_store.notes_dir(), slug + ".md")
    assert os.path.exists(path)                             # file created on disk
    raw = open(path).read()
    assert raw.startswith("---") and "title: Rate card" in raw and "business" in raw
    # edit
    client.post(f"/notes/{slug}/save", data={"title": "Rate card 2026", "body": "world", "tags": "business"})
    n = vault_store.read_note(slug)
    assert n["title"] == "Rate card 2026" and "world" in n["body"]
    # soft-delete moves file to .trash, restore brings it back
    client.post(f"/notes/{slug}/delete")
    assert not os.path.exists(path)
    client.post(f"/notes/{slug}/restore")
    assert os.path.exists(path)


# ── journal append ────────────────────────────────────────────────────────────
def test_journal_append_lands_in_file(client):
    today = today_iso()
    r = client.post("/journal/entry", data={"text": "ate chicken rice", "day": today},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    path = vault_store.journal_path(today)
    assert os.path.exists(path)
    raw = open(path).read()
    assert "ate chicken rice" in raw and raw.count("##") >= 1     # timestamped header
    page = vault_store.read_journal(today)
    assert page["entries"] and page["entries"][-1]["text"] == "ate chicken rice"


def test_capture_journal_via_router(client):
    conn = _db()
    route_capture(conn, "j: second entry via capture")
    conn.close()
    page = vault_store.read_journal(today_iso())
    assert any("second entry" in e["text"] for e in page["entries"])


# ── goals rollup math ─────────────────────────────────────────────────────────
def test_goal_rollup_and_number(client):
    from goals_core import goal_progress, current_period_start
    conn = _db()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('2 videos','month',?,'rollup',NULL,0,?)",
            (current_period_start("month"), now_iso()))
        gid = cur.lastrowid
        t1 = create_task(conn, "Video A", col="week", goal_id=gid)
        create_task(conn, "Video B", col="week", goal_id=gid)
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (t1,))
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    prog = goal_progress(conn, g)
    assert prog["done"] == 1 and prog["total"] == 2 and prog["pct"] == 50
    conn.close()
    # number goal update endpoint
    conn = _db()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('Newsletter','month',?,'number',500,438,?)",
            (current_period_start("month"), now_iso()))
        nid = cur.lastrowid
    conn.close()
    client.post(f"/goals/{nid}/update", data={"current": "450"})
    conn = _db()
    row = conn.execute("SELECT current_num FROM goals WHERE id=?", (nid,)).fetchone()
    conn.close()
    assert row["current_num"] == 450


def test_csrf_blocks_unprotected_post(client):
    """A raw client (no CSRF token) must be rejected on a mutating request.
    make_test_client() sets a CSRF-injecting client class globally, so restore the
    stock FlaskClient to prove the guard actually rejects a token-less POST."""
    from flask.testing import FlaskClient
    saved = web_core.app.test_client_class
    web_core.app.test_client_class = FlaskClient
    try:
        c = web_core.app.test_client()
        r = c.post("/capture", data={"text": "t: nope"})
    finally:
        web_core.app.test_client_class = saved
    assert r.status_code == 403
