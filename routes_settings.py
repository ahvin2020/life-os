"""Settings: the ONE UI for the key/value `settings` table.

Toggles + timing for the proactive AI surfaces, voice transcription overrides, and
housekeeping thresholds — all read by the daemon/proactive but previously unwritable.
Blank field = reset to the code default (row deleted). Validation is atomic: nothing
is written until every field parses. A read-only System status card mirrors the
sidebar health dots. Field defaults live in DEFAULTS (single source for placeholders
and validation); toggles default ON (missing row = enabled).
"""

from __future__ import annotations

import os
import subprocess
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, render_template, request, jsonify

from datetime import timedelta

from web_core import db, respond, health_status
from db import (get_setting, set_setting, delete_setting, machine_tz_name,
                reload_tz, now_sg, get_tz)

bp = Blueprint("settings", __name__)

_ROOT = os.path.dirname(os.path.abspath(__file__))

# A short, curated pick-list for the timezone <select> (phone-first: no 600-row
# datalist). The effective value and the machine zone are always injected so the
# current selection is present even if it's off this list.
_COMMON_TZS = [
    "Asia/Singapore", "Asia/Kuala_Lumpur", "Asia/Jakarta", "Asia/Bangkok",
    "Asia/Hong_Kong", "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul",
    "Asia/Taipei", "Asia/Manila", "Asia/Kolkata", "Asia/Dubai",
    "Australia/Sydney", "Australia/Perth", "Pacific/Auckland",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Sao_Paulo", "UTC",
]

DEFAULTS = {
    "digest_hour": "07:00",
    "reflection_hour": "21:30",
    "voice_language": "en",
    "archive_done_days": "7",
    "purge_deleted_days": "30",
    "stale_backlog_days": "30",
    "backup_keep": "7",
    "triage_time": "09:00",
}
TOGGLES = ("brief_enabled", "triage_enabled", "reflection_enabled")
_TRIAGE_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "daily")

_DAYS_LABELS = {
    "archive_done_days": "Archive-done days",
    "purge_deleted_days": "Purge-deleted days",
    "stale_backlog_days": "Stale-backlog days",
}


def _hhmm(val, dh, dm):
    """Parse an 'HH:MM' (or bare 'HH') setting into (hour, minute)."""
    try:
        s = str(val)
        h, m = (s.split(":", 1) + ["0"])[:2] if ":" in s else (s, "0")
        return int(h), int(m)
    except (TypeError, ValueError):
        return dh, dm


def _next_label(now, h, m, ran_today):
    """When a daily job at HH:MM next fires, as a glanceable label ('today 07:00' /
    'tomorrow 07:00' / 'due now'). ran_today collapses an already-done job to tomorrow."""
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if not ran_today and cand < now:
        return "due now"
    dt = cand if (not ran_today and cand >= now) else cand + timedelta(days=1)
    when = "today" if dt.date() == now.date() else "tomorrow"
    return f"{when} {dt.strftime('%H:%M')}"


def _ran_today(conn, key, now):
    """Was this heartbeat/guard stamped for the app-tz 'today'? Accepts a bare date
    (guards) or a UTC ISO timestamp (heartbeats), both normalised to the app zone."""
    val = get_setting(conn, key)
    if not val:
        return False
    s = str(val)
    if "T" in s:  # UTC audit timestamp → app-tz date
        from datetime import datetime, timezone
        try:
            s = (datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
                 .replace(tzinfo=timezone.utc).astimezone(get_tz()).date().isoformat())
        except Exception:
            return False
    return s[:10] == now.date().isoformat()


