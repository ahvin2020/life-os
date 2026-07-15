"""Suggest-then-confirm: the shared pending-action infra (router) + the weekly-review
suggestion + the daemon confirm fast path. A "yes" executes the armed action; a "no"
clears it; anything else routes normally."""

import os

import capture_daemon as cd
from ai import router, proactive
from domain.capture import create_task
from core.db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


class FakeTG:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text); return {"ok": True}
    def send_chat_action(self, *a, **k):
        return {"ok": True}


def test_affirmation_and_rejection_matchers():
    assert router.is_affirmation("yes") and router.is_affirmation("Yes please!")
    assert router.is_rejection("no") and router.is_rejection("nope")
    assert not router.is_affirmation("yesterday")


def test_pending_set_peek_clear_and_expiry(client):
    conn = _db()
    router.set_pending(conn, "archive_tasks", {"ids": [1, 2]})
    p = router.peek_pending(conn)
    assert p["kind"] == "archive_tasks" and p["payload"]["ids"] == [1, 2]
    router.clear_pending(conn)
    assert router.peek_pending(conn) is None
    conn.close()


def test_execute_archive_soft_deletes(client):
    conn = _db()
    with conn:
        a = create_task(conn, "Dead task A")
        b = create_task(conn, "Dead task B")
    msg = router.execute_pending(conn, {"kind": "archive_tasks", "payload": {"ids": [a, b]}})
    gone = conn.execute("SELECT COUNT(*) c FROM tasks WHERE deleted_at IS NOT NULL").fetchone()["c"]
    conn.close()
    assert gone == 2 and "Archived" in msg


def test_execute_archive_cascades_to_subtasks(client):
    """Archiving a parent must soft-delete its subtasks too — the cascade every other
    delete site does (`WHERE id=? OR parent_id=?`). This one wrote `WHERE id=?`, leaving
    children with deleted_at IS NULL: invisible (subtask reads filter by parent) but
    un-undoable, and live to the first subtask-aware query."""
    conn = _db()
    with conn:
        parent = create_task(conn, "Parent to archive")
        kid = create_task(conn, "Subtask of it", parent_id=parent)
        other = create_task(conn, "Untouched task")
    router.execute_pending(conn, {"kind": "archive_tasks", "payload": {"ids": [parent]}})
    rows = {r["id"]: r["deleted_at"] for r in
            conn.execute("SELECT id, deleted_at FROM tasks").fetchall()}
    conn.close()
    assert rows[parent] is not None                    # the parent went
    assert rows[kid] is not None, "subtask orphaned with deleted_at IS NULL"
    assert rows[other] is None                         # nothing else touched


def test_weekly_suggestion_archive_then_plan(client):
    conn = _db()
    with conn:
        t = create_task(conn, "Chronically postponed")
        conn.execute("UPDATE tasks SET reschedule_count=3, updated=? WHERE id=?",
                     ("2020-01-01T00:00:00Z", t))
    sug = proactive.weekly_suggestion(conn, today_iso())
    assert sug["kind"] == "archive_tasks" and t in sug["payload"]["ids"]
    conn.close()


def test_daemon_yes_executes_pending(client):
    conn = _db()
    with conn:
        t = create_task(conn, "Archive me")
    router.set_pending(conn, "archive_tasks", {"ids": [t]})
    tg = FakeTG()
    cd._handle_text_single(conn, tg, "123", "yes")
    deleted = conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (t,)).fetchone()["deleted_at"]
    cleared = router.peek_pending(conn) is None
    conn.close()
    assert deleted is not None                       # the "yes" archived it
    assert cleared                                   # pending consumed


def test_daemon_no_clears_pending(client):
    conn = _db()
    with conn:
        t = create_task(conn, "Keep me")
    router.set_pending(conn, "archive_tasks", {"ids": [t]})
    tg = FakeTG()
    cd._handle_text_single(conn, tg, "123", "no")
    still = conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (t,)).fetchone()["deleted_at"]
    assert still is None and router.peek_pending(conn) is None
    conn.close()
