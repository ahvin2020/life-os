#!/usr/bin/env python3
"""Flask app + shared web plumbing for Life OS.

Lifted from youtube-assistant/executors/invoicing/web_core.py: module-level Flask
app, persisted secret key, hand-rolled CSRF (session token + before_request guard +
the fetch/form patching that lives in base.html), respond() AJAX-or-redirect helper,
db() accessor reading module-level _DB_PATH, make_test_client(), and the fdate /
days_ago Jinja filters. All money/Dropbox/privacy/entity code stripped.

Route blueprints import * from here.
"""

from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timezone

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, session, abort,
)

from core.db import connect, now_iso, today_iso, now_sg, get_tz, DB_PATH

# Repo root (this module lives in core/), so templates/static/data resolve there
# rather than relative to the package dir.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__,
            template_folder=os.path.join(_ROOT, "web", "templates"),
            static_folder=os.path.join(_ROOT, "web", "static"))

_DATA_DIR = os.path.join(_ROOT, "data")


def _load_or_create_secret_key() -> str:
    """Persist a random Flask secret key to data/secret_key (gitignored, chmod 600)
    so sessions survive restarts instead of being invalidated by an ephemeral key."""
    path = os.path.join(_DATA_DIR, "secret_key")
    try:
        if os.path.exists(path):
            key = open(path).read().strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(key)
        os.chmod(path, 0o600)
    except OSError:
        pass                                    # in-memory fallback for read-only FS
    return key


app.secret_key = _load_or_create_secret_key()


# ── CSRF protection (no external dependency) ──────────────────────────────────
_CSRF_FIELD = "csrf_token"
_CSRF_HEADER = "X-CSRFToken"
_CSRF_SESSION_KEY = "_csrf_token"