@bp.route("/settings")
def settings_page():
    conn = db()
    values = {k: get_setting(conn, k) for k in DEFAULTS}
    # normalize a legacy bare-hour digest_hour ("7") to HH:MM so <input type=time> shows it
    if values.get("digest_hour") and ":" not in values["digest_hour"]:
        try:
            values["digest_hour"] = f"{int(values['digest_hour']):02d}:00"
        except ValueError:
            pass
    toggles = {k: get_setting(conn, k, "1") != "0" for k in TOGGLES}
    backup_location = get_setting(conn, "backup_location") or ""
    # the resolved default offsite dir (shown per-machine so blank stays portable
    # across the Mac↔NAS sync — mirrors backup_db.synced_dir())
    backup_dir_default = os.path.join(_ROOT, "data-backups")
    triage_day = get_setting(conn, "triage_day") or "sun"
    machine_tz = machine_tz_name()
    tz_current = get_setting(conn, "app_tz") or machine_tz    # what's active now
    # curated list + whatever's active + the machine zone, de-duped, order preserved
    tz_options = list(dict.fromkeys(_COMMON_TZS + [machine_tz, tz_current]))
    status = {
        "health": health_status(conn),
        "digest_last_sent": get_setting(conn, "digest_last_sent"),
        "reflection_last_sent": get_setting(conn, "reflection_last_sent"),
        "capture_last_ran": get_setting(conn, "capture_last_ran"),
        "triage_last_ran": get_setting(conn, "triage_last_ran"),
        "backup_last_ran": get_setting(conn, "backup_last_ran"),
    }
    # Next scheduled fire per job (app tz), honouring the once-per-day guards + toggles.
    now = now_sg()
    nextrun = {"capture": "live",
               "triage": _next_label(now, 0, 0, _ran_today(conn, "sweep_last_day", now)),
               "backup": _next_label(now, 3, 0, _ran_today(conn, "backup_last_ran", now))}
    if toggles.get("brief_enabled", True):
        dh, dm = _hhmm(values.get("digest_hour"), 7, 0)
        nextrun["brief"] = _next_label(now, dh, dm, _ran_today(conn, "digest_last_sent", now))
    if toggles.get("reflection_enabled", True):
        rh, rm = _hhmm(values.get("reflection_hour"), 21, 30)
        nextrun["reflection"] = _next_label(now, rh, rm, _ran_today(conn, "reflection_last_sent", now))
    conn.close()
    return render_template("settings.html", active="settings",
                           values=values, defaults=DEFAULTS, toggles=toggles,
                           status=status, tz_current=tz_current, nextrun=nextrun,
                           machine_tz=machine_tz, tz_options=tz_options,
                           backup_location=backup_location, triage_day=triage_day,
                           backup_dir_default=backup_dir_default,
                           triage_days=_TRIAGE_DAYS)


@bp.route("/settings/run/<job>", methods=["POST"])
def settings_run(job):
    """Trigger a background job on demand from the System-status card. Restart the capture
    daemon / run the triage sweep / run the nightly backup now. Single-user + CSRF-guarded;
    fire-and-forget so the request returns immediately."""
    uid = os.getuid()
    try:
        if job == "capture":
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.kelvin.lifeos.capture"],
                           check=True, capture_output=True, text=True, timeout=15)
            msg = "Capture daemon restarting"
        elif job == "backup":
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.kelvin.lifeos.backup"],
                           check=True, capture_output=True, text=True, timeout=15)
            msg = "Backup started"
        elif job == "triage":
            subprocess.Popen([sys.executable, os.path.join(_ROOT, "triage", "run_triage.py"), "--sweep"],
                             cwd=_ROOT)
            msg = "Triage sweep started"
        else:
            return jsonify({"status": "error", "message": "unknown job"}), 400
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "message": (e.stderr or str(e)).strip()[:200]}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)[:200]}), 500
    return jsonify({"status": "ok", "job": job, "message": msg})


def _parse_hhmm(raw):
    """Tolerant HH:MM / bare-HH parser → 'HH:MM' or None if malformed."""
    parts = raw.split(":")
    if len(parts) == 1:
        h, m = parts[0], "0"
    elif len(parts) == 2:
        h, m = parts
    else:
        return None
    try:
        h, m = int(h), int(m)
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


