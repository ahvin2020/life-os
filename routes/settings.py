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
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, render_template, request, jsonify, redirect, flash, session

from datetime import timedelta

from core.web_core import db, respond, health_status, ai_health, health_reasons
from core.db import (get_setting, set_setting, delete_setting, machine_tz_name,
                reload_tz, reload_time_format, now_sg, get_tz, now_iso)

bp = Blueprint("settings", __name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    "time_format": "24h",
    "digest_hour": "07:00",
    "reflection_hour": "21:30",
    "voice_language": "en",
    "archive_done_days": "7",
    "purge_deleted_days": "30",
    "stale_backlog_days": "30",
    "backup_keep": "7",
    "triage_time": "09:00",
    "weekly_time": "18:00",
    "monthly_time": "17:00",
}
TOGGLES = ("brief_enabled", "triage_enabled", "reflection_enabled", "weekly_enabled",
           "monthly_enabled", "docscan_enabled")

# AI providers — authenticated by SUBSCRIPTION OAuth (e.g. `claude setup-token`), NOT
# pay-per-use API keys (that stays out per CLAUDE.md). Only a 'wired' provider actually
# executes; the rest show in the picker as the roadmap (disabled) so it's an honest
# chooser. Adding a real provider = flip wired + fill oauth_cmd/manage_url here, then
# teach ai/claude_cli (or the router) to invoke its CLI. The active choice lives in
# settings.ai_provider; each token in settings.<id>_oauth_token.
AI_PROVIDERS = [
    {"id": "claude", "label": "Claude", "wired": True,
     "oauth_cmd": "claude setup-token", "manage_url": "https://claude.ai/settings"},
    {"id": "gemini", "label": "Gemini", "wired": False, "oauth_cmd": "", "manage_url": ""},
    {"id": "codex", "label": "ChatGPT / Codex", "wired": False, "oauth_cmd": "", "manage_url": ""},
]
_AI_DEFAULT = "claude"
_AI_WIRED = {p["id"] for p in AI_PROVIDERS if p["wired"]}


def _active_provider(conn):
    """The chosen provider dict, falling back to the default if the stored id is unknown
    or has since been un-wired."""
    pid = get_setting(conn, "ai_provider") or _AI_DEFAULT
    for p in AI_PROVIDERS:
        if p["id"] == pid and p["wired"]:
            return p
    return AI_PROVIDERS[0]
_TRIAGE_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "daily")

