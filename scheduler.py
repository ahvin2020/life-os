#!/usr/bin/env python3
"""Outbound time-of-day scheduler — the proactive half of the daemon.

capture_daemon.py owns inbound Telegram I/O; this module owns the OUTBOUND cadences
it fires from the main poll loop: the morning brief, evening reflection, scheduled
backlog triage, weekly review, monthly retrospective, the silent document-facts scan,
timed reminders, the daily #unsorted sweep, and the Claude-down health nudge.

Every "send once per day at/after HH:MM" surface shares ONE gate (`_due_daily`); each
function keeps its own send + guard-stamp body, because the stamp TIMING differs on
purpose (most stamp after a successful send; the document scan stamps BEFORE the slow
scan so a failing scan can't re-run every poll cycle).

capture_daemon imports these back so all existing call sites (the main loop and the
tests' `capture_daemon.maybe_*`) resolve unchanged. This module must NOT import
capture_daemon at module level (that would cycle); it pulls the daemon's `_log` via a
function-local import, by which time capture_daemon is fully loaded.
"""

from __future__ import annotations

from datetime import datetime

from core.db import (get_setting as _get_setting, set_setting as _set_setting,
                     today_iso, now_sg)
from core.dates import parse_iso_utc

_WEEKDAY_NUM = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _log(msg: str) -> None:
    """Delegate to the daemon's logger (function-local import avoids an import cycle)."""
    from capture_daemon import _log as _daemon_log
    _daemon_log(msg)


def _parse_hhmm(val, default_h, default_m):
    """Parse an 'HH:MM' (or bare 'HH') setting into (hour, minute)."""
    try:
        s = str(val)
        if ":" in s:
            h, m = s.split(":", 1)
            return int(h), int(m)
        return int(s), 0
    except (TypeError, ValueError):
        return default_h, default_m


def _due_daily(conn, *, enabled_key, guard_key, time_key=None, time_default=None,
               parse_default=(0, 0), day_key=None, day_default="sun",
               weekday_fallback=6, first_sunday=False, guard_by_month=False,
               now=None):
    """Shared GATE for the once-a-day scheduled surfaces. Returns (should_run, now, today).

    Checks, in order: `enabled_key` (default '1'; '0' disables); an optional weekday gate
    (`day_key`, 'daily' or mon..sun — or `first_sunday` for the monthly retro, which fires
    only on the first Sunday of the month); an at/after-`time_key` time gate (parsed via
    `_parse_hhmm`, missing setting → `time_default`, corrupt value → `parse_default`; omit
    `time_key` to skip the time gate entirely); and the once-per-day guard `guard_key`
    (compared against today, or today[:7] when `guard_by_month`).

    The GATE only decides whether to run; the caller does the actual send and stamps the
    guard itself, so each surface keeps its own stamp timing (before vs after work)."""
    now = now or now_sg()
    today = today_iso()
    if _get_setting(conn, enabled_key, "1") == "0":
        return False, now, today
    if first_sunday:
        if now.weekday() != 6 or now.day > 7:          # first Sunday only
            return False, now, today
    elif day_key is not None:
        day = (_get_setting(conn, day_key, day_default) or day_default).lower()
        if day != "daily" and now.weekday() != _WEEKDAY_NUM.get(day, weekday_fallback):
            return False, now, today
    if time_key is not None:
        h, m = _parse_hhmm(_get_setting(conn, time_key, time_default), *parse_default)
        if (now.hour, now.minute) < (h, m):
            return False, now, today
    guard = today[:7] if guard_by_month else today
    if _get_setting(conn, guard_key) == guard:
        return False, now, today
    return True, now, today


# ── timed reminders ────────────────────────────────────────────────────────────
def maybe_fire_reminders(conn, tg, chat_id) -> int:
    """Push any timed reminders whose fire_at has passed, then mark them fired. Runs every
    poll cycle, so a reminder lands within one long-poll interval of its stated time."""
    from core.db import now_iso
    now = now_iso()
    due = conn.execute(
        "SELECT id, text FROM reminders WHERE fired_at IS NULL AND fire_at <= ? ORDER BY fire_at",
        (now,)).fetchall()
    sent = 0
    for r in due:
        try:
            tg.send_message(chat_id, f"⏰ Reminder: {r['text']}")
            conn.execute("UPDATE reminders SET fired_at=? WHERE id=?", (now, r["id"]))
            sent += 1
        except Exception as e:
            _log(f"reminder send failed: {e}")
    if sent:
        conn.commit()
    return sent


