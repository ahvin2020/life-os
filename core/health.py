#!/usr/bin/env python3
"""Background-job health + integration status for Life OS.

A self-contained subsystem, extracted from core/web_core.py so the web layer keeps
only Flask glue. Everything here is Flask-free and pure over a passed `conn`:

- `health_status`/`health_ages`/`health_reasons` — the sidebar staleness dots from the
  capture/triage/backup heartbeats stamped in `settings`.
- `ai_health` — Claude CLI/OAuth state from its call heartbeats.
- `_integration_pending` — half-finished / failing Google+Dropbox integrations.
- `health_context` — the full per-request template context (dots + ages + ai + nav
  alert count); the Flask context processor in web_core is a thin wrapper over it.

The `ai.google_client` import inside `_integration_pending` is FUNCTION-LOCAL and
deferred on purpose — this module has no module-level `ai` import, so `core` never
imports `ai` at load time (breaking the old layer inversion + import cycle).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from core.db import get_setting
from core.dates import parse_iso_utc


# ── background-job health (staleness dots) ────────────────────────────────────
# The capture daemon and triage runner stamp settings keys each run; the sidebar
# dots go 'stale' (red) when a heartbeat is older than its budget, 'off' (grey)
# when a job has never run. Budgets: capture 10 min (long-poll cycle ≈ 50 s), triage
# and backup 26 h (daily jobs + slack).
_HEALTH_CHECKS = (
    ("capture", "capture_last_ran", 10),
    ("triage", "triage_last_ran", 26 * 60),
    ("backup", "backup_last_ran", 26 * 60),
)


def health_status(conn, now=None) -> dict:
    """Per-job dot state from the settings heartbeats: 'ok' | 'stale' | 'off'.
    Pure enough to unit-test: pass a seeded conn and a fixed `now`."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for name, key, budget_min in _HEALTH_CHECKS:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        ts = parse_iso_utc(row["value"]) if row else None
        if ts is None:
            out[name] = "off"
        elif (now - ts).total_seconds() > budget_min * 60:
            out[name] = "stale"
        else:
            out[name] = "ok"
    return out


def _fmt_age(seconds) -> str:
    """Compact last-ran age for the sidebar dots: 40s ago / 12m ago / 5h ago / 3d ago."""
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def health_ages(conn, now=None) -> dict:
    """Per-job 'time since last run' label ('40s'|'5h'|…), or None if never ran."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for name, key, _ in _HEALTH_CHECKS:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        ts = parse_iso_utc(row["value"]) if row else None
        out[name] = _fmt_age((now - ts).total_seconds()) if ts else None
    return out


# Human "why is this red" copy per job — {age}/{budget} filled in when stale.
_HEALTH_WHY = {
    "capture": ("No heartbeat for {age} — the capture daemon looks stopped",
                "Capture daemon hasn't started yet"),
    "triage": ("Hasn't run in {age} (expected within {budget})",
               "Hasn't run yet"),
    "backup": ("No backup in {age} (expected within {budget})",
               "No backup has run yet"),
}


def health_reasons(conn, now=None) -> dict:
    """Per-job explanation of a non-'ok' dot ('' when ok) — so the red dot can SAY why
    it's red instead of just being red. Pairs the state with the last-seen age + the
    staleness budget it blew. Pure/unit-testable."""
    status = health_status(conn, now)
    ages = health_ages(conn, now)
    budgets = {name: b for name, _key, b in _HEALTH_CHECKS}
    out = {}
    for name in ("capture", "triage", "backup"):
        st = status.get(name)
        stale_msg, off_msg = _HEALTH_WHY[name]
        if st == "off":
            out[name] = off_msg
        elif st == "stale":
            b = budgets[name]
            budget = f"{b} min" if b < 60 else f"{b // 60} h"
            age = (ages.get(name) or "a while").replace(" ago", "")   # "3d ago" → "3d"
            out[name] = stale_msg.format(age=age, budget=budget)
        else:
            out[name] = ""
    return out


def ai_health(conn) -> dict:
    """State of the Claude CLI / OAuth token from its call heartbeats:
      'ok'    — last call succeeded (and no newer failure)
      'error' — the most recent call failed; `detail` says why ('auth' ⇒ token lapsed)
      'off'   — never called yet
    Written by ai.claude_cli._stamp_health on every call. Pure/unit-testable."""
    def _row(key):
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None
    ok_raw, err_raw = _row("claude_last_ok"), _row("claude_last_err")
    ok_ts = parse_iso_utc(ok_raw)
    # err value is "ISO | reason"
    err_ts = parse_iso_utc((err_raw or "").split("|", 1)[0].strip()) if err_raw else None
    detail = (err_raw or "").split("|", 1)[1].strip() if (err_raw and "|" in err_raw) else ""
    if err_ts and (ok_ts is None or err_ts > ok_ts):
        return {"state": "error", "detail": detail or "call failed",
                "auth": detail.lower().startswith("auth")}
    if ok_ts is not None:
        return {"state": "ok", "detail": "", "auth": False}
    return {"state": "off", "detail": "", "auth": False}


def _integration_pending(conn) -> int:
    """Count integrations needing attention: STARTED-but-not-finished (creds saved, not
    connected) OR connected-but-FAILING (a recent API call errored). Optional integrations
    you never touch don't nag; a half-done setup or a broken connection does."""
    n = 0
    try:
        from ai import google_client
        g_conn = google_client.is_configured()
        if (get_setting(conn, "google_client_id") and get_setting(conn, "google_client_secret")
                and not g_conn and not get_setting(conn, "google_disconnected")):
            n += 1                                   # half-finished (not a deliberate disconnect)
        elif g_conn and get_setting(conn, "google_last_err"):
            n += 1                                   # connected but failing
    except Exception:
        pass
    d_conn = bool(get_setting(conn, "dropbox_token"))
    if (get_setting(conn, "dropbox_app_key") and get_setting(conn, "dropbox_app_secret")
            and not d_conn and not get_setting(conn, "dropbox_disconnected")):
        n += 1
    elif d_conn and get_setting(conn, "dropbox_last_err"):
        n += 1
    return n


def health_context(conn) -> dict:
    """The full per-request health context for templates: dot states, last-ran ages, the
    Claude/AI state, and the nav 'needs attention' badge count. All the business logic that
    used to live in web_core's `inject_health`; the context processor there is now just the
    Flask glue that opens a conn and falls back to a safe default on any error."""
    status = health_status(conn)
    ages = health_ages(conn)
    ai = ai_health(conn)
    integ_pending = _integration_pending(conn)
    tg_configured = bool(get_setting(conn, "telegram_bot_token")
                         or os.environ.get("TELEGRAM_BOT_TOKEN"))
    # map AI 'error'→'stale' so it reuses the red dot styling; keep raw state under ai.
    status["ai"] = {"ok": "ok", "error": "stale", "off": "off"}.get(ai["state"], "off")
    # nav badge: how many things need attention now. triage/backup count only when stale
    # ('off'/never-run is setup-pending, they auto-start). AI counts whenever it's not 'ok'.
    alerts = sum(1 for k in ("triage", "backup") if status.get(k) == "stale")
    # capture: stale always counts; a STOPPED (off) daemon also counts when a bot is
    # configured — a connected Telegram bot with the daemon down is a real error, not setup.
    if status.get("capture") == "stale" or (tg_configured and status.get("capture") != "ok"):
        alerts += 1
    if ai["state"] != "ok":
        alerts += 1
    alerts += integ_pending          # a half-finished integration (creds saved, not connected)
    return {"health": status, "health_age": ages, "ai_health": ai, "nav_alerts": alerts}