_DAYS_LABELS = {
    "archive_done_days": "Archive-done days",
    "purge_deleted_days": "Purge-deleted days",
    "stale_backlog_days": "Stale-backlog days",
}


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
    weekly_day = get_setting(conn, "weekly_day") or "sun"
    docscan_day = get_setting(conn, "docscan_day") or "daily"
    machine_tz = machine_tz_name()
    tz_current = get_setting(conn, "app_tz") or machine_tz    # what's active now
    # curated list + whatever's active + the machine zone, de-duped, order preserved
    tz_options = list(dict.fromkeys(_COMMON_TZS + [machine_tz, tz_current]))
    status = {
        "health": health_status(conn),
        "capture_last_ran": get_setting(conn, "capture_last_ran"),
        "triage_last_ran": get_setting(conn, "triage_last_ran"),
        "backup_last_ran": get_setting(conn, "backup_last_ran"),
        "claude_last_ok": get_setting(conn, "claude_last_ok"),
    }
    # Document folders (facts cache / retrieval sources) — stored as a JSON list, shown
    # one path per line; app_base_url powers the Tailscale-only file links.
    import json as _json
    try:
        doc_roots_list = _json.loads(get_setting(conn, "document_roots", "") or "[]")
    except (ValueError, TypeError):
        doc_roots_list = []
    document_roots_text = "\n".join(p for p in doc_roots_list if isinstance(p, str))
    app_base_url = get_setting(conn, "app_base_url") or ""
    # Connected-integration status for the connect/disconnect cards.
    from ai import google_client
    g_conn = google_client.is_configured()
    d_conn = bool(get_setting(conn, "dropbox_token"))
    integrations = {
        "google": {"creds_set": bool(get_setting(conn, "google_client_id")
                                     and get_setting(conn, "google_client_secret")),
                   "connected": g_conn,
                   "failing": g_conn and bool(get_setting(conn, "google_last_err"))},
        "dropbox": {"creds_set": bool(get_setting(conn, "dropbox_app_key")
                                      and get_setting(conn, "dropbox_app_secret")),
                    "connected": d_conn,
                    "failing": d_conn and bool(get_setting(conn, "dropbox_last_err"))},
        "telegram": {"connected": bool(get_setting(conn, "telegram_bot_token")
                                       or os.environ.get("TELEGRAM_BOT_TOKEN")),
                     "user": (get_setting(conn, "telegram_allowed_user")
                              or os.environ.get("TELEGRAM_ALLOWED_USER_ID") or "")},
    }
    ai = ai_health(conn)
    active_provider = _active_provider(conn)
    ai["token_set"] = (bool(get_setting(conn, f"{active_provider['id']}_oauth_token"))
                       or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")))
    status["health"]["ai"] = {"ok": "ok", "error": "stale", "off": "off"}.get(ai["state"], "off")
    health_why = health_reasons(conn)          # why each red dot is red (computed before close)
    # The "Restart capture" button only works where launchctl exists (the Mac). In the
    # NAS container capture is its own service — hide the button there.
    capture_restartable = shutil.which("launchctl") is not None
    # Next scheduled fire per job (app tz), honouring the once-per-day guards + toggles.
    now = now_sg()
    nextrun = {"capture": "live",
               "triage": _next_label(now, 0, 0, _ran_today(conn, "sweep_last_day", now)),
               "backup": _next_label(now, 3, 0, _ran_today(conn, "backup_last_ran", now))}
    conn.close()
    return render_template("settings.html", active="settings",
                           values=values, defaults=DEFAULTS, toggles=toggles,
                           status=status, tz_current=tz_current, nextrun=nextrun,
                           machine_tz=machine_tz, tz_options=tz_options,
                           backup_location=backup_location, triage_day=triage_day,
                           weekly_day=weekly_day, docscan_day=docscan_day,
                           backup_dir_default=backup_dir_default,
                           triage_days=_TRIAGE_DAYS, ai=ai,
                           ai_providers=AI_PROVIDERS, active_provider=active_provider,
                           document_roots_text=document_roots_text, app_base_url=app_base_url,
                           integrations=integrations,
                           health_why=health_why, capture_restartable=capture_restartable)


@bp.route("/settings/run/<job>", methods=["POST"])
def settings_run(job):
    """Trigger a background job on demand from the System-status card. Restart the capture
    daemon / run the triage sweep / run the nightly backup now. Single-user + CSRF-guarded;
    fire-and-forget so the request returns immediately."""
    uid = os.getuid()
    try:
        if job == "capture":
            if not shutil.which("launchctl"):     # NAS container: no launchd to kick
                return jsonify({"status": "error",
                                "message": "Capture runs as a container here — restart it from Container Manager"}), 400
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.kelvin.lifeos.capture"],
                           check=True, capture_output=True, text=True, timeout=15)
            msg = "Capture daemon restarting"
        elif job == "backup":
            # Run the backup job directly (portable) rather than via launchctl, which
            # only exists on the Mac — in the NAS container we just invoke the script.
            from core import web_core as _wc
            env = {**os.environ, "LIFEOS_DB_PATH": _wc._DB_PATH}
            subprocess.Popen([sys.executable, os.path.join(_ROOT, "scripts", "backup_db.py")],
                             cwd=_ROOT, env=env)
            msg = "Backup started"
        elif job == "triage":
            subprocess.Popen([sys.executable, os.path.join(_ROOT, "triage", "run_triage.py"), "--sweep"],
                             cwd=_ROOT)
            msg = "Triage sweep started"
        elif job == "claude":
            # Validate synchronously. If the form passed a pasted token, trial THAT one
            # WITHOUT saving (record=False, so a mistyped token doesn't flip the live dot
            # or fire the Telegram nudge); otherwise probe the saved token (record=True).
            from ai.claude_cli import call_claude
            tok = (request.form.get("oauth_token") or "").strip()
            out = call_claude("Reply with exactly the word: ok", timeout=30,
                              token=tok or None, record=not tok)
            if out and out.strip():
                msg = "AI reachable ✓"
            else:
                return jsonify({"status": "error",
                                "message": "AI unreachable — check the token"}), 502
        else:
            return jsonify({"status": "error", "message": "unknown job"}), 400
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "message": (e.stderr or str(e)).strip()[:200]}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)[:200]}), 500
    return jsonify({"status": "ok", "job": job, "message": msg})