# ── outbound: morning brief ────────────────────────────────────────────────────
def maybe_send_digest(conn, tg, chat_id, now=None) -> bool:
    """Send the AI morning brief once per day at/after digest_hour (default 7). Returns
    True if sent. proactive.build_digest remains the deterministic fallback inside proactive."""
    from ai import proactive
    ok, now, today = _due_daily(conn, enabled_key="brief_enabled",
                                time_key="digest_hour", time_default="7", parse_default=(7, 0),
                                guard_key="digest_last_sent", now=now)
    if not ok:
        return False
    # Backlog triage is now its own scheduled surface (maybe_send_backlog_triage),
    # independent of the brief — no longer woven in on Sundays.
    text = proactive.morning_brief(conn, today, now)
    tg.send_message(chat_id, text)
    _set_setting(conn, "digest_last_sent", today)
    _log("morning brief sent")
    return True


def maybe_send_reflection(conn, tg, chat_id, now=None) -> bool:
    """Send the evening journal reflection once per day at/after reflection_hour
    (default 21:30). Returns True if sent."""
    from ai import proactive
    ok, now, today = _due_daily(conn, enabled_key="reflection_enabled",
                                time_key="reflection_hour", time_default="21:30",
                                parse_default=(21, 30), guard_key="reflection_last_sent", now=now)
    if not ok:
        return False
    text = proactive.evening_reflection(conn, today, now)
    tg.send_message(chat_id, text)
    _set_setting(conn, "reflection_last_sent", today)
    _log("evening reflection sent")
    return True


def maybe_send_backlog_triage(conn, tg, chat_id, now=None) -> bool:
    """Send the Do/Defer/Delete backlog triage once on its scheduled day at/after its
    time (settings triage_day + triage_time; default Sunday 09:00). Independent of the
    morning brief. On-demand triage ("triage my backlog") still works separately."""
    from ai import proactive
    ok, now, today = _due_daily(conn, enabled_key="triage_enabled",
                                day_key="triage_day", day_default="sun", weekday_fallback=6,
                                time_key="triage_time", time_default="09:00", parse_default=(9, 0),
                                guard_key="triage_scheduled_sent", now=now)
    if not ok:
        return False
    try:
        text = proactive.backlog_triage(conn)
    except Exception as e:
        _log(f"scheduled backlog triage failed: {e}")
        return False
    tg.send_message(chat_id, text)
    _set_setting(conn, "triage_scheduled_sent", today)
    _log("backlog triage sent")
    # Piggyback the profile-rule suggestion (guarded to once a week, ≥3 corrections).
    try:
        proactive.maybe_suggest_profile_rule(conn, tg, chat_id)
    except Exception as e:
        _log(f"profile suggestion failed: {e}")
    return True


def maybe_send_weekly_review(conn, tg, chat_id, now=None) -> bool:
    """Send the weekly review once on its scheduled day at/after its time (settings
    weekly_day + weekly_time; default Sunday 18:00). Celebrates the week's wins, names
    what slipped, and tees up next week — distinct from the morning brief and the
    do/defer/delete backlog triage. Returns True if sent."""
    from ai import proactive
    ok, now, today = _due_daily(conn, enabled_key="weekly_enabled",
                                day_key="weekly_day", day_default="sun", weekday_fallback=6,
                                time_key="weekly_time", time_default="18:00", parse_default=(18, 0),
                                guard_key="weekly_last_sent", now=now)
    if not ok:
        return False
    try:
        text = proactive.weekly_review(conn, today, now)
        sug = proactive.weekly_suggestion(conn, today)
    except Exception as e:
        _log(f"scheduled weekly review failed: {e}")
        return False
    if sug:
        text += "\n\n" + sug["line"]
    tg.send_message(chat_id, text)
    if sug:                                    # arm the "yes" for the next message
        from ai import router
        router.set_pending(conn, sug["kind"], sug["payload"])
        router.record_exchange(conn, "[weekly review]", text, reply_cap=1200)
    _set_setting(conn, "weekly_last_sent", today)
    _log("weekly review sent")
    return True


def maybe_send_monthly_retro(conn, tg, chat_id, now=None) -> bool:
    """Send the monthly retrospective once, on the FIRST Sunday of the month at/after its
    time (default 17:00 — an hour before the weekly at 18:00 so month-then-week reads in
    order). Guarded once-per-month via monthly_last_sent ('YYYY-MM'). Returns True if sent."""
    from ai import proactive
    ok, now, today = _due_daily(conn, enabled_key="monthly_enabled", first_sunday=True,
                                time_key="monthly_time", time_default="17:00", parse_default=(17, 0),
                                guard_key="monthly_last_sent", guard_by_month=True, now=now)
    if not ok:
        return False
    try:
        text = proactive.monthly_retrospective(conn, today, now)
    except Exception as e:
        _log(f"scheduled monthly retro failed: {e}")
        return False
    tg.send_message(chat_id, text)
    _set_setting(conn, "monthly_last_sent", today[:7])
    _log("monthly retrospective sent")
    return True


