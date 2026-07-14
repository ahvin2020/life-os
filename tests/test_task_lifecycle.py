"""Task lifecycle — comprehensive coverage of the board/Today machinery, so UI
regressions surface in pytest, not on Sam's phone.

Failure classes covered:
  1. STATE-MACHINE INVARIANTS — after ANY endpoint mutation a task row must be
     coherent (done ⟺ col='done'+completed_at; week_since ⟺ open-in-week;
     ☀-on-today never parked in backlog).
  2. ACTION MATRIX — every (seed × action × action) ordered pair runs through
     the REAL endpoints and re-checks the invariants after each step.
  3. SURFACE AGREEMENT — Today list, board pinned group, Telegram digest, AI
     brief, and the rendered pages must all agree on what "today" means.
  4. HOSTILE INPUTS — garbage ids/payloads never 500 and never corrupt state.
  5. SUBTASK ↔ PARENT — auto-complete/reopen reconciliation stays coherent.
  6. RECURRENCE — respawn rules, future-landing overdue respawns, subtask carry.
  7. ARCHIVE / PURGE — done rows leave every surface after 7 days but stay
     queryable; soft-deleted rows purge after 30.
  8. ORDERING — drag order persists exactly; new tasks land at the bottom.
"""

import itertools
import os
import re
from datetime import date, timedelta

from core import db_init  # noqa: F401  (ensures path set up by conftest import order)
from domain.capture import create_task
from core.db import connect, today_iso

XHR = {"X-Requested-With": "XMLHttpRequest"}


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _days(n):
    return (date.fromisoformat(today_iso()) + timedelta(days=n)).isoformat()


# ── class 1: invariants ───────────────────────────────────────────────────────
def assert_task_invariants(conn):
    """Every live top-level task must be in a coherent state — the exact
    contradictions that reached the screen before (fake-Done, zombie
    done+planned, planned-but-in-backlog, dead staleness clocks)."""
    today = today_iso()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id IS NULL AND deleted_at IS NULL "
        "AND archived_at IS NULL").fetchall()
    for r in rows:
        ctx = (f"task {r['id']} '{r['title']}' col={r['col']} done={r['done']} "
               f"planned={r['planned_on']} week_since={r['week_since']}")
        if r["done"]:
            assert r["col"] == "done", f"done task not in Done column: {ctx}"
            assert r["completed_at"], f"done without completed_at (fake-Done): {ctx}"
            assert r["week_since"] is None, f"done task keeps the week clock: {ctx}"
        else:
            assert r["col"] in ("backlog", "week"), f"open task in Done column: {ctx}"
            assert r["completed_at"] is None, f"open task with completed_at: {ctx}"
            if r["col"] == "week":
                assert r["week_since"], f"in week without a staleness clock: {ctx}"
            else:
                assert r["week_since"] is None, f"backlog with a week clock: {ctx}"
                planned = r["planned_on"] and r["planned_on"] <= today
                assert not planned, f"☀-planned task parked in backlog: {ctx}"


# ── class 2: exhaustive action matrix ─────────────────────────────────────────
SEEDS = {
    "backlog plain":  dict(col="backlog"),
    "week plain":     dict(col="week"),
    "due today":      dict(col="week", due_date="TODAY"),
    "overdue":        dict(col="week", due_date="D-3"),
    "planned today":  dict(col="week", planned_on="TODAY"),
    "rolled over":    dict(col="week", planned_on="D-2"),
    "recurring":      dict(col="week", due_date="TODAY", recur_rule="daily"),
}


def _mk_seed(conn, name):
    kw = dict(SEEDS[name])
    for k in ("due_date", "planned_on"):
        if kw.get(k) == "TODAY":
            kw[k] = today_iso()
        elif isinstance(kw.get(k), str) and kw[k].startswith("D-"):
            kw[k] = _days(-int(kw[k][2:]))
    with conn:
        return create_task(conn, f"matrix {name}", **kw)


