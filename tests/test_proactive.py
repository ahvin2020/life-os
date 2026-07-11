"""Tests for the three proactive AI surfaces (proactive.py) + the reschedule_count
signal and the scheduler guards that drive them.

Every claude call is mocked (claude_fn) or monkeypatched — no test reaches the real
CLI. Context builders are exercised as PURE functions on seeded data; the mocked-claude
and fallback paths verify the orchestration; the guard tests verify once-per-day +
correct-time gating without sending.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import capture_daemon as cd
import proactive
import router
import vault_store
from capture import create_task
from db import connect, now_iso, today_iso

TZ = ZoneInfo("Asia/Singapore")


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.actions = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))
        return {"ok": True}


# ── FEATURE 1: morning brief context (ages, goal-pace math, reschedule signal) ──
def test_brief_context_tasks_ages_and_reschedule(client):
    conn = _db()
    day = "2026-07-09"
    with conn:
        t1 = create_task(conn, "Ship invoice", col="week", due_date="2026-07-01",
                         priority="high", category="business")
        conn.execute("UPDATE tasks SET created=? WHERE id=?", ("2026-06-20T00:00:00Z", t1))
        conn.execute("UPDATE tasks SET reschedule_count=2 WHERE id=?", (t1,))
        create_task(conn, "Record intro", col="week", planned_on=day)
    ctx = proactive.build_brief_context(conn, day, now=datetime(2026, 7, 9, 7, 0, tzinfo=TZ))
    conn.close()
    titles = {t["title"]: t for t in ctx["tasks"]}
    assert "Ship invoice" in titles and "Record intro" in titles
    inv = titles["Ship invoice"]
    assert inv["marker"] == "overdue by 8d"          # 2026-07-09 − 2026-07-01
    assert inv["age_days"] == 19                      # created 2026-06-20
    assert inv["reschedule_count"] == 2
    # the postpone signal reaches the prompt text
    assert "postponed 2×" in ctx["text"]
    assert titles["Record intro"]["marker"] == "planned today"


def test_brief_context_goal_pace_math(client):
    """A week measure goal: 438/500, week ends Sunday → the builder computes the
    period-end date and the ~per-day rate the spec describes."""
    conn = _db()
    day = "2026-07-09"                               # a Thursday; week = Mon 06 → Sun 12
    with conn:
        conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, "
            "current_num, timeframe, created) VALUES "
            "('Subscribers','week','2026-07-06','number',500,438,'week',?)", (now_iso(),))
    ctx = proactive.build_brief_context(conn, day, now=datetime(2026, 7, 9, 7, 0, tzinfo=TZ))
    conn.close()
    g = ctx["goals"][0]
    assert g["period_end"] == "2026-07-12"           # Sunday
    assert g["days_left"] == 3
    assert round(g["need_per_day"], 1) == 20.7       # (500−438)/3 ≈ 20.7/day
    assert "period ends 2026-07-12" in ctx["text"]


def test_brief_context_behind_pace_no_task_alert(client):
    """A month measure goal far behind pace with NO linked open task → the builder flags
    behind + no-linked-task so the prompt can raise the goal-pace alert."""
    conn = _db()
    day = "2026-07-25"                               # deep into July
    with conn:
        conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, "
            "current_num, timeframe, created) VALUES "
            "('Newsletter','month','2026-07-01','number',500,50,'month',?)", (now_iso(),))
    ctx = proactive.build_brief_context(conn, day, now=datetime(2026, 7, 25, 7, 0, tzinfo=TZ))
    conn.close()
    g = ctx["goals"][0]
    assert g["behind"] is True
    assert g["has_open_task"] is False
    assert "BEHIND PACE" in ctx["text"] and "no linked open task" in ctx["text"]


def test_morning_brief_mocked_claude_and_fallback(client):
    conn = _db()
    day = "2026-07-09"
    now = datetime(2026, 7, 9, 7, 0, tzinfo=TZ)
    with conn:
        create_task(conn, "Due thing", col="week", due_date=day)
    # mocked claude → wrapped with header
    out = proactive.morning_brief(conn, day, now,
                                  claude_fn=lambda p: "Do the invoice first — it's overdue.")
    assert "Morning brief" in out and "Do the invoice first" in out
    # claude returns nothing → deterministic digest fallback (never a missed send)
    fb = proactive.morning_brief(conn, day, now, claude_fn=lambda p: "")
    conn.close()
    assert "Good morning" in fb and "Due thing" in fb


# ── FEATURE 2: backlog intelligence ────────────────────────────────────────────
def test_backlog_context_staleness_stats_and_reschedule(client):
    conn = _db()
    day = "2026-07-09"
    with conn:
        stale = create_task(conn, "Old vague thing", col="backlog", category="personal")
        conn.execute("UPDATE tasks SET created=?, updated=?, reschedule_count=3 WHERE id=?",
                     ("2026-05-01T00:00:00Z", "2026-05-10T00:00:00Z", stale))
        fresh = create_task(conn, "New thing", col="backlog")
        conn.execute("UPDATE tasks SET updated=? WHERE id=?", ("2026-07-08T00:00:00Z", fresh))
        # completion history: two content tasks done in the last 14 days
        for i in range(2):
            d = create_task(conn, f"done {i}", col="week", category="content")
            conn.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?", (day, d))
    ctx = proactive.build_backlog_context(conn, day)
    conn.close()
    # stalest first
    assert ctx["tasks"][0]["title"] == "Old vague thing"
    assert ctx["tasks"][0]["untouched_days"] == 60   # 2026-07-09 − 2026-05-10
    assert ctx["stats"].get("content") == 2
    assert "postponed 3×" in ctx["text"]
    assert "COMPLETED LAST 14 DAYS BY CATEGORY: content 2" in ctx["text"]


def test_backlog_context_includes_goals_and_link_status(client):
    """Context lists active goals with #ids and marks each open task linked/unlinked so
    the prompt can propose goal links only for the UNLINKED ones."""
    conn = _db()
    day = "2026-07-09"
    with conn:
        gid = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, timeframe, created) "
            "VALUES ('Retire by 50','month','2026-07-01','rollup','year',?)", (now_iso(),)).lastrowid
        linked = create_task(conn, "Already linked", col="backlog")
        conn.execute("UPDATE tasks SET goal_id=? WHERE id=?", (gid, linked))
        create_task(conn, "Set up GIRO", col="backlog")           # unlinked
    ctx = proactive.build_backlog_context(conn, day)
    conn.close()
    assert any(g["id"] == gid and g["title"] == "Retire by 50" for g in ctx["goals"])
    assert f"#{gid} Retire by 50" in ctx["text"]
    assert "ACTIVE GOALS" in ctx["text"]
    assert f"→ goal #{gid}" in ctx["text"]                        # the linked task shows its goal
    assert "Set up GIRO" in ctx["text"] and ", unlinked]" in ctx["text"]
    unlinked = [t for t in ctx["tasks"] if t["title"] == "Set up GIRO"][0]
    assert unlinked["goal_id"] is None


def test_backlog_prompt_has_suggestion_instruction_fallback_does_not(client):
    conn = _db()
    with conn:
        create_task(conn, "Something", col="backlog")
    ctx = proactive.build_backlog_context(conn, "2026-07-09")
    conn.close()
    prompt = proactive.backlog_prompt(ctx)
    assert "Suggested links:" in prompt
    assert 'reply "link task' in prompt.lower() or "link task 62 to goal 2" in prompt
    assert "only proposing" in prompt.lower() or "NEVER claim you've linked" in prompt
    # the deterministic fallback stays dumb — no link suggestions leak into it
    fb = proactive.fallback_backlog(ctx)
    assert "goal" not in fb.lower() and "link" not in fb.lower()


def test_backlog_triage_mocked_and_fallback(client):
    conn = _db()
    with conn:
        t = create_task(conn, "Rotting task", col="backlog")
        conn.execute("UPDATE tasks SET updated=? WHERE id=?", ("2026-05-01T00:00:00Z", t))
    out = proactive.backlog_triage(conn, day="2026-07-09",
                                   claude_fn=lambda p: "Delete: Rotting task — untouched 69d.")
    assert "Backlog triage" in out and "Rotting task" in out

    def _boom(p):
        raise TimeoutError("claude down")
    fb = proactive.backlog_triage(conn, day="2026-07-09", claude_fn=_boom)
    conn.close()
    assert fb.startswith("🧹 Backlog triage — stalest") and "Rotting task" in fb


def test_backlog_triage_empty(client):
    conn = _db()
    out = proactive.backlog_triage(conn, claude_fn=lambda p: "should not be used")
    conn.close()
    assert "clear" in out.lower()


def test_backlog_trigger_detection():
    assert proactive.is_backlog_triage_request("triage my backlog")
    assert proactive.is_backlog_triage_request("can you clean up my tasks please?")
    assert proactive.is_backlog_triage_request("Clean up my backlog")
    assert not proactive.is_backlog_triage_request("what's in my backlog?")
    assert not proactive.is_backlog_triage_request("add a task to the backlog")


def test_daemon_backlog_trigger_runs_intelligence(client, monkeypatch):
    """'clean up my tasks' hits the deterministic fast-path → backlog_triage, skips the
    router, files nothing."""
    conn = _db()
    with conn:
        create_task(conn, "existing", col="backlog")
    tg = FakeTelegram()
    monkeypatch.setattr(proactive, "backlog_triage", lambda c: "🧹 TRIAGE OUTPUT")
    upd = {"update_id": 40, "message": {"from": {"id": 12345678},
           "chat": {"id": 12345678}, "text": "clean up my tasks"}}
    due = cd._process_update(conn, tg, "12345678", upd, None)
    n_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()
    assert tg.sent[-1][1] == "🧹 TRIAGE OUTPUT"
    assert "typing" in [a for _, a in tg.actions]
    assert n_tasks == 1 and not vault_store.list_notes()     # a triage files nothing
    assert due is None


# ── FEATURE 3: evening reflection ──────────────────────────────────────────────
def test_reflection_context_7day_window_and_journaled_flag(client):
    conn = _db()
    day = "2026-07-09"
    # seed journal across days: one outside the 7-day window, two inside, one today
    vault_store.append_journal_entry("2026-07-01", "way back")     # 8 days before → excluded
    vault_store.append_journal_entry("2026-07-03", "midweek felt good")
    vault_store.append_journal_entry("2026-07-08", "tired yesterday")
    vault_store.append_journal_entry(day, "shipped the REITs video today")
    with conn:
        d = create_task(conn, "Edit REITs video", col="week", category="content")
        conn.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?", (day, d))
    ctx = proactive.build_reflection_context(conn, day, now=datetime(2026, 7, 9, 21, 30, tzinfo=TZ))
    conn.close()
    assert ctx["journaled_today"] is True
    assert any("shipped the REITs" in e for e in ctx["today_entries"])
    assert "Edit REITs video" in ctx["text"]                       # today's completed task
    joined = "\n".join(ctx["recent_journal"])
    assert "midweek felt good" in joined and "tired yesterday" in joined
    assert "way back" not in joined                                # outside the 7-day window
    assert "2026-07-01" not in joined


def test_reflection_notes_capped(client):
    conn = _db()
    day = today_iso()
    for i in range(20):
        vault_store.create_note(title=f"capture {i}", body="x", tags=["unsorted"])
    ctx = proactive.build_reflection_context(conn, day)
    conn.close()
    assert len(ctx["notes_today"]) == 15                           # flood guard
    assert "20 total" in ctx["text"]


def test_reflection_mocked_and_fallback(client):
    conn = _db()
    day = today_iso()
    now = datetime(2026, 7, 9, 21, 30, tzinfo=TZ)
    out = proactive.evening_reflection(
        conn, day, now, claude_fn=lambda p: "You shipped the REITs video — how did that feel?")
    assert "Evening reflection" in out and "REITs" in out
    fb = proactive.evening_reflection(conn, day, now, claude_fn=lambda p: "")
    conn.close()
    assert "check-in" in fb.lower()                                # warm static fallback


# ── reschedule_count increment logic (v4 signal) ───────────────────────────────
def test_reschedule_router_set_due_later_and_earlier(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Invoice", col="week", due_date="2026-07-10")
    ctx = router.build_context(conn)
    router.apply_action(conn, {"action": "set_due", "id": tid, "date": "2026-07-20"}, ctx)
    n1 = conn.execute("SELECT reschedule_count FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    # moving EARLIER is not a postpone
    ctx = router.build_context(conn)
    router.apply_action(conn, {"action": "set_due", "id": tid, "date": "2026-07-12"}, ctx)
    n2 = conn.execute("SELECT reschedule_count FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    conn.close()
    assert n1 == 1 and n2 == 1


def test_reschedule_router_unplan(client):
    conn = _db()
    today = today_iso()
    with conn:
        tid = create_task(conn, "Planned task", col="week")
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?", (today, tid))
    ctx = router.build_context(conn)
    router.apply_action(conn, {"action": "unplan", "id": tid}, ctx)
    n = conn.execute("SELECT reschedule_count FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    conn.close()
    assert n == 1


def test_reschedule_task_plan_toggle_off(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Toggle me", col="week")
    conn.close()
    client.post(f"/tasks/{tid}/plan")               # sets planned_on
    client.post(f"/tasks/{tid}/plan")               # clears it → a postpone
    conn = _db()
    n = conn.execute("SELECT reschedule_count FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    conn.close()
    assert n == 1


def test_reschedule_task_edit_due_later(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Edit due", col="week", due_date="2026-07-10")
    conn.close()
    client.post(f"/tasks/{tid}/edit", data={"due_date": "2026-07-25"})
    conn = _db()
    later = conn.execute("SELECT reschedule_count FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    conn.close()
    client.post(f"/tasks/{tid}/edit", data={"due_date": "2026-07-15"})   # earlier
    conn = _db()
    earlier = conn.execute("SELECT reschedule_count FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    conn.close()
    assert later == 1 and earlier == 1              # only the later move counted


# ── scheduler guards (once per day, correct hour/time) ─────────────────────────
def test_digest_guard_hour_and_once(client, monkeypatch):
    monkeypatch.setattr(proactive, "morning_brief",
                        lambda c, d, n, backlog_summary=None: "BRIEF")
    conn = _db()
    tg = FakeTelegram()
    before = datetime(2026, 7, 9, 6, 0, tzinfo=TZ)  # before digest_hour (default 7)
    assert cd.maybe_send_digest(conn, tg, "chat", now=before) is False
    at = datetime(2026, 7, 9, 7, 30, tzinfo=TZ)
    assert cd.maybe_send_digest(conn, tg, "chat", now=at) is True
    assert cd.maybe_send_digest(conn, tg, "chat", now=at) is False       # once per day
    conn.close()
    assert tg.sent[-1][1] == "BRIEF"


def test_digest_does_not_weave_backlog(client, monkeypatch):
    """Backlog triage is its own scheduled surface now — the morning brief no longer
    weaves it in, even on Sunday."""
    seen = {}
    monkeypatch.setattr(proactive, "morning_brief",
                        lambda c, d, n, backlog_summary=None: seen.update(bs=backlog_summary) or "BRIEF")
    conn = _db()
    tg = FakeTelegram()
    sunday = datetime(2026, 7, 12, 8, 0, tzinfo=TZ)                      # a Sunday
    assert cd.maybe_send_digest(conn, tg, "chat", now=sunday) is True
    conn.close()
    assert seen["bs"] is None                                           # not woven in


def test_scheduled_backlog_triage_day_time_and_once(client, monkeypatch):
    """The independent triage fires on its day at/after its time, once per day."""
    from db import delete_setting
    monkeypatch.setattr(proactive, "backlog_triage", lambda c: "TRIAGE-MSG")
    conn = _db()
    tg = FakeTelegram()
    sun_8 = datetime(2026, 7, 12, 8, 0, tzinfo=TZ)                       # before 09:00 default
    assert cd.maybe_send_backlog_triage(conn, tg, "chat", now=sun_8) is False
    sun_9 = datetime(2026, 7, 12, 9, 0, tzinfo=TZ)                       # Sunday 09:00
    assert cd.maybe_send_backlog_triage(conn, tg, "chat", now=sun_9) is True
    assert tg.sent[-1][1] == "TRIAGE-MSG"
    assert cd.maybe_send_backlog_triage(conn, tg, "chat", now=sun_9) is False   # once/day
    delete_setting(conn, "triage_scheduled_sent")
    mon_9 = datetime(2026, 7, 13, 9, 0, tzinfo=TZ)                       # wrong day
    assert cd.maybe_send_backlog_triage(conn, tg, "chat", now=mon_9) is False
    conn.close()


def test_reflection_guard_time_and_once(client, monkeypatch):
    monkeypatch.setattr(proactive, "evening_reflection", lambda c, d, n: "REFLECT")
    conn = _db()
    tg = FakeTelegram()
    early = datetime(2026, 7, 9, 21, 0, tzinfo=TZ)   # 21:00 < 21:30
    assert cd.maybe_send_reflection(conn, tg, "chat", now=early) is False
    at = datetime(2026, 7, 9, 21, 45, tzinfo=TZ)
    assert cd.maybe_send_reflection(conn, tg, "chat", now=at) is True
    assert cd.maybe_send_reflection(conn, tg, "chat", now=at) is False   # once per day
    conn.close()
    assert tg.sent[-1][1] == "REFLECT"