@bp.route("/settings/claude-token", methods=["POST"])
def settings_claude_token():
    """Save the active AI provider + its OAuth token, WITHOUT going through
    /settings/save — a password field renders blank, so folding it into the main save
    would wipe the token on every unrelated save. Its own form + button: a value sets
    it, an empty submit is an explicit clear. Provider-aware (settings.ai_provider +
    <id>_oauth_token) but only a WIRED provider is accepted. call_claude reads the token
    live, so no container restart is needed to rotate it."""
    provider = (request.form.get("ai_provider") or _AI_DEFAULT).strip()
    if provider not in _AI_WIRED:
        return respond(False, "That provider isn't wired up yet", fallback="/settings")
    # accept the generic field name, or the legacy claude-specific one
    token = (request.form.get("oauth_token") or request.form.get("claude_oauth_token") or "").strip()
    conn = db()
    set_setting(conn, "ai_provider", provider)
    if token:
        set_setting(conn, f"{provider}_oauth_token", token)
        # Save is gated behind a passing Test, so the token is verified as of now — mark
        # it connected so the status goes green immediately, and clear the old failure +
        # nudge guard so a later lapse still notifies.
        set_setting(conn, "claude_last_ok", now_iso())
        delete_setting(conn, "claude_last_err")
        delete_setting(conn, "claude_down_notified")
        msg = "Token saved"
    else:
        delete_setting(conn, f"{provider}_oauth_token")
        msg = "Token cleared"
    conn.close()
    return respond(True, msg, to="/settings")


@bp.route("/settings/test/<provider>", methods=["POST"])
def settings_test(provider):
    """Live connection check for a connected provider — pings its API, records the ok/err
    heartbeat, and returns reachable/not. Catches an expired token that 'Connected' hides."""
    conn = db()
    ok, msg = False, "unknown"
    try:
        if provider == "claude":
            from ai.claude_cli import call_claude
            ok = bool((call_claude("Reply with exactly: ok", timeout=30) or "").strip())
            msg = "Claude reachable ✓" if ok else "Claude unreachable — check the token"
        elif provider == "google":
            from ai import google_client
            if not google_client.is_configured():
                conn.close()
                return jsonify({"status": "error", "message": "Not connected"}), 400
            google_client.calendar_today(now_sg().date().isoformat())   # records the heartbeat
            ok = not get_setting(conn, "google_last_err")
            msg = "Google reachable ✓" if ok else "Google call failed — reconnect"
        elif provider == "dropbox":
            from ai import dropbox_client
            if not dropbox_client.is_configured(conn):
                conn.close()
                return jsonify({"status": "error", "message": "Not connected"}), 400
            dropbox_client.list_documents(conn, cap=1)                  # records the heartbeat
            ok = not get_setting(conn, "dropbox_last_err")
            msg = "Dropbox reachable ✓" if ok else "Dropbox call failed — reconnect"
        elif provider == "telegram":
            from ai.telegram_api import Telegram
            tok = get_setting(conn, "telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
            chat = get_setting(conn, "telegram_allowed_user") or os.environ.get("TELEGRAM_ALLOWED_USER_ID")
            if not (tok and chat):
                conn.close()
                return jsonify({"status": "error", "message": "Not connected"}), 400
            res = Telegram(tok).send_message(chat, "✅ Life OS test — your bot is connected and can reach you.")
            ok = bool(res and res.get("ok"))
            # A valid token that still can't deliver almost always means you haven't messaged the bot yet.
            msg = "Sent you a message ✓ — check Telegram" if ok else \
                (res or {}).get("description") or "Couldn't send — open Telegram and message your bot once first"
        else:
            conn.close()
            return jsonify({"status": "error", "message": "unknown provider"}), 400
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)[:120]}), 502
    conn.close()
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 502)