def _actions(client):
    return {
        "plan_toggle":  lambda tid: client.post(f"/tasks/{tid}/plan"),
        "complete":     lambda tid: client.post(f"/tasks/{tid}/complete", data={"done": "1"}),
        "uncomplete":   lambda tid: client.post(f"/tasks/{tid}/complete", data={"done": "0"}),
        "edit_done":    lambda tid: client.post(f"/tasks/{tid}/edit", data={"col": "done"}, headers=XHR),
        "edit_week":    lambda tid: client.post(f"/tasks/{tid}/edit", data={"col": "week"}, headers=XHR),
        "edit_backlog": lambda tid: client.post(f"/tasks/{tid}/edit", data={"col": "backlog"}, headers=XHR),
        "drag_done":    lambda tid: client.post("/tasks/reorder", json={"col": "done", "ids": [tid]}),
        "drag_week":    lambda tid: client.post("/tasks/reorder", json={"col": "week", "ids": [tid]}),
        "drag_backlog": lambda tid: client.post("/tasks/reorder", json={"col": "backlog", "ids": [tid]}),
    }


def test_every_action_pair_from_every_seed_keeps_invariants(client):
    """~570 (seed, action, action) sequences through the real endpoints. Any
    incoherent intermediate or final state fails with the exact sequence named."""
    acts = _actions(client)
    for seed_name in SEEDS:
        for a1, a2 in itertools.product(acts, repeat=2):
            conn = _db()
            tid = _mk_seed(conn, seed_name)
            conn.close()
            for step, a in (("1:" + a1, a1), ("2:" + a2, a2)):
                r = acts[a](tid)
                assert r.status_code < 400, \
                    f"[{seed_name}] → {a1} → {a2}: HTTP {r.status_code} @{step}"
                conn = _db()
                try:
                    assert_task_invariants(conn)
                except AssertionError as e:
                    raise AssertionError(
                        f"seq [{seed_name}] → {a1} → {a2} broke at {step}: {e}") from e
                finally:
                    conn.close()


# ── class 3: cross-surface agreement ──────────────────────────────────────────
def _mixture(conn):
    """One task per today-relevant state; returns the set that IS on today."""
    with conn:
        create_task(conn, "MIX due today", col="week", due_date=today_iso())
        create_task(conn, "MIX overdue", col="week", due_date=_days(-2))
        create_task(conn, "MIX planned today", col="week", planned_on=today_iso())
        create_task(conn, "MIX rolled over", col="week", planned_on=_days(-2))
        create_task(conn, "MIX plain week", col="week")
        create_task(conn, "MIX plain backlog", col="backlog")
        create_task(conn, "MIX future due", col="week", due_date=_days(3))
    return {"MIX due today", "MIX overdue", "MIX planned today", "MIX rolled over"}


def _board_columns(html):
    cols = re.split(r'<div class="col" data-col="(\w+)">', html)
    return dict(zip(cols[1::2], cols[2::2]))


def test_all_surfaces_agree_on_today(client):
    """The class of bug where the app shows a task on Today but the digest /
    brief / board silently disagree — one mixture, five surfaces, one set."""
    conn = _db()
    expected = _mixture(conn)

    from domain.tasks_core import today_tasks
    assert {t["title"] for t in today_tasks(conn)} == expected, "today_tasks drifted"

    from ai.proactive import _digest_tasks
    assert {r["title"] for r in _digest_tasks(conn, today_iso())} == expected, \
        "morning digest drifted"

    from ai.proactive import build_brief_context
    assert {t["title"] for t in build_brief_context(conn, day=today_iso())["tasks"]} \
        == expected, "AI brief drifted"
    conn.close()

    # board render: pinned group == the on-today set, and nowhere else
    cols = _board_columns(client.get("/tasks").data.decode())
    pinned = set(re.findall(r'class="kcard pinned"[^>]*data-title="([^"]*)"',
                            cols["week"]))
    assert pinned == expected, "board pinned group drifted"
    assert "kcard pinned" not in cols["backlog"] and "kcard pinned" not in cols["done"]
    assert "On today ✓" not in cols["backlog"], "on-today pill leaked into Backlog"

    # home render: hero shows exactly the on-today set; the pool shows the rest
    # of the week and nothing else (bounded before the right-column cards, which
    # contain the captured-today feed)
    home = client.get("/").data.decode()
    lcol = home.split('class="card hero"')[1].split('class="card sub2"')[0]
    hero = lcol.split('class="card weekpool"')[0]
    pool = lcol.split('class="card weekpool"')[1] if 'card weekpool' in lcol else ""
    for title in expected:
        assert title in hero, f"{title} missing from Today hero"
        assert title not in pool, f"{title} leaked into the week pool"
    for title in ("MIX plain week", "MIX future due"):
        assert title in pool and title not in hero, f"{title} misplaced"
    assert "MIX plain backlog" not in hero and "MIX plain backlog" not in pool


