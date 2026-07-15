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
from datetime import datetime

from flask import (
    Flask, render_template, render_template_string, request, redirect, url_for,
    flash, jsonify, session, abort,
)

from core.db import connect, data_dir, now_iso, today_iso, now_sg, get_tz, time_format, DB_PATH
from core.dates import parse_iso_utc, fmt_date, due_label, fmt_clock
# Health + integration-status subsystem lives in core.health (Flask-free, pure over conn).
# Re-exported here because routes/settings and several tests import these from web_core.
from core.health import (  # noqa: F401
    health_status, health_ages, health_reasons, ai_health,
    _integration_pending, health_context,
)

# Repo root (this module lives in core/), so templates/static/data resolve there
# rather than relative to the package dir.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__,
            template_folder=os.path.join(_ROOT, "web", "templates"),
            static_folder=os.path.join(_ROOT, "web", "static"))
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024   # 30 MB cap on image-attachment uploads
# Recompile a template when its .html changes (Flask caches them otherwise, since we run
# debug=False). The .py self-reloader (reloader.py) only watches .py, so without this a
# synced template edit stays invisible until a manual restart — this makes the Synology
# sync the deploy channel for templates too, not just .py. Per-request stat cost is
# negligible for a single-user app.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

_DATA_DIR = data_dir()          # the persistent mount, not /app/data — see core.db.data_dir.
                                # The secret key lives here: inside the image it was regenerated
                                # on every deploy, silently invalidating sessions + CSRF tokens.


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
def _fmt_stamp(value):
    """UTC ISO audit timestamp → app-tz '10 Jul 21:07' (year shown only when it isn't
    this year). Used for the heartbeat 'ran ...' lines. Empty → 'never'."""
    if not value:
        return "never"
    parsed = parse_iso_utc(value)
    if parsed is None:
        return value
    dt = parsed.astimezone(get_tz())
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
    """The `due_label` Jinja filter — the single-source due vocabulary in core.dates,
    resolved against the app-tz 'today'."""
    return due_label(value, today_iso())


def _fmt_time(value):
    """A stored 'HH:MM' clock time → the user's preferred format: 24h '13:35' (default)
    or 12h '1:35pm' (dropping ':00', matching the calendar agenda). The stored value stays
    24h — this is display only, so entry keys (data-time, audio URLs) are untouched."""
    return fmt_clock(value, time_format())


app.jinja_env.filters["fdate"] = fmt_date
app.jinja_env.filters["fstamp"] = _fmt_stamp
app.jinja_env.filters["days_ago"] = _days_ago
app.jinja_env.filters["due_label"] = _due_label
app.jinja_env.filters["ftime"] = _fmt_time


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


# ── in-place card rendering ───────────────────────────────────────────────────
# A mutation route returns the task's freshly-rendered card so the page can swap that ONE
# node instead of reloading (the design contract's "in-place updates"). The server stays the
# single owner of card markup — the JS never hand-builds a card and drifts from the macro.
_CARD_MACROS = {"week": "week_item", "today": "today_item", "kcard": "kcard"}


def task_card_html(conn, task_id, surface="week"):
    """ONE task's card markup, rendered exactly as the page would render it. `surface`
    picks the shape: 'week' (Today's This-week row), 'today' (Today's hero row) or 'kcard'
    (the /tasks kanban card). Returns "" when the task is gone (deleted/purged) so callers
    can treat "no card" as "remove the node"."""
    from domain.tasks_core import task_dict, is_pinned
    row = conn.execute("SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
                       (task_id,)).fetchone()
    if not row:
        return ""
    today = today_iso()
    t = task_dict(conn, row)
    # tasks_page sets `pinned` while bucketing the board; a single re-rendered card has no
    # such pass, so derive it from the SAME predicate. Without this the kcard macro (whose
    # `stale` line is gated on `not t.pinned`) would show a "Nd · K× moved" line on a
    # swapped-in pinned card that a real page load never renders.
    t["pinned"] = is_pinned(t, today)
    macro = _CARD_MACROS.get(surface, "week_item")
    return render_template_string(
        "{%% import '_macros.html' as m %%}{{ m.%s(t, today) }}" % macro,
        t=t, today=today)


# ── DB accessor ───────────────────────────────────────────────────────────────
_DB_PATH = DB_PATH  # overridden by --db flag at startup (server.py sets _wc._DB_PATH)


def db():
    return connect(_DB_PATH)


@app.context_processor
def inject_health():
    """Make the health dots + last-ran ages + nav alert count available to every template.
    Just the Flask glue — opens a conn, delegates the actual computation to
    core.health.health_context, and falls back to a safe default on any error."""
    try:
        conn = db()
        ctx = health_context(conn)
        conn.close()
        return ctx
    except Exception:
        return {"health": {"capture": "off", "triage": "off", "backup": "off"},
                "health_age": {"capture": None, "triage": None, "backup": None},
                "ai_health": {"state": "off", "detail": "", "auth": False},
                "nav_alerts": 0}


@app.context_processor
def inject_nav():
    """Sidebar/bottom-nav badge counts, available to every template. Only the Tasks
    count remains — a Notes total is inventory, not a notification, so it's dropped
    (and its vault scan with it)."""
    counts = {"tasks": 0}
    try:
        conn = db()
        counts["tasks"] = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_id IS NULL "
            "AND archived_at IS NULL AND deleted_at IS NULL AND done = 0").fetchone()[0]
        conn.close()
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
