"""Router v2 tests — the agentic `claude -p` entry point.

Covers JSON application for every action type (incl. multi + invalid-id → clarify),
the fallback path when claude fails, undo inverse ops + daemon callback handling,
and the raw-log safety rail. The model is always mocked (claude_fn) — no real calls.
"""

import json
import os

import router
import vault_store
from capture import create_task
from db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _fn(obj):
    """A fake claude_fn that always returns the given decision as JSON text."""
    return lambda prompt: json.dumps(obj)


def _open_task(conn, title, **kw):
    with conn:
        return create_task(conn, title, col=kw.pop("col", "week"), **kw)


# ── raw-log safety rail (written BEFORE the claude call) ──────────────────────
def test_raw_log_always_written(client, tmp_path, monkeypatch):
    logp = tmp_path / "capture_raw.log"
    monkeypatch.setattr(router, "_RAW_LOG", str(logp))
    conn = _db()
    # even if the model raises, the raw message must already be on disk
    router.route(conn, "rambling that will fall back",
                 claude_fn=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    conn.close()
    assert logp.exists()
    assert "rambling that will fall back" in logp.read_text()


# ── every action type ─────────────────────────────────────────────────────────
def test_create_task_with_subtasks(client):
    conn = _db()
    out = router.route(conn, "plan the Q3 video", claude_fn=_fn({
        "action": "create_task", "title": "Plan Q3 video", "due": None,
        "category": "content", "priority": "high", "subtasks": ["outline", "script"]}))
    row = conn.execute("SELECT * FROM tasks WHERE title='Plan Q3 video'").fetchone()
    subs = conn.execute("SELECT COUNT(*) FROM tasks WHERE parent_id=?", (row["id"],)).fetchone()[0]
    conn.close()
    assert out["reply"].startswith("⏰ Task: Plan Q3 video")
    assert row["category"] == "content" and row["priority"] == "high" and subs == 2


def test_create_note(client):
    conn = _db()
    out = router.route(conn, "interesting REIT thread", claude_fn=_fn({
        "action": "create_note", "title": "REIT thread", "tags": ["idea", "research"],
        "body": "worth revisiting"}))
    conn.close()
    assert out["reply"].startswith("📝 Note: REIT thread")
    note = [n for n in vault_store.list_notes() if n["title"] == "REIT thread"][0]
    assert "idea" in note["tags"] and "unsorted" not in note["tags"]


def test_append_journal(client):
    conn = _db()
    out = router.route(conn, "felt great after the gym", claude_fn=_fn({
        "action": "append_journal", "text": "Felt great after the gym today."}))
    conn.close()
    assert out["reply"] == "📝 → today's Journal"
    page = vault_store.read_journal(today_iso())
    assert page and any("gym" in e["text"] for e in page["entries"])


def test_complete_task_carries_undo(client):
    conn = _db()
    tid = _open_task(conn, "Publish CPF Life video")
    out = router.route(conn, "mark the cpf video done",
                       claude_fn=_fn({"action": "complete_task", "id": tid}))
    done = conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"]
    conn.close()
    assert out["reply"] == "✓ Done: Publish CPF Life video" and done == 1
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|comp|{tid}"


def test_uncomplete_task(client):
    conn = _db()
    tid = _open_task(conn, "Weekly review")
    from routes_tasks import complete_task
    with conn:
        complete_task(conn, tid, True)                          # now done + completed today
    out = router.route(conn, "actually reopen the weekly review",
                       claude_fn=_fn({"action": "uncomplete_task", "id": tid}))
    done = conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"]
    conn.close()
    assert done == 0 and out["reply"].startswith("↩ Reopened")


def test_plan_today_and_undo_keyboard(client):
    conn = _db()
    tid = _open_task(conn, "Edit the intro", col="backlog")
    out = router.route(conn, "do the intro edit today",
                       claude_fn=_fn({"action": "plan_today", "id": tid}))
    planned = conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()["planned_on"]
    conn.close()
    assert planned == today_iso()
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|plan|{tid}"


def test_unplan(client):
    conn = _db()
    tid = _open_task(conn, "Later thing")
    with conn:
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?", (today_iso(), tid))
    out = router.route(conn, "take the later thing off today",
                       claude_fn=_fn({"action": "unplan", "id": tid}))
    planned = conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()["planned_on"]
    conn.close()
    assert planned is None and "Removed from today" in out["reply"]


def test_set_due(client):
    conn = _db()
    tid = _open_task(conn, "Send the invoice")
    out = router.route(conn, "push the invoice to friday",
                       claude_fn=_fn({"action": "set_due", "id": tid, "date": "2026-07-10"}))
    due = conn.execute("SELECT due_date FROM tasks WHERE id=?", (tid,)).fetchone()["due_date"]
    conn.close()
    assert due == "2026-07-10" and out["reply"].startswith("⏰ Send the invoice — due")


def test_rename_task(client):
    conn = _db()
    tid = _open_task(conn, "vague title")
    out = router.route(conn, "rename it to Draft the sponsor reply",
                       claude_fn=_fn({"action": "rename_task", "id": tid, "title": "Draft the sponsor reply"}))
    title = conn.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()["title"]
    conn.close()
    assert title == "Draft the sponsor reply" and "Renamed" in out["reply"]


def test_move_task_undo_has_prev_col(client):
    conn = _db()
    tid = _open_task(conn, "Backlog item", col="backlog")
    out = router.route(conn, "move backlog item to this week",
                       claude_fn=_fn({"action": "move_task", "id": tid, "col": "week"}))
    col = conn.execute("SELECT col FROM tasks WHERE id=?", (tid,)).fetchone()["col"]
    conn.close()
    assert col == "week"
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|move|{tid}|backlog"


def test_delete_task_is_soft_with_undo(client):
    conn = _db()
    tid = _open_task(conn, "Drop me")
    out = router.route(conn, "drop the drop me task",
                       claude_fn=_fn({"action": "delete_task", "id": tid}))
    row = conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["deleted_at"] is not None                        # SOFT delete
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|del|{tid}"


def test_create_goal(client):
    conn = _db()
    out = router.route(conn, "goal: publish 8 videos this month", claude_fn=_fn({
        "action": "create_goal", "title": "8 videos", "period": "month",
        "kind": "number", "target": 8}))
    row = conn.execute("SELECT * FROM goals WHERE title='8 videos'").fetchone()
    conn.close()
    assert row and row["period"] == "month" and row["target_num"] == 8
    assert out["reply"].startswith("🎯 Goal")


def test_update_goal_number(client):
    conn = _db()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('Newsletter','month','2026-07-01','number',500,400,?)", (now_iso(),))
        gid = cur.lastrowid
    out = router.route(conn, "newsletter is at 460",
                       claude_fn=_fn({"action": "update_goal_number", "id": gid, "value": 460}))
    cur_num = conn.execute("SELECT current_num FROM goals WHERE id=?", (gid,)).fetchone()["current_num"]
    conn.close()
    assert cur_num == 460 and "460/500" in out["reply"]


def test_answer_action(client):
    conn = _db()
    out = router.route(conn, "how many videos this week?",
                       claude_fn=_fn({"action": "answer", "text": "You've shipped 2 videos this week."}))
    conn.close()
    assert out["reply"] == "You've shipped 2 videos this week."
    assert not vault_store.list_notes()                         # a question files nothing


def test_clarify_action(client):
    conn = _db()
    out = router.route(conn, "do the thing",
                       claude_fn=_fn({"action": "clarify", "question": "Which thing do you mean?"}))
    conn.close()
    assert out["reply"] == "❓ Which thing do you mean?"


def test_multi_compound_message(client):
    conn = _db()
    tid = _open_task(conn, "Publish CPF video")
    out = router.route(conn, "mark cpf done and remind me to invoice friday", claude_fn=_fn({
        "action": "multi", "actions": [
            {"action": "complete_task", "id": tid},
            {"action": "create_task", "title": "Send invoice", "due": "2026-07-10",
             "category": "business"}]}))
    done = conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"]
    inv = conn.execute("SELECT 1 FROM tasks WHERE title='Send invoice'").fetchone()
    conn.close()
    assert done == 1 and inv is not None
    assert "✓ Done" in out["reply"] and "Send invoice" in out["reply"]
    assert out["applied"] == ["complete_task", "create_task"]


# ── invalid id → clarify (never mutate the wrong row) ─────────────────────────
def test_invalid_task_id_becomes_clarify(client):
    conn = _db()
    _open_task(conn, "A real task")                             # id 1 exists; ask for 999
    out = router.route(conn, "mark task 999 done",
                       claude_fn=_fn({"action": "complete_task", "id": 999}))
    # nothing was completed
    done_any = conn.execute("SELECT COUNT(*) FROM tasks WHERE done=1").fetchone()[0]
    conn.close()
    assert done_any == 0 and out["reply"].startswith("❓")


def test_invalid_goal_id_becomes_clarify(client):
    conn = _db()
    out = router.route(conn, "set the goal to 5",
                       claude_fn=_fn({"action": "update_goal_number", "id": 42, "value": 5}))
    conn.close()
    assert out["reply"].startswith("❓")


# ── fallback path on claude failure ───────────────────────────────────────────
def test_fallback_on_exception(client):
    conn = _db()
    out = router.route(conn, "a lost thought",
                       claude_fn=lambda p: (_ for _ in ()).throw(TimeoutError("timeout")))
    conn.close()
    assert out["fell_back"] and out["reply"] == router.FALLBACK_REPLY
    unsorted = [n for n in vault_store.list_notes() if "unsorted" in n["tags"]]
    assert any("lost thought" in n["body"] for n in unsorted)   # input preserved as #unsorted


def test_fallback_on_invalid_json_after_retry(client):
    conn = _db()
    calls = []

    def _garbage(p):
        calls.append(1)
        return "sorry, I can't do that as JSON"

    out = router.route(conn, "another lost thought", claude_fn=_garbage)
    conn.close()
    assert len(calls) == 2                                      # one retry before giving up
    assert out["fell_back"]


# ── undo inverse operations ───────────────────────────────────────────────────
def test_handle_callback_inverses(client):
    conn = _db()
    from routes_tasks import complete_task
    tid = _open_task(conn, "Undo target")

    # complete → undo reopens
    with conn:
        complete_task(conn, tid, True)
    assert "reopened" in router.handle_callback(conn, f"u|comp|{tid}").lower()
    assert conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"] == 0

    # soft delete → undo restores
    with conn:
        conn.execute("UPDATE tasks SET deleted_at=? WHERE id=?", (now_iso(), tid))
    assert "restored" in router.handle_callback(conn, f"u|del|{tid}").lower()
    assert conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (tid,)).fetchone()["deleted_at"] is None

    # plan → undo unplans
    with conn:
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?", (today_iso(), tid))
    router.handle_callback(conn, f"u|plan|{tid}")
    assert conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()["planned_on"] is None

    # move → undo restores previous column
    with conn:
        conn.execute("UPDATE tasks SET col='week' WHERE id=?", (tid,))
    router.handle_callback(conn, f"u|move|{tid}|backlog")
    assert conn.execute("SELECT col FROM tasks WHERE id=?", (tid,)).fetchone()["col"] == "backlog"
    conn.close()