def test_board_column_counts_match_cards(client):
    conn = _db()
    _mixture(conn)
    conn.close()
    cols = _board_columns(client.get("/tasks").data.decode())
    for name, body in cols.items():
        count = int(re.search(r'<span class="count">(\d+)</span>', body).group(1))
        cards = len(re.findall(r'class="kcard', body))
        assert count == cards, f"{name} count badge says {count}, renders {cards}"


def test_board_data_planned_attr_matches_db(client):
    """data-planned feeds the task editor's ☀ state — it must reflect sticky
    membership (a rolled-over plan is still 'on today')."""
    conn = _db()
    _mixture(conn)
    conn.close()
    html = client.get("/tasks").data.decode()

    def attr(title):
        m = re.search(r'data-title="%s"[^>]*data-planned="(\d)"' % re.escape(title),
                      html)
        return m.group(1) if m else None

    assert attr("MIX planned today") == "1"
    assert attr("MIX rolled over") == "1"
    assert attr("MIX plain week") == "0"
    assert attr("MIX due today") == "0"          # on today via date, not ☀


# ── class 4: hostile inputs ───────────────────────────────────────────────────
def test_garbage_inputs_never_500_or_corrupt(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "victim", col="week")
    conn.close()
    attempts = [
        lambda: client.post("/tasks/999999/plan"),
        lambda: client.post("/tasks/999999/complete", data={"done": "1"}),
        lambda: client.post("/tasks/999999/edit", data={"col": "week"}, headers=XHR),
        lambda: client.post("/tasks/999999/delete"),
        lambda: client.post("/tasks/999999/restore"),
        lambda: client.post("/tasks/reorder", json={"col": "nonsense", "ids": [tid]}),
        lambda: client.post("/tasks/reorder", json={"col": "week", "ids": ["abc", None, tid]}),
        lambda: client.post("/tasks/reorder", json={}),
        lambda: client.post("/tasks/reorder", data="not json"),
        lambda: client.post("/tasks/new", data={"title": ""}, headers=XHR),
        lambda: client.post("/tasks/new", data={"title": "ok", "col": "bogus"}, headers=XHR),
        lambda: client.post(f"/tasks/{tid}/edit", data={}, headers=XHR),
        lambda: client.post(f"/tasks/{tid}/edit", data={"col": "bogus"}, headers=XHR),
    ]
    for i, go in enumerate(attempts):
        r = go()
        assert r.status_code < 500, f"attempt {i} returned {r.status_code}"
    conn = _db()
    assert_task_invariants(conn)
    row = conn.execute("SELECT col, done FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["col"] == "week" and row["done"] == 0     # victim untouched


# ── class 5: subtask ↔ parent reconciliation ──────────────────────────────────
def test_subtask_flow_reconciles_parent_coherently(client):
    conn = _db()
    with conn:
        pid = create_task(conn, "Parent video", col="week")
        s1 = create_task(conn, "record", parent_id=pid)
        s2 = create_task(conn, "edit", parent_id=pid)
    conn.close()
    client.post(f"/tasks/{s1}/complete", data={"done": "1"})
    conn = _db()
    assert_task_invariants(conn)
    assert conn.execute("SELECT done FROM tasks WHERE id=?", (pid,)).fetchone()["done"] == 0
    conn.close()
    client.post(f"/tasks/{s2}/complete", data={"done": "1"})   # last one → parent done
    conn = _db()
    assert_task_invariants(conn)
    p = conn.execute("SELECT done, col, completed_at FROM tasks WHERE id=?", (pid,)).fetchone()
    assert p["done"] == 1 and p["col"] == "done" and p["completed_at"]
    conn.close()
    client.post(f"/tasks/{s1}/complete", data={"done": "0"})   # reopens parent
    conn = _db()
    assert_task_invariants(conn)
    p = conn.execute("SELECT done, col, week_since FROM tasks WHERE id=?", (pid,)).fetchone()
    conn.close()
    assert p["done"] == 0 and p["col"] == "week" and p["week_since"]


def test_parent_delete_and_restore_cascade_to_subtasks(client):
    conn = _db()
    with conn:
        pid = create_task(conn, "Cascade parent", col="week")
        sid = create_task(conn, "cascade sub", parent_id=pid)
    conn.close()
    client.post(f"/tasks/{pid}/delete")
    conn = _db()
    dels = [r["deleted_at"] for r in conn.execute(
        "SELECT deleted_at FROM tasks WHERE id IN (?, ?)", (pid, sid))]
    conn.close()
    assert all(dels), "subtask not soft-deleted with its parent"
    client.post(f"/tasks/{pid}/restore")
    conn = _db()
    dels = [r["deleted_at"] for r in conn.execute(
        "SELECT deleted_at FROM tasks WHERE id IN (?, ?)", (pid, sid))]
    conn.close()
    assert not any(dels), "restore did not bring the subtask back"


# ── class 6: recurrence ───────────────────────────────────────────────────────
def test_next_due_date_rules(client):
    from domain.tasks_core import next_due_date
    fri = "2026-07-10"                                   # a Friday
    assert next_due_date("daily", fri) == "2026-07-11"
    assert next_due_date("weekly:mon", fri) == "2026-07-13"
    assert next_due_date("weekly:fri", fri) == "2026-07-17"   # strictly future
    assert next_due_date("monthly:5", fri) == "2026-08-05"


def test_overdue_recurring_respawn_lands_in_the_future_with_subtasks(client):
    conn = _db()
    with conn:
        pid = create_task(conn, "Water plants", col="week",
                          due_date=_days(-5), recur_rule="daily")
        create_task(conn, "refill can", parent_id=pid)
    conn.close()
    client.post(f"/tasks/{pid}/complete", data={"done": "1"})
    conn = _db()
    assert_task_invariants(conn)
    respawn = conn.execute(
        "SELECT * FROM tasks WHERE title='Water plants' AND done=0 "
        "AND parent_id IS NULL").fetchone()
    assert respawn is not None
    assert respawn["due_date"] == _days(1), "overdue respawn must land in the future"
    assert respawn["col"] == "week" and respawn["week_since"]
    subs = conn.execute("SELECT done FROM tasks WHERE parent_id=?",
                        (respawn["id"],)).fetchall()
    conn.close()
    assert len(subs) == 1 and subs[0]["done"] == 0       # carried over, unchecked


# ── class 7: archive / purge lifecycle ────────────────────────────────────────
def test_done_archives_after_7_days_and_leaves_every_surface(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Old glory", col="week")
    conn.close()
    client.post(f"/tasks/{tid}/complete", data={"done": "1"})
    conn = _db()
    with conn:
        conn.execute("UPDATE tasks SET completed_at=? WHERE id=?", (_days(-8), tid))
    conn.close()
    board = client.get("/tasks").data.decode()          # triggers archive_old_done
    assert "Old glory" not in board
    # home: gone from the task lists (it stays in the captured-today feed, which
    # is a record of what was captured, not of what's open)
    home_lists = client.get("/").data.decode().split(
        'class="card hero"')[1].split('class="card sub2"')[0]
    assert "Old glory" not in home_lists
    conn = _db()
    row = conn.execute("SELECT archived_at FROM tasks WHERE id=?", (tid,)).fetchone()
    from ai.proactive import _digest_tasks
    assert "Old glory" not in {r["title"] for r in _digest_tasks(conn, today_iso())}
    conn.close()
    assert row is not None and row["archived_at"], "archived rows stay queryable"


def test_soft_deleted_purges_after_30_days(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Long forgotten", col="backlog")
    conn.close()
    client.post(f"/tasks/{tid}/delete")
    conn = _db()
    with conn:
        conn.execute("UPDATE tasks SET deleted_at=? WHERE id=?",
                     (_days(-31) + "T00:00:00Z", tid))
    conn.close()
    client.get("/tasks")                                 # triggers purge_deleted
    conn = _db()
    row = conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row is None, "purge window elapsed but the row survived"


# ── class 8: ordering ─────────────────────────────────────────────────────────
def test_reorder_persists_exact_order(client):
    conn = _db()
    with conn:
        a = create_task(conn, "order a", col="week")
        b = create_task(conn, "order b", col="week")
        c = create_task(conn, "order c", col="week")
    conn.close()
    client.post("/tasks/reorder", json={"col": "week", "ids": [c, a, b]})
    conn = _db()
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE col='week' AND parent_id IS NULL "
        "ORDER BY sort_order, id")]
    conn.close()
    assert ids == [c, a, b]


def test_captured_task_lands_at_the_top_of_week(client):
    """Bot/composer captures surface at the TOP of This week — "I just sent
    this" must be the first thing seen, not buried under the column."""
    from domain.capture import route_capture
    conn = _db()
    with conn:
        create_task(conn, "old week 1", col="week")
        create_task(conn, "old week 2", col="week")
    res = route_capture(conn, "t: urgent capture")
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE col='week' AND parent_id IS NULL "
        "ORDER BY sort_order, id")]
    conn.close()
    assert ids[0] == res["id"]