@bp.route("/settings/save", methods=["POST"])
def settings_save():
    f = request.form
    staged = {}          # DEFAULTS key -> str value, or None to delete (reset)

    # app_tz: blank OK (→ machine timezone), else a valid IANA zone name
    tz_raw = (f.get("app_tz") or "").strip()
    tz_staged = None                       # None = delete row (reset to machine tz)
    if tz_raw:
        try:
            ZoneInfo(tz_raw)
        except (ZoneInfoNotFoundError, ValueError):
            return respond(False, "Unknown timezone", fallback="/settings")
        tz_staged = tz_raw

    # digest_hour: blank OK, else HH:MM (minute-precise morning brief)
    raw = (f.get("digest_hour") or "").strip()
    if not raw:
        staged["digest_hour"] = None
    else:
        parsed = _parse_hhmm(raw)
        if parsed is None:
            return respond(False, "Morning brief time must be HH:MM", fallback="/settings")
        staged["digest_hour"] = parsed

    # reflection_hour: blank OK, else HH:MM or bare HH
    raw = (f.get("reflection_hour") or "").strip()
    if not raw:
        staged["reflection_hour"] = None
    else:
        parsed = _parse_hhmm(raw)
        if parsed is None:
            return respond(False, "Reflection time must be HH:MM", fallback="/settings")
        staged["reflection_hour"] = parsed

    # three *_days: blank OK, else int 1–365
    for key, label in _DAYS_LABELS.items():
        raw = (f.get(key) or "").strip()
        if not raw:
            staged[key] = None
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return respond(False, f"{label} must be 1–365 days", fallback="/settings")
        if not (1 <= n <= 365):
            return respond(False, f"{label} must be 1–365 days", fallback="/settings")
        staged[key] = str(n)

    # voice_language: blank OK, else ≤10 chars, letters/hyphen only
    raw = (f.get("voice_language") or "").strip()
    if not raw:
        staged["voice_language"] = None
    else:
        if len(raw) > 10 or not raw.replace("-", "").isalpha():
            return respond(False, "Voice language must be letters/hyphens (≤10)",
                           fallback="/settings")
        staged["voice_language"] = raw

    # backup_location: blank OK (→ default offsite dir), else a path string
    raw = (f.get("backup_location") or "").strip()
    staged["backup_location"] = raw[:255] if raw else None

    # backup_keep: blank OK, else int 1–365 (retention count)
    raw = (f.get("backup_keep") or "").strip()
    if not raw:
        staged["backup_keep"] = None
    else:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return respond(False, "Keep backups must be 1–365", fallback="/settings")
        if not (1 <= n <= 365):
            return respond(False, "Keep backups must be 1–365", fallback="/settings")
        staged["backup_keep"] = str(n)

    # triage_time: blank OK, else HH:MM
    raw = (f.get("triage_time") or "").strip()
    if not raw:
        staged["triage_time"] = None
    else:
        parsed = _parse_hhmm(raw)
        if parsed is None:
            return respond(False, "Triage time must be HH:MM", fallback="/settings")
        staged["triage_time"] = parsed

    # triage_day: one of mon..sun / daily; blank or unknown → reset to default (Sunday)
    raw = (f.get("triage_day") or "").strip().lower()
    staged["triage_day"] = raw if raw in _TRIAGE_DAYS else None

    # everything validated — now write atomically
    conn = db()
    if tz_staged is None:
        delete_setting(conn, "app_tz")
    else:
        set_setting(conn, "app_tz", tz_staged)
    for k in TOGGLES:
        set_setting(conn, k, "1" if f.get(k) else "0")
    for k, v in staged.items():
        if v is None:
            delete_setting(conn, k)
        else:
            set_setting(conn, k, v)
    conn.close()
    reload_tz()          # drop the cached zone so date logic picks up the change now
    return respond(True, "Settings saved", to="/settings")