def csrf_token() -> str:
    """The active session's CSRF token, minting one on first use."""
    tok = session.get(_CSRF_SESSION_KEY)
    if not tok:
        tok = secrets.token_hex(32)
        session[_CSRF_SESSION_KEY] = tok
    return tok


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def _csrf_protect():
    """Reject mutating requests whose CSRF token is missing or mismatched. Read-only
    methods pass through. The token rides as a form field or the X-CSRFToken header."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    expected = session.get(_CSRF_SESSION_KEY)
    sent = request.form.get(_CSRF_FIELD) or request.headers.get(_CSRF_HEADER) or ""
    if not expected or not sent or not hmac.compare_digest(str(expected), str(sent)):
        abort(403)
    return None


def make_test_client():
    """Flask test client that transparently attaches a real CSRF token to every
    mutating request, so tests exercise the real protection without boilerplate."""
    from flask.testing import FlaskClient

    class _CsrfClient(FlaskClient):
        def open(self, *args, **kwargs):
            method = (kwargs.get("method") or (args[1] if len(args) > 1 else "") or "GET").upper()
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.session_transaction() as sess:
                    tok = sess.get(_CSRF_SESSION_KEY)
                    if not tok:
                        tok = secrets.token_hex(32)
                        sess[_CSRF_SESSION_KEY] = tok
                headers = kwargs.get("headers")
                headers = dict(headers) if headers else {}
                headers.setdefault(_CSRF_HEADER, tok)
                kwargs["headers"] = headers
            return super().open(*args, **kwargs)

    app.test_client_class = _CsrfClient
    return app.test_client()


# ── Jinja filters ─────────────────────────────────────────────────────────────
def _fmt_date(value):
    """ISO date → '9 Jul 2026'. Empty → em dash."""
    if not value:
        return "—"
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        return f"{d.day} {d.strftime('%b')} {d.year}"
    except Exception:
        return value


def _fmt_stamp(value):
    """UTC ISO audit timestamp → app-tz '10 Jul 21:07' (year shown only when it isn't
    this year). Used for the heartbeat 'ran ...' lines. Empty → 'never'."""
    if not value:
        return "never"
    try:
        dt = (datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")
              .replace(tzinfo=timezone.utc).astimezone(get_tz()))
    except Exception:
        return value
    stamp = f"{dt.day} {dt.strftime('%b')} {dt.strftime('%H:%M')}"
    if dt.year != now_sg().year:
        stamp = f"{dt.day} {dt.strftime('%b')} {dt.year}, {dt.strftime('%H:%M')}"
    return stamp


def _days_ago(value):
    """Days between today (SG) and value; positive = past, negative = future."""
    if not value:
        return None
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        return (now_sg().date() - d).days
    except Exception:
        return None


def _due_label(value):
    """Human due label relative to today, compact + glanceable: today / yesterday /
    '3d over' (severity without date math) / tomorrow / weekday inside a week /
    '13 Jul' (the year only when it isn't this year)."""
    n = _days_ago(value)
    if n is None:
        return _fmt_date(value)
    if n == 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n > 1:
        return f"{n}d over"
    if n == -1:
        return "tomorrow"
    d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    if n >= -6:
        return d.strftime("%a")
    if d.year == now_sg().year:
        return f"{d.day} {d.strftime('%b')}"
    return _fmt_date(value)


app.jinja_env.filters["fdate"] = _fmt_date
app.jinja_env.filters["fstamp"] = _fmt_stamp
app.jinja_env.filters["days_ago"] = _days_ago
app.jinja_env.filters["due_label"] = _due_label


# ── request helpers ───────────────────────────────────────────────────────────
def is_ajax() -> bool:
    """True when the request came from our fetch() helpers (X-Requested-With).
    Single source — route blueprints import this rather than re-declaring it."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def respond(ok, msg, to=None, fallback=None, extra=None):
    """AJAX-or-redirect reply shared by POST action routes.

    On an AJAX request returns JSON; otherwise flashes and redirects to `to` (or
    `fallback`). `extra` (a dict) is merged into the JSON payload — e.g. the new row
    id from a create route — so callers stay on this one helper instead of forking."""
    if is_ajax():
        payload = {"status": "ok" if ok else "error", "message": msg}
        if extra:
            payload.update(extra)
        return jsonify(payload), (200 if ok else 400)
    flash(msg, "success" if ok else "error")
    return redirect(to or fallback or "/")


# ── DB accessor ───────────────────────────────────────────────────────────────
_DB_PATH = DB_PATH  # overridden by --db flag at startup (server.py sets _wc._DB_PATH)


def db():
    return connect(_DB_PATH)


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


def _parse_iso_utc(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def health_status(conn, now=None) -> dict:
    """Per-job dot state from the settings heartbeats: 'ok' | 'stale' | 'off'.
    Pure enough to unit-test: pass a seeded conn and a fixed `now`."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for name, key, budget_min in _HEALTH_CHECKS:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        ts = _parse_iso_utc(row["value"]) if row else None
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
        ts = _parse_iso_utc(row["value"]) if row else None
        out[name] = _fmt_age((now - ts).total_seconds()) if ts else None
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
    ok_ts = _parse_iso_utc(ok_raw)
    # err value is "ISO | reason"
    err_ts = _parse_iso_utc((err_raw or "").split("|", 1)[0].strip()) if err_raw else None
    detail = (err_raw or "").split("|", 1)[1].strip() if (err_raw and "|" in err_raw) else ""
    if err_ts and (ok_ts is None or err_ts > ok_ts):
        return {"state": "error", "detail": detail or "call failed",
                "auth": detail.lower().startswith("auth")}
    if ok_ts is not None:
        return {"state": "ok", "detail": "", "auth": False}
    return {"state": "off", "detail": "", "auth": False}


@app.context_processor
def inject_health():
    """Make the health dots + last-ran ages available to every template."""
    status = {"capture": "off", "triage": "off", "backup": "off"}
    ages = {"capture": None, "triage": None, "backup": None}
    ai = {"state": "off", "detail": "", "auth": False}
    try:
        conn = db()
        status = health_status(conn)
        ages = health_ages(conn)
        ai = ai_health(conn)
        conn.close()
    except Exception:
        pass
    # map AI 'error'→'stale' so it reuses the red dot styling; keep raw state under ai.
    status["ai"] = {"ok": "ok", "error": "stale", "off": "off"}.get(ai["state"], "off")
    return {"health": status, "health_age": ages, "ai_health": ai}


@app.context_processor
def inject_nav():
    """Sidebar/bottom-nav badge counts, available to every template."""
    from domain import vault_store
    counts = {"tasks": 0, "notes": 0}
    try:
        conn = db()
        counts["tasks"] = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_id IS NULL "
            "AND archived_at IS NULL AND deleted_at IS NULL AND done = 0").fetchone()[0]
        conn.close()
    except Exception:
        pass
    try:
        counts["notes"] = len(vault_store.list_notes())
    except Exception:
        pass
    return {"nav_counts": counts, "today": today_iso()}


@app.context_processor
def inject_asset_ver():
    """Cache-bust static assets by file mtime — the no-build-step app is synced
    to the NAS, so a stale app.css/app.js is otherwise served after every edit."""
    import os
    static_dir = os.path.join(_ROOT, "web", "static")

    def asset_ver(filename):
        try:
            return int(os.path.getmtime(os.path.join(static_dir, filename)))
        except OSError:
            return 0
    return {"asset_ver": asset_ver}