def _base_url(conn):
    """The app's external base URL (explicit app_base_url override, else the request host),
    trailing slash stripped — used to build OAuth redirects + file links."""
    return (get_setting(conn, "app_base_url") or request.host_url).rstrip("/")


# ── Google connect/disconnect (browser OAuth — replaces the JSON-file + script) ──
def _google_redirect_uri(conn):
    return _base_url(conn) + "/settings/google/callback"


@bp.route("/settings/app-url", methods=["POST"])
def settings_app_url():
    """Save the app's base URL (used to build OAuth redirect + file links)."""
    conn = db()
    base = (request.form.get("app_base_url") or "").strip()
    if base:
        if not base.startswith(("http://", "https://")):
            conn.close()
            return respond(False, "URL must start with http:// or https://", fallback="/settings")
        set_setting(conn, "app_base_url", base.rstrip("/")[:255])
    else:
        delete_setting(conn, "app_base_url")
    conn.close()
    return respond(True, "App URL saved", to="/settings")


@bp.route("/settings/google-creds", methods=["POST"])
def settings_google_creds():
    """Save the pasted Google client ID + secret (from the one-time Google Cloud setup),
    then Connect does the browser OAuth. Own route so it persists independently."""
    conn = db()
    cid = (request.form.get("google_client_id") or "").strip()
    csec = (request.form.get("google_client_secret") or "").strip()
    if cid:
        set_setting(conn, "google_client_id", cid)
    if csec:
        set_setting(conn, "google_client_secret", csec)
    delete_setting(conn, "google_disconnected")   # fresh creds → a real setup; let the nudge fire
    conn.close()
    return respond(True, "Google app credentials saved — now click Connect", to="/settings")


