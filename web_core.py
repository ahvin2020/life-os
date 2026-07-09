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
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, session, abort,
)

from db import connect, now_iso, today_iso, now_sg, DB_PATH, TZ

app = Flask(__name__, template_folder="web/templates", static_folder="web/static")

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


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
    """Human due label relative to today: today / yesterday / overdue / a date."""
    n = _days_ago(value)
    if n is None:
        return _fmt_date(value)
    if n == 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 0:
        return _fmt_date(value)
    return _fmt_date(value)


app.jinja_env.filters["fdate"] = _fmt_date
app.jinja_env.filters["days_ago"] = _days_ago
app.jinja_env.filters["due_label"] = _due_label


# ── request helpers ───────────────────────────────────────────────────────────
def _is_ajax() -> bool:
    """True when the request came from our fetch() helpers (X-Requested-With)."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def respond(ok, msg, to=None, fallback=None):
    """AJAX-or-redirect reply shared by POST action routes.

    On an AJAX request returns JSON; otherwise flashes and redirects to `to` (or
    `fallback`). Extra JSON payload can be merged by callers via jsonify directly."""
    if _is_ajax():
        return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 400)
    flash(msg, "success" if ok else "error")
    return redirect(to or fallback or "/")


# ── DB accessor ───────────────────────────────────────────────────────────────
_DB_PATH = DB_PATH  # overridden by --db flag at startup (server.py sets _wc._DB_PATH)


def db():
    return connect(_DB_PATH)


@app.context_processor
def inject_nav():
    """Sidebar/bottom-nav badge counts, available to every template."""
    import vault_store
    counts = {"tasks": 0, "notes": 0}
    try:
        conn = db()
        counts["tasks"] = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_id IS NULL "
            "AND archived_at IS NULL AND done = 0").fetchone()[0]
        conn.close()
    except Exception:
        pass
    try:
        counts["notes"] = len(vault_store.list_notes())
    except Exception:
        pass
    return {"nav_counts": counts, "today": today_iso()}
