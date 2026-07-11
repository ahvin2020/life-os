"""Tests for the /settings panel (routes_settings.py) and the backend wiring it
drives: the three proactive toggles (brief/triage/reflection), timing overrides,
and the housekeeping thresholds (archive/purge/stale) read by the daemon + proactive.

Web-layer tests use the shared CSRF-aware `client` fixture; the scheduler/toggle tests
reuse the FakeTelegram + monkeypatched-proactive harness from test_proactive.py so no
test reaches the real claude CLI. Every claude surface is stubbed.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import capture_daemon as cd
from ai import proactive
from domain.capture import create_task
from core.db import connect, today_iso, now_iso, get_setting, set_setting, delete_setting
from domain.tasks_core import archive_old_done, purge_deleted

TZ = ZoneInfo("Asia/Singapore")


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _days_ago(n):
    return (datetime.strptime(today_iso(), "%Y-%m-%d") - timedelta(days=n)).date().isoformat()


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}


# ── 1. renders ────────────────────────────────────────────────────────────────
def test_settings_page_prefills_defaults_as_values(client):
    html = client.get("/settings").data.decode()
    # every field name present
    for name in ("brief_enabled", "triage_enabled", "reflection_enabled",
                 "digest_hour", "reflection_hour", "voice_language",
                 "archive_done_days", "purge_deleted_days", "stale_backlog_days"):
        assert f'name="{name}"' in html
    # with no settings rows, code defaults surface as the input VALUE (never a blank box)
    assert 'value="07:00"' in html            # digest_hour default
    assert 'value="21:30"' in html            # reflection_hour default
    assert 'value="30"' in html               # purge/stale default
    assert 'value="7"' in html                # archive/backup_keep default
    # backup_location stays blank (portable across the Mac↔NAS sync) with its resolved
    # default shown in the description instead of pinned into the field
    assert 'name="backup_location" value=""' in html
    assert "data-backups" in html
    # gear/link chrome points at /settings
    assert 'href="/settings"' in html
    # toggles default ON (missing row = enabled) → rendered checked
    assert "checked" in html


# ── 2. round-trip ─────────────────────────────────────────────────────────────
def test_settings_save_round_trip_persists_and_prefills(client):
    r = client.post("/settings/save", data={
        "brief_enabled": "1", "triage_enabled": "1", "reflection_enabled": "1",
        "digest_hour": "08:00", "reflection_hour": "22:00", "archive_done_days": "14"})
    assert r.status_code in (200, 302)
    conn = _db()
    assert get_setting(conn, "digest_hour") == "08:00"   # morning brief is now HH:MM
    assert get_setting(conn, "reflection_hour") == "22:00"
    assert get_setting(conn, "archive_done_days") == "14"
    conn.close()
    html = client.get("/settings").data.decode()
    assert 'value="08:00"' in html and 'value="22:00"' in html and 'value="14"' in html


# ── 3. atomic validation (one bad field writes NOTHING) ───────────────────────
def test_settings_save_validation_is_atomic(client):
    ajax = {"X-Requested-With": "XMLHttpRequest"}
    for bad in ({"digest_hour": "25"},
                {"reflection_hour": "9pm"},
                {"archive_done_days": "0"}):
        data = {"brief_enabled": "1", "archive_done_days": "14",  # a valid field alongside
                "digest_hour": "8", "reflection_hour": "22:00"}
        data.update(bad)                                          # clobber with the invalid one
        r = client.post("/settings/save", data=data, headers=ajax)
        assert r.status_code == 400
        conn = _db()
        # nothing written — not even the valid fields nor the toggles
        n = conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
        conn.close()
        assert n == 0, f"{bad} leaked a write"


# ── 4. blank resets a stored override to the code default ─────────────────────
def test_blank_field_deletes_row_restoring_default(client, monkeypatch):
    conn = _db()
    set_setting(conn, "digest_hour", "9")                        # a stored override
    conn.close()
    # POST with digest_hour blank → row deleted (keep the brief toggle on)
    client.post("/settings/save", data={"brief_enabled": "1", "digest_hour": ""})
    conn = _db()
    assert get_setting(conn, "digest_hour") is None              # reset to code default
    conn.close()

    monkeypatch.setattr(proactive, "morning_brief",
                        lambda c, d, n, backlog_summary=None: "BRIEF")
    conn = _db()
    tg = FakeTelegram()
    at = datetime(2026, 7, 9, 7, 30, tzinfo=TZ)                  # Thursday, ≥ default 7
    assert cd.maybe_send_digest(conn, tg, "chat", now=at) is True
    conn.close()
    assert tg.sent[-1][1] == "BRIEF"


# ── 5. brief toggle gates BEFORE the last-sent stamp ──────────────────────────
def test_brief_toggle_off_never_sends_or_stamps(client, monkeypatch):
    monkeypatch.setattr(proactive, "morning_brief",
                        lambda c, d, n, backlog_summary=None: "BRIEF")
    conn = _db()
    tg = FakeTelegram()
    at = datetime(2026, 7, 9, 8, 0, tzinfo=TZ)
    set_setting(conn, "brief_enabled", "0")
    assert cd.maybe_send_digest(conn, tg, "chat", now=at) is False
    assert get_setting(conn, "digest_last_sent") is None         # NOT stamped when disabled
    # flip on the same day → sends exactly once
    set_setting(conn, "brief_enabled", "1")
    assert cd.maybe_send_digest(conn, tg, "chat", now=at) is True
    assert cd.maybe_send_digest(conn, tg, "chat", now=at) is False   # last-sent guard
    conn.close()
    assert [s[1] for s in tg.sent] == ["BRIEF"]


# ── 6. reflection toggle, same shape ──────────────────────────────────────────
def test_reflection_toggle_off_never_sends_or_stamps(client, monkeypatch):
    monkeypatch.setattr(proactive, "evening_reflection", lambda c, d, n: "REFLECT")
    conn = _db()
    tg = FakeTelegram()
    at = datetime(2026, 7, 9, 22, 0, tzinfo=TZ)                  # ≥ default 21:30
    set_setting(conn, "reflection_enabled", "0")
    assert cd.maybe_send_reflection(conn, tg, "chat", now=at) is False
    assert get_setting(conn, "reflection_last_sent") is None
    set_setting(conn, "reflection_enabled", "1")
    assert cd.maybe_send_reflection(conn, tg, "chat", now=at) is True
    assert cd.maybe_send_reflection(conn, tg, "chat", now=at) is False
    conn.close()
    assert [s[1] for s in tg.sent] == ["REFLECT"]


# ── 7. triage is independent of the brief; its toggle gates the scheduler ─────
def test_triage_independent_of_brief_and_gated_by_toggle(client, monkeypatch):
    seen = {}
    calls = []
    monkeypatch.setattr(proactive, "backlog_triage", lambda c: calls.append(1) or "BACKLOG")
    monkeypatch.setattr(proactive, "morning_brief",
                        lambda c, d, n, backlog_summary=None: seen.update(bs=backlog_summary) or "BRIEF")
    conn = _db()
    tg = FakeTelegram()
    sunday = datetime(2026, 7, 12, 9, 0, tzinfo=TZ)            # Sunday, at the 09:00 default

    # the brief NEVER weaves backlog now (independent surface)
    assert cd.maybe_send_digest(conn, tg, "chat", now=sunday) is True
    assert seen["bs"] is None and calls == []

    # triage toggle OFF → the scheduler doesn't fire
    set_setting(conn, "triage_enabled", "0")
    assert cd.maybe_send_backlog_triage(conn, tg, "chat", now=sunday) is False
    assert calls == []

    # toggle ON (default) → the scheduler fires on its own
    delete_setting(conn, "triage_enabled")
    assert cd.maybe_send_backlog_triage(conn, tg, "chat", now=sunday) is True
    assert calls == [1]
    conn.close()


# ── 8. housekeeping thresholds ────────────────────────────────────────────────
def test_archive_done_days_threshold(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Old done", col="done")
        conn.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?",
                     (_days_ago(3), tid))
    archive_old_done(conn)                                      # default 7 → leaves it
    assert conn.execute("SELECT archived_at FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None
    set_setting(conn, "archive_done_days", "1")
    archive_old_done(conn)                                      # 1 → 3-day-old task archives
    assert conn.execute("SELECT archived_at FROM tasks WHERE id=?", (tid,)).fetchone()[0] is not None
    conn.close()


def test_purge_deleted_days_threshold(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Soft deleted", col="backlog")
        conn.execute("UPDATE tasks SET deleted_at=? WHERE id=?",
                     (_days_ago(10) + "T00:00:00Z", tid))
    purge_deleted(conn)                                         # default 30 → keeps it
    assert conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone() is not None
    set_setting(conn, "purge_deleted_days", "5")
    purge_deleted(conn)                                         # 5 → 10-day-old row purged
    assert conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone() is None
    conn.close()


def test_stale_backlog_days_threshold(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Untouched", col="backlog")
        conn.execute("UPDATE tasks SET updated=? WHERE id=?",
                     (_days_ago(10) + "T00:00:00Z", tid))
    today = today_iso()
    # default 30 → a 10-day-untouched task is not yet stale
    assert proactive._stale_backlog(conn, today) == []
    assert proactive.build_brief_context(conn)["stale_count"] == 0
    # lower the threshold → it surfaces in both the daemon list and the brief count
    set_setting(conn, "stale_backlog_days", "5")
    stale = proactive._stale_backlog(conn, today)
    assert any(r["title"] == "Untouched" for r in stale)
    assert proactive.build_brief_context(conn)["stale_count"] >= 1
    conn.close()