# ── outbound: silent document-facts scan ───────────────────────────────────────
def maybe_scan_documents(conn, now=None) -> bool:
    """Once per scheduled day, read newly-arrived documents and refresh the facts cache
    (booking refs, prices, expiry/renewal dates) so document questions answer instantly.
    Silent — findings surface via the morning brief, not a push. Returns True if it ran."""
    from domain import docs
    # Default "daily" so a newly-synced document becomes queryable within a day (the
    # "run periodically / whenever there's new info" cadence). Each run only reads files
    # whose mtime changed, so a daily pass over an unchanged folder is nearly free.
    ok, now, today = _due_daily(conn, enabled_key="docscan_enabled",
                                day_key="docscan_day", day_default="daily", weekday_fallback=0,
                                guard_key="docscan_last_day", now=now)
    if not ok:
        return False
    # Set the once-per-day guard BEFORE the (slow, synchronous) scan runs: if the scan
    # raises, we must NOT re-attempt it on every poll cycle for the rest of the day — each
    # attempt can block Telegram handling for minutes. On-demand scan_documents_now (fired
    # after a document capture) still keeps same-day new arrivals queryable.
    _set_setting(conn, "docscan_last_day", today)
    try:
        found = docs.scan_documents(conn)
        _log(f"document scan: {len(found)} new fact(s)")
    except Exception as e:
        _log(f"document scan failed: {e}")
        return False
    return True


def scan_documents_now(conn) -> int:
    """Immediate facts refresh — the 'whenever there's new info' trigger. Called after a
    document capture so a just-arrived booking is queryable without waiting for the
    scheduled scan. Returns the number of new facts."""
    from domain import docs
    try:
        return len(docs.scan_documents(conn))
    except Exception as e:
        _log(f"document scan (on-demand) failed: {e}")
        return 0


def schedule_doc_scan():
    """Fire scan_documents_now in the background (fresh connection) so a just-captured
    document becomes queryable within seconds without blocking the Telegram loop — the
    request's conn stays owned by the caller and SQLite conns aren't thread-safe."""
    import threading
    from core.db import connect

    def _run():
        try:
            c = connect()
            try:
                n = scan_documents_now(c)
                if n:
                    _log(f"document scan (on-capture): {n} new fact(s)")
            finally:
                c.close()
        except Exception as e:
            _log(f"on-capture document scan failed: {e}")

    threading.Thread(target=_run, name="tg-docscan", daemon=True).start()


# ── daily #unsorted sweep ──────────────────────────────────────────────────────
def maybe_daily_sweep(conn, tg, chat_id) -> bool:
    """Run the #unsorted sweep at most once per day (a floor under the on-fallback
    sweep, so leftovers never rot). Returns True if it ran."""
    today = today_iso()
    if _get_setting(conn, "sweep_last_day") == today:
        return False
    _set_setting(conn, "sweep_last_day", today)
    # reclaim .media/.audio files no live note/journal/task points at (past the undo window)
    try:
        from domain import vault_store
        try:
            days = int(_get_setting(conn, "purge_deleted_days", "30") or "30")
        except (TypeError, ValueError):
            days = 30
        n = vault_store.purge_orphan_attachments(conn, days=days)
        if n:
            _log(f"purged {n} orphan attachment(s)")
    except Exception as e:
        _log(f"orphan-attachment purge failed: {e}")
    from capture_daemon import capture_has_unsorted, run_triage_now
    if capture_has_unsorted():
        run_triage_now(conn, tg, chat_id)
    return True


# ── Claude-down health nudge ───────────────────────────────────────────────────
def maybe_notify_claude_down(conn, tg, chat_id) -> bool:
    """Nudge over Telegram when Claude starts failing (usually a lapsed OAuth token),
    and once more when it recovers. Reads the claude_last_ok/err heartbeats stamped by
    ai.claude_cli; keyed on the error timestamp so a persistent outage doesn't spam.
    Since ALL AI (bot router, enrichment, proactive surfaces) runs through claude, this
    is Kelvin's single 'your token expired, update it in Settings' signal."""
    ok = _get_setting(conn, "claude_last_ok")
    err = _get_setting(conn, "claude_last_err") or ""
    err_ts = err.split("|", 1)[0].strip()
    reason = err.split("|", 1)[1].strip() if "|" in err else ""
    okp, errp = parse_iso_utc(ok), parse_iso_utc(err_ts)
    down = errp is not None and (okp is None or errp > okp)
    notified = _get_setting(conn, "claude_down_notified") or ""
    if down:
        if err_ts and err_ts != notified:
            tip = (" Your Claude token may have expired — paste a fresh one on the Settings page."
                   if reason.lower().startswith("auth") else "")
            tg.send_message(chat_id, "⚠️ AI is offline — I couldn't reach Claude." + tip)
            _set_setting(conn, "claude_down_notified", err_ts)
            return True
    elif okp is not None and notified:
        tg.send_message(chat_id, "✅ AI is back online.")
        _set_setting(conn, "claude_down_notified", "")
        return True
    return False