def test_new_task_lands_at_the_bottom(client):
    conn = _db()
    with conn:
        create_task(conn, "first", col="week")
        create_task(conn, "second", col="week")
    conn.close()
    r = client.post("/tasks/new", data={"title": "third", "col": "week"}, headers=XHR)
    tid = r.get_json()["id"]
    conn = _db()
    ids = [x["id"] for x in conn.execute(
        "SELECT id FROM tasks WHERE col='week' AND parent_id IS NULL "
        "ORDER BY sort_order, id")]
    conn.close()
    assert ids[-1] == tid


# ── undo inverses ─────────────────────────────────────────────────────────────
def test_complete_undo_roundtrip_restores_state_including_respawn(client):
    """The JS undo = uncomplete + soft-delete the respawn; the result must equal
    the original state (open, in week, clock running, no stray copy)."""
    conn = _db()
    with conn:
        tid = create_task(conn, "Undo recurring", col="week",
                          due_date=today_iso(), recur_rule="daily")
    conn.close()
    respawn = client.post(f"/tasks/{tid}/complete",
                          data={"done": "1"}).get_json()["respawned"]
    client.post(f"/tasks/{tid}/complete", data={"done": "0"})
    client.post(f"/tasks/{respawn}/delete")
    conn = _db()
    assert_task_invariants(conn)
    row = conn.execute("SELECT done, col, week_since FROM tasks WHERE id=?",
                       (tid,)).fetchone()
    live = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE title='Undo recurring' "
        "AND deleted_at IS NULL").fetchone()["c"]
    conn.close()
    assert row["done"] == 0 and row["col"] == "week" and row["week_since"]
    assert live == 1                                     # the respawn is gone (soft)