def test_handle_callback_garbage_is_safe(client):
    conn = _db()
    assert router.handle_callback(conn, "not-a-callback") == "Nothing to undo."
    assert router.handle_callback(conn, "") == "Nothing to undo."
    conn.close()


# ── daemon callback wiring ────────────────────────────────────────────────────
def test_daemon_process_callback(client):
    import capture_daemon as cd
    from tests.test_phase2 import FakeTelegram
    from routes_tasks import complete_task
    conn = _db()
    tid = _open_task(conn, "Callback task")
    with conn:
        complete_task(conn, tid, True)
    tg = FakeTelegram()
    upd = {"update_id": 5, "callback_query": {
        "id": "cbq1", "from": {"id": 12345678}, "data": f"u|comp|{tid}",
        "message": {"message_id": 88, "chat": {"id": 12345678}}}}
    cd._process_callback(conn, tg, "12345678", upd)
    reopened = conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"]
    conn.close()
    assert reopened == 0                                        # inverse applied
    assert tg.answered and tg.answered[-1][0] == "cbq1"         # tap acknowledged
    assert tg.edited and tg.edited[-1] == (12345678, 88)       # spent button removed


def test_daemon_callback_rejects_unauthorised(client):
    import capture_daemon as cd
    from tests.test_phase2 import FakeTelegram
    conn = _db()
    tid = _open_task(conn, "Protected task")
    tg = FakeTelegram()
    upd = {"update_id": 6, "callback_query": {
        "id": "cbq2", "from": {"id": 999}, "data": f"u|del|{tid}",
        "message": {"message_id": 9, "chat": {"id": 999}}}}
    cd._process_callback(conn, tg, "12345678", upd)
    conn.close()
    assert tg.answered == []                                    # ignored, no action