@bp.route("/settings/google/connect")
def settings_google_connect():
    """Kick off the browser OAuth: redirect to Google's consent screen."""
    conn = db()
    cid = get_setting(conn, "google_client_id")
    csec = get_setting(conn, "google_client_secret")
    redirect_uri = _google_redirect_uri(conn)
    conn.close()
    from ai import google_client
    if not (cid and csec):
        flash("Add your Google client ID + secret first", "error")
        return redirect("/settings")
    if not google_client.sdk_available():
        flash("Google libraries not installed — run: pip install -r requirements.txt", "error")
        return redirect("/settings")
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")   # http over the private tailnet
    try:
        flow = google_client.build_flow(cid, csec, redirect_uri)
        auth_url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                                 include_granted_scopes="true")
    except Exception as e:
        flash(f"Couldn't start Google connect: {str(e)[:100]}", "error")
        return redirect("/settings")
    session["google_oauth_state"] = state
    # PKCE (on by default now): the callback runs a fresh flow, so the one-time verifier
    # generated here must ride the session across the round-trip or fetch_token rejects it.
    session["google_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@bp.route("/settings/google/callback")
def settings_google_callback():
    """Google redirects back here with a code; exchange it and save the token server-side."""
    conn = db()
    cid = get_setting(conn, "google_client_id")
    csec = get_setting(conn, "google_client_secret")
    redirect_uri = _google_redirect_uri(conn)
    conn.close()
    from ai import google_client
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    # Google returns scopes reordered (and, with incremental auth, previously-granted extras),
    # so the returned set rarely matches the request byte-for-byte — relax or fetch_token raises.
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow = google_client.build_flow(cid, csec, redirect_uri, state=session.get("google_oauth_state"))
        flow.code_verifier = session.get("google_code_verifier")   # restore the PKCE verifier
        flow.fetch_token(authorization_response=request.url)
        google_client.save_token(flow.credentials.to_json())
        conn = db()                          # a fresh token → drop any stale failure note
        delete_setting(conn, "google_last_err")
        delete_setting(conn, "google_disconnected")
        conn.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
        flash(f"Google connect failed: {str(e)[:120]}", "error")
        return redirect("/settings")
    flash("Google connected ✓", "success")
    return redirect("/settings")


@bp.route("/settings/google/disconnect", methods=["POST"])
def settings_google_disconnect():
    from ai import google_client
    google_client.forget_token()
    conn = db()
    delete_setting(conn, "google_last_err")
    set_setting(conn, "google_disconnected", "1")   # deliberate off → don't nag as "half-finished"
    conn.close()
    return respond(True, "Google disconnected", to="/settings")


# ── Dropbox connect/disconnect (portable — the app's own OAuth, no NAS sync) ─────
@bp.route("/settings/dropbox-creds", methods=["POST"])
def settings_dropbox_creds():
    conn = db()
    key = (request.form.get("dropbox_app_key") or "").strip()
    sec = (request.form.get("dropbox_app_secret") or "").strip()
    if key:
        set_setting(conn, "dropbox_app_key", key)
    if sec:
        set_setting(conn, "dropbox_app_secret", sec)
    delete_setting(conn, "dropbox_disconnected")   # fresh creds → a real setup; let the nudge fire
    conn.close()
    return respond(True, "Dropbox app credentials saved — now click Connect", to="/settings")


@bp.route("/settings/dropbox/connect")
def settings_dropbox_connect():
    conn = db()
    key = get_setting(conn, "dropbox_app_key")
    sec = get_setting(conn, "dropbox_app_secret")
    base = _base_url(conn)
    conn.close()
    from ai import dropbox_client
    if not (key and sec):
        flash("Add your Dropbox app key + secret first", "error")
        return redirect("/settings")
    if not dropbox_client.sdk_available():
        flash("Dropbox library not installed — run: pip install dropbox", "error")
        return redirect("/settings")
    try:
        flow = dropbox_client.build_flow(key, sec, base + "/settings/dropbox/callback", session)
        return redirect(flow.start())
    except Exception as e:
        flash(f"Couldn't start Dropbox connect: {str(e)[:100]}", "error")
        return redirect("/settings")


@bp.route("/settings/dropbox/callback")
def settings_dropbox_callback():
    conn = db()
    key = get_setting(conn, "dropbox_app_key")
    sec = get_setting(conn, "dropbox_app_secret")
    base = _base_url(conn)
    from ai import dropbox_client
    try:
        flow = dropbox_client.build_flow(key, sec, base + "/settings/dropbox/callback", session)
        result = flow.finish(request.args)
        # offline access → a refresh token; store it (survives without re-consent)
        token = getattr(result, "refresh_token", None) or getattr(result, "access_token", None)
        set_setting(conn, "dropbox_token", token)
        delete_setting(conn, "dropbox_last_err")   # fresh token → drop any stale failure note
        delete_setting(conn, "dropbox_disconnected")
        conn.close()
    except Exception as e:
        conn.close()
        flash(f"Dropbox connect failed: {str(e)[:120]}", "error")
        return redirect("/settings")
    flash("Dropbox connected ✓", "success")
    return redirect("/settings")


@bp.route("/settings/dropbox/disconnect", methods=["POST"])
def settings_dropbox_disconnect():
    conn = db()
    from ai import dropbox_client
    dropbox_client.forget(conn)
    delete_setting(conn, "dropbox_last_err")
    set_setting(conn, "dropbox_disconnected", "1")   # deliberate off → don't nag as "half-finished"
    conn.close()
    return respond(True, "Dropbox disconnected", to="/settings")


# ── Telegram connect/disconnect (bot token + allowed user, stored in Settings) ───
@bp.route("/settings/telegram-creds", methods=["POST"])
def settings_telegram_creds():
    """Save the bot token + allowed user id. Validates the token via getMe so a typo is
    caught here rather than silently failing in the daemon. Restart capture to pick it up."""
    token = (request.form.get("telegram_bot_token") or "").strip()
    user = (request.form.get("telegram_allowed_user") or "").strip()
    conn = db()
    already = bool(get_setting(conn, "telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN"))
    if not token:
        # user-id-only edit on an already-connected bot (keep the existing token)
        if already and user:
            set_setting(conn, "telegram_allowed_user", user)
            conn.close()
            return respond(True, "Updated — restart capture to apply", to="/settings")
        conn.close()
        return respond(False, "Paste your BotFather token", fallback="/settings")
    try:
        from ai.telegram_api import Telegram
        me = Telegram(token).get_me()
        if not (me and me.get("ok")):
            conn.close()
            return respond(False, "That bot token didn't work — check it", fallback="/settings")
    except Exception:
        conn.close()
        return respond(False, "Couldn't reach Telegram — check the token/network", fallback="/settings")
    set_setting(conn, "telegram_bot_token", token)
    if user:
        set_setting(conn, "telegram_allowed_user", user)
    conn.close()
    bot = (me.get("result") or {}).get("username", "")
    return respond(True, f"Connected @{bot} — restart capture to start it", to="/settings")


@bp.route("/settings/telegram/disconnect", methods=["POST"])
def settings_telegram_disconnect():
    conn = db()
    delete_setting(conn, "telegram_bot_token")
    delete_setting(conn, "telegram_allowed_user")
    conn.close()
    return respond(True, "Telegram disconnected", to="/settings")


def _parse_doc_roots(raw: str) -> list:
    """Textarea (one folder path per line) → a de-duped list of non-empty absolute paths.
    Relative/blank lines are dropped."""
    out = []
    for line in (raw or "").splitlines():
        p = line.strip()
        if p and os.path.isabs(p) and p not in out:
            out.append(p)
    return out


@bp.route("/settings/test-doc-roots", methods=["POST"])
def settings_test_doc_roots():
    """Probe the pasted document folders WITHOUT saving (mirrors the AI-token Test): for
    each path report whether it's a readable directory and how many documents it holds,
    so Sam sees his synced folders are wired before committing them."""
    from domain import docs
    paths = _parse_doc_roots(request.form.get("document_roots") or "")
    if not paths:
        return jsonify({"status": "error", "message": "Add at least one absolute folder path"}), 400
    reports = []
    total = 0
    for p in paths:
        if not os.path.isdir(p):
            reports.append(f"✗ {p} — not a folder (is Cloud Sync set up?)")
            continue
        if not os.access(p, os.R_OK):
            reports.append(f"✗ {p} — not readable")
            continue
        count = 0
        for _dp, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            count += sum(1 for fn in filenames if fn.lower().endswith(docs._DOC_EXTS))
            if count > 2000:
                break
        total += count
        reports.append(f"✓ {p} — {count} document{'s' if count != 1 else ''}")
    ok = all(r.startswith("✓") for r in reports)
    msg = ("Found " + str(total) + " documents across " + str(len(paths)) + " folder"
           + ("s" if len(paths) != 1 else "")) if ok else "Some folders aren't reachable"
    return jsonify({"status": "ok" if ok else "error", "message": msg,
                    "detail": "\n".join(reports)}), (200 if ok else 400)


@bp.route("/settings/doc-roots", methods=["POST"])
def settings_doc_roots():
    """Save the document folders + the Tailscale base URL for file links. Own route (not
    /settings/save) so the multi-line textarea + URL persist independently of unrelated
    saves, matching the AI-token flow."""
    conn = db()
    paths = _parse_doc_roots(request.form.get("document_roots") or "")
    if paths:
        set_setting(conn, "document_roots", __import__("json").dumps(paths))
    else:
        delete_setting(conn, "document_roots")
    base = (request.form.get("app_base_url") or "").strip()
    if base:
        if not base.startswith(("http://", "https://")):
            conn.close()
            return respond(False, "Base URL must start with http:// or https://", fallback="/settings")
        set_setting(conn, "app_base_url", base.rstrip("/")[:255])
    # Blank here means "leave as-is", NOT "clear": app_base_url is shared with the OAuth
    # redirect and the dedicated /settings/app-url card (which is where it gets cleared).
    # Deleting it from this card on an empty field would silently break OAuth + /docs links.
    conn.close()
    return respond(True, "Document folders saved", to="/settings")


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


class _Invalid(Exception):
    """A field failed validation; its message is what the user should see."""


def _hhmm_field(raw, label):
    """Blank → None; else 'HH:MM' or raise _Invalid('<label> must be HH:MM')."""
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = _parse_hhmm(raw)
    if parsed is None:
        raise _Invalid(f"{label} must be HH:MM")
    return parsed


def _int_range_field(raw, lo, hi, msg):
    """Blank → None; else str(int) within [lo, hi] or raise _Invalid(msg)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise _Invalid(msg)
    if not (lo <= n <= hi):
        raise _Invalid(msg)
    return str(n)


def _enum_field(raw, allowed):
    """Lower-cased value if it's in `allowed`, else None (blank/unknown → reset)."""
    raw = (raw or "").strip().lower()
    return raw if raw in allowed else None


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

    # Field validators raise _Invalid on a bad value (caught once, below). Blank always
    # stages None (= delete the row → reset to the code default). Field order preserved so
    # the first-failing message a user sees is unchanged.
    try:
        # time_format: 12h/24h clock for display; anything else → reset to default (24h)
        raw = (f.get("time_format") or "").strip()
        staged["time_format"] = raw if raw in ("12h", "24h") else None

        # digest_hour: blank OK, else HH:MM (minute-precise morning brief)
        staged["digest_hour"] = _hhmm_field(f.get("digest_hour"), "Morning brief time")

        # reflection_hour: blank OK, else HH:MM or bare HH
        staged["reflection_hour"] = _hhmm_field(f.get("reflection_hour"), "Reflection time")

        # three *_days: blank OK, else int 1–365
        for key, label in _DAYS_LABELS.items():
            staged[key] = _int_range_field(f.get(key), 1, 365, f"{label} must be 1–365 days")

        # voice_language: blank OK, else ≤10 chars, letters/hyphen only
        raw = (f.get("voice_language") or "").strip()
        if not raw:
            staged["voice_language"] = None
        elif len(raw) > 10 or not raw.replace("-", "").isalpha():
            raise _Invalid("Voice language must be letters/hyphens (≤10)")
        else:
            staged["voice_language"] = raw

        # backup_location: blank OK (→ default offsite dir), else a path string
        raw = (f.get("backup_location") or "").strip()
        staged["backup_location"] = raw[:255] if raw else None

        # backup_keep: blank OK, else int 1–365 (retention count)
        staged["backup_keep"] = _int_range_field(f.get("backup_keep"), 1, 365, "Keep backups must be 1–365")

        # triage_time: blank OK, else HH:MM
        staged["triage_time"] = _hhmm_field(f.get("triage_time"), "Triage time")

        # triage_day: one of mon..sun / daily; blank or unknown → reset to default (Sunday)
        staged["triage_day"] = _enum_field(f.get("triage_day"), _TRIAGE_DAYS)

        # weekly_time: blank OK, else HH:MM
        staged["weekly_time"] = _hhmm_field(f.get("weekly_time"), "Weekly review time")

        # weekly_day: one of mon..sun / daily; blank or unknown → reset to default (Sunday)
        staged["weekly_day"] = _enum_field(f.get("weekly_day"), _TRIAGE_DAYS)

        # monthly_time: blank OK, else HH:MM
        staged["monthly_time"] = _hhmm_field(f.get("monthly_time"), "Monthly retrospective time")

        # docscan_day: one of mon..sun / daily; blank or unknown → reset to default (daily)
        staged["docscan_day"] = _enum_field(f.get("docscan_day"), _TRIAGE_DAYS)
    except _Invalid as e:
        return respond(False, str(e), fallback="/settings")

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
    reload_time_format() # ditto the clock format so the next render reflects the choice
    return respond(True, "Settings saved", to="/settings")
