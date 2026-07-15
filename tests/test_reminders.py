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


def test_set_reminder_surfaces_row_for_live_splice(client):
    # the web composer drops the new reminder straight into the strip (no toast/reload),
    # so apply_action must hand the created row back through ctx
    conn = _db()
    ctx = {"today": "2026-07-14"}
    router.apply_action(
        conn, {"action": "set_reminder", "text": "fix bike", "fire_at": "2026-07-14T11:30"}, ctx)
    conn.close()
    r = ctx.get("created_reminder")
    assert r and r["text"] == "fix bike"
    assert r["id"] and r["fire_at"].endswith("Z") and r["label"]


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


# ── dashboard add / dismiss / restore (the web twin of the bot) ──────────────────

def test_web_add_reminder_shows_on_today(client):
    r = client.post("/reminders", data={"text": "call the bank", "at": "2099-12-25T15:00"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["status"] == "ok" and j["text"] == "call the bank"
    assert j["fire_at"].endswith("Z") and "15:00" in j["label"]
    # the pending reminder is rendered on Today
    home = client.get("/").get_data(as_text=True)
    assert "call the bank" in home and j["label"] in home


def test_reminder_label_follows_time_format(client):
    """The strip + alarm modal show the clock in the user's chosen format (settings
    `time_format`), matching the rest of the app — not a hardcoded 24h."""
    from core.db import reload_time_format
    from domain.reminders import create_reminder, reminder_label
    conn = _db()
    r = create_reminder(conn, "test", "2099-12-25T13:55")
    assert r["label"].endswith("13:55")                  # 24h is the default
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('time_format','12h')")
    conn.commit()
    reload_time_format()
    try:
        assert reminder_label(r["fire_at"]).endswith("1:55pm")
        # on-the-hour drops the ':00', same convention as the calendar agenda
        r2 = create_reminder(conn, "test2", "2099-12-25T06:00")
        assert reminder_label(r2["fire_at"]).endswith("6am")
    finally:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('time_format','24h')")
        conn.commit(); reload_time_format(); conn.close()


def test_web_add_reminder_rejects_bad_input(client):
    assert client.post("/reminders", data={"text": "", "at": ""}).status_code == 400
    assert client.post("/reminders", data={"text": "x", "at": "nonsense"}).status_code == 400


def test_web_dismiss_then_restore(client):
    add = client.post("/reminders", data={"text": "gym", "at": "2099-01-02T09:00"}).get_json()
    rid = add["id"]
    dis = client.post(f"/reminders/{rid}/dismiss")
    assert dis.status_code == 200
    dj = dis.get_json()
    assert dj["text"] == "gym" and dj["fire_at"] == add["fire_at"]
    # gone from the pending list
    conn = _db()
    assert conn.execute("SELECT 1 FROM reminders WHERE id=? AND fired_at IS NULL",
                        (rid,)).fetchone() is None
    conn.close()
    # undo re-inserts it verbatim
    res = client.post("/reminders/restore", data={"text": dj["text"], "fire_at": dj["fire_at"]})
    assert res.status_code == 200 and res.get_json()["label"] == add["label"]


def test_web_dismiss_missing_is_404(client):
    assert client.post("/reminders/999/dismiss").status_code == 404


def test_capture_returns_reminder_for_live_splice(client, monkeypatch):
    """A composer-added reminder comes back on the JSON so the strip updates in place —
    no bottom toast, no reload (reload stays False: set_reminder isn't 'structural').

    This covers the ROUTER path, which still owns any phrasing the deterministic parser
    declines. The wording matters: "remind me to call the bank at 3pm" is now parsed
    instantly (see test_reminder_is_captured_deterministically), so this uses a phrasing
    with no literal clock time — exactly what the parser hands back to Claude."""
    import ai.claude_cli, ai.router
    monkeypatch.setattr(ai.claude_cli, "has_claude", lambda: True)
    monkeypatch.setattr(ai.router, "route", lambda conn, text, **kw: {
        "reply": "⏰ Reminder set — 15:00: call the bank", "applied": ["set_reminder"],
        "fell_back": False, "created_task_id": None,
        "created_reminder": {"id": 7, "text": "call the bank",
                             "fire_at": "2099-01-02T07:00:00Z", "label": "15:00"}})
    j = client.post("/capture", data={"text": "nudge me about the bank in the afternoon",
                                      "type": "auto"}).get_json()
    assert j["ai"] is True and j["reload"] is False      # no page refresh
    assert j["reminder"]["text"] == "call the bank"      # → window.LifeOS.remAdd splices it
    assert j["reminder"]["label"] == "15:00"


def test_web_fire_stamps_and_is_idempotent(client):
    # the no-Telegram path: the open tab POSTs /fire when a reminder comes due
    add = client.post("/reminders", data={"text": "stretch", "at": "2020-01-01T09:00"}).get_json()
    rid = add["id"]
    first = client.post(f"/reminders/{rid}/fire")
    assert first.status_code == 200 and first.get_json()["fired"] is True
    conn = _db()
    assert conn.execute("SELECT fired_at FROM reminders WHERE id=?", (rid,)).fetchone()[0] is not None
    conn.close()
    # a second tab (or the daemon) racing the same reminder gets fired=False, so it won't re-notify
    second = client.post(f"/reminders/{rid}/fire")
    assert second.status_code == 200 and second.get_json()["fired"] is False
    # and it's dropped from the pending strip
    assert "stretch" not in client.get("/").get_data(as_text=True)


# ── deterministic reminder parsing (shared by BOTH surfaces) ──────────────────
def test_parse_reminder_needs_trigger_and_clock():
    """Fires ONLY on an explicit trigger + a real clock time. A miss costs ~5s via the
    router and still lands right; a false positive puts a wrong time on Sam's phone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from domain.reminders import parse_reminder
    NOW = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Asia/Singapore"))   # Wed 10:00

    assert parse_reminder("add reminder 1 min test", now=NOW) == {
        "text": "test", "fire_local": "2026-07-15T10:01"}
    assert parse_reminder("remind me in 10 minutes to call mum", now=NOW) == {
        "text": "call mum", "fire_local": "2026-07-15T10:10"}
    assert parse_reminder("remind me at 3pm to check the email", now=NOW) == {
        "text": "check the email", "fire_local": "2026-07-15T15:00"}
    assert parse_reminder("remind me tomorrow 9am standup", now=NOW) == {
        "text": "standup", "fire_local": "2026-07-16T09:00"}
    # a clock already past today rolls to tomorrow
    assert parse_reminder("remind me at 9am to call the bank", now=NOW)["fire_local"] \
        == "2026-07-16T09:00"

    # THE critical split, which the router gets right today and this must not break:
    # a DATE with no clock is a task with a due date, NOT a timed push.
    assert parse_reminder("remind me on friday to renew the domain", now=NOW) is None
    assert parse_reminder("remind me tomorrow to call the dentist", now=NOW) is None
    # no trigger / no time / no text / nonsense clock → the router's problem
    for t in ["buy milk", "remind me why I did this", "remind me", "remind me in 10 minutes",
              "remind me the 5 hour meeting is tomorrow", "remind me at 25pm to do x"]:
        assert parse_reminder(t, now=NOW) is None, t


def test_reminder_is_captured_deterministically(client):
    """A parseable reminder must never reach Claude — on EITHER surface."""
    from ai import router

    def _boom(*a, **k):
        raise AssertionError("a parseable reminder must not reach the AI router")
    import pytest
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(router, "call_claude", _boom)
    try:
        j = client.post("/capture", data={"text": "remind me in 10 minutes to call mum",
                                          "type": "auto"},
                        headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
        assert j["kind"] == "reminder" and j["title"] == "call mum"
        assert j["reminder"]["id"] and j["reminder"]["label"]
    finally:
        monkeypatch.undo()


def test_web_and_telegram_share_one_ladder(client):
    """Single source of truth: both surfaces route through capture.route_deterministic, so
    the same text classifies the same way. This is the drift that made the web bar file a
    note while the phone made a task."""
    from domain.capture import route_deterministic
    from core.db import connect
    import os as _os

    for text, kind in [("add a task test 1", "task"), ("buy milk", "task"),
                       ("remind me in 5 min to stretch", "reminder"),
                       ("what's overdue?", "answer")]:
        conn = connect(_os.environ["LIFEOS_DB_PATH"])
        web = route_deterministic(conn, text, source="web")
        conn.close()
        conn = connect(_os.environ["LIFEOS_DB_PATH"])
        tele = route_deterministic(conn, text, source="telegram", enrich="off")
        conn.close()
        assert web is not None and tele is not None, text
        assert web["kind"] == tele["kind"] == kind, text

    # genuine prose matches on NEITHER surface → both hand off to the router
    conn = connect(_os.environ["LIFEOS_DB_PATH"])
    assert route_deterministic(conn, "felt drained after the shoot today") is None
    conn.close()