def test_plan_unplan_roundtrip_from_backlog(client):
    """backlog → ☀ on → ☀ off must land in This week (promoted, never demoted
    back), with exactly one postpone counted."""
    conn = _db()
    with conn:
        tid = create_task(conn, "Roundtrip", col="backlog")
    conn.close()
    client.post(f"/tasks/{tid}/plan")
    client.post(f"/tasks/{tid}/plan")
    conn = _db()
    assert_task_invariants(conn)
    row = conn.execute(
        "SELECT col, planned_on, reschedule_count, week_since FROM tasks WHERE id=?",
        (tid,)).fetchone()
    conn.close()
    assert row["col"] == "week" and row["planned_on"] is None
    assert row["reschedule_count"] == 1 and row["week_since"] == today_iso()


def test_editor_demote_to_backlog_unplans(client):
    """The editor's column select must obey the same rule as the board drag:
    Backlog means off-Today (☀ cleared), never a planned task hiding in backlog."""
    conn = _db()
    with conn:
        tid = create_task(conn, "Editor demote", col="week", planned_on=today_iso())
    conn.close()
    client.post(f"/tasks/{tid}/edit", data={"col": "backlog"}, headers=XHR)
    conn = _db()
    assert_task_invariants(conn)
    row = conn.execute("SELECT col, planned_on FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["col"] == "backlog" and row["planned_on"] is None
