"""Bug-hunt regression tests — backend date/recurrence logic + the deleted_at rule.

Companion to the two bugs Kelvin already hit (TZ mismatch, client/server contract).
Each test reproduces a distinct backend defect and fails on the pre-fix code.
"""

import os
from datetime import date, timedelta

from core import db_init  # noqa: F401  (path set up by conftest import order)
from domain.capture import create_task
from domain.tasks_core import complete_task
from routes.journal import today_so_far
from core.db import connect, today_iso, now_iso

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _iso(d):
    return d.isoformat()


# ── BUG 1: completing an OVERDUE recurring task respawns it already-overdue ──────
# _respawn_recurring based next_due on the OLD due_date, so a long-overdue daily/
# weekly task came back with a due_date still in the past (reappears as overdue the
# instant you finish it). The respawn must always land strictly in the future.
def test_overdue_daily_recurring_respawns_in_future(client):
    conn = _db()
    today = date.fromisoformat(today_iso())
    old_due = _iso(today - timedelta(days=9))          # long overdue
    with conn:
        create_task(conn, "Overdue daily", col="week",
                    due_date=old_due, recur_rule="daily")
        tid = conn.execute(
            "SELECT id FROM tasks WHERE title='Overdue daily'").fetchone()["id"]
        complete_task(conn, tid, True)
    rows = conn.execute(
        "SELECT due_date FROM tasks WHERE title='Overdue daily' AND done=0").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["due_date"] >= today_iso(), rows[0]["due_date"]


def test_overdue_weekly_recurring_respawns_in_future(client):
    conn = _db()
    today = date.fromisoformat(today_iso())
    weekday = _WEEKDAYS[today.weekday()]
    old_due = _iso(today - timedelta(days=14))         # two cycles overdue
    with conn:
        create_task(conn, "Overdue weekly", col="week",
                    due_date=old_due, recur_rule=f"weekly:{weekday}")
        tid = conn.execute(
            "SELECT id FROM tasks WHERE title='Overdue weekly'").fetchone()["id"]
        complete_task(conn, tid, True)
    rows = conn.execute(
        "SELECT due_date FROM tasks WHERE title='Overdue weekly' AND done=0").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["due_date"] >= today_iso(), rows[0]["due_date"]


def test_on_time_daily_recurring_unchanged(client):
    """Guard: completing a due-today recurring task still lands tomorrow (no regression)."""
    conn = _db()
    tomorrow = _iso(date.fromisoformat(today_iso()) + timedelta(days=1))
    with conn:
        create_task(conn, "On-time daily", col="week",
                    due_date=today_iso(), recur_rule="daily")
        tid = conn.execute(
            "SELECT id FROM tasks WHERE title='On-time daily'").fetchone()["id"]
        complete_task(conn, tid, True)
    row = conn.execute(
        "SELECT due_date FROM tasks WHERE title='On-time daily' AND done=0").fetchone()
    conn.close()
    assert row["due_date"] == tomorrow


# ── BUG 2: today_so_far counted a completed-then-DELETED task ────────────────────
# Every task query must filter deleted_at IS NULL (CLAUDE.md). The journal's
# "today so far" completed list didn't, so a soft-deleted task still showed.
def test_today_so_far_excludes_deleted_completed(client):
    conn = _db()
    today = today_iso()
    with conn:
        create_task(conn, "Done then deleted", col="week")
        tid = conn.execute(
            "SELECT id FROM tasks WHERE title='Done then deleted'").fetchone()["id"]
        complete_task(conn, tid, True)
        conn.execute("UPDATE tasks SET deleted_at=? WHERE id=?", (now_iso(), tid))
    tsf = today_so_far(conn, today)
    conn.close()
    titles = [c["title"] for c in tsf["completed"]]
    assert "Done then deleted" not in titles, titles


# ── BUG 3: voice handler leaked its mkdtemp scratch dir every note ───────────────
# _handle_voice created a tempfile.mkdtemp per voice note and never removed it, so
# every voice message left an (in.oga + in.wav) dir behind. A try/finally now purges
# it on every exit path — success, silence, and transcription failure.
def test_voice_handler_cleans_tmpdir(client, monkeypatch):
    import capture_daemon as cd

    created = []
    real_mkdtemp = cd.tempfile.mkdtemp

    def _spy_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        created.append(d)
        return d

    monkeypatch.setattr(cd.tempfile, "mkdtemp", _spy_mkdtemp)
    monkeypatch.setattr(cd, "oga_to_wav", lambda o, w: w)
    monkeypatch.setattr(cd, "transcribe_wav", lambda *a, **k: "buy milk tomorrow")
    monkeypatch.setattr(cd, "_preserve_audio", lambda p: None)

    from ai import router
    monkeypatch.setattr(router, "route",
                        lambda *a, **k: {"reply": "ok", "keyboard": None, "fell_back": False})

    class _FakeTg:
        def get_file_path(self, fid):
            return "voice/x.oga"

        def download_file(self, fpath, dest):
            with open(dest, "wb") as f:
                f.write(b"x")
            return dest

        def send_message(self, *a, **k):
            pass

        def send_chat_action(self, *a, **k):
            pass

    conn = _db()
    msg = {"voice": {"file_id": "abc"}}
    cd._handle_voice(conn, _FakeTg(), msg, chat_id=1)
    conn.close()

    assert created, "mkdtemp was not called"
    for d in created:
        assert not os.path.exists(d), f"leaked tmpdir {d}"
