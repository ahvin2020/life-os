"""Timed reminders: the router set_reminder action stores a UTC fire_at, and the daemon
tick pushes due ones via Telegram and marks them fired."""

import os

from core.db import connect, now_iso
from ai import router
import capture_daemon


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


class _FakeTG:
    def __init__(self): self.sent = []
    def send_message(self, chat, text, reply_markup=None): self.sent.append((chat, text))


def test_set_reminder_stores_utc_row(client):
    conn = _db()
    reply, _ = router.apply_action(
        conn, {"action": "set_reminder", "text": "fix bike", "fire_at": "2026-07-14T11:30"},
        {"today": "2026-07-14"})
    row = conn.execute("SELECT text, fire_at, fired_at FROM reminders").fetchone()
    conn.close()
    assert "Reminder set" in reply and "fix bike" in reply
    assert row["text"] == "fix bike" and row["fired_at"] is None
    assert row["fire_at"].endswith("Z")            # normalised to UTC for the daemon


def test_set_reminder_needs_text_and_time(client):
    conn = _db()
    reply, _ = router.apply_action(conn, {"action": "set_reminder", "text": "", "fire_at": ""},
                                   {"today": "2026-07-14"})
    conn.close()
    assert "❓" in reply


def test_daemon_fires_due_reminder(client):
    conn = _db()
    conn.execute("INSERT INTO reminders (text, fire_at, created, fired_at) VALUES (?,?,?,NULL)",
                 ("call the bank", "2020-01-01T00:00:00Z", now_iso()))
    conn.commit()
    tg = _FakeTG()
    n = capture_daemon.maybe_fire_reminders(conn, tg, "999")
    row = conn.execute("SELECT fired_at FROM reminders").fetchone()
    conn.close()
    assert n == 1 and tg.sent == [("999", "⏰ Reminder: call the bank")]
    assert row["fired_at"] is not None             # not re-sent next tick


def test_daemon_skips_future_reminder(client):
    conn = _db()
    conn.execute("INSERT INTO reminders (text, fire_at, created, fired_at) VALUES (?,?,?,NULL)",
                 ("later", "2099-01-01T00:00:00Z", now_iso()))
    conn.commit()
    tg = _FakeTG()
    n = capture_daemon.maybe_fire_reminders(conn, tg, "999")
    conn.close()
    assert n == 0 and tg.sent == []
