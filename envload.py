"""Tiny .env loader — no python-dotenv dependency (runtime stays Flask-only).

Reads KEY=VALUE lines from a .env file at the repo root and injects any that are
not already set in the process environment. Used by capture_daemon.py and
triage/run_triage.py so secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID)
live in one gitignored file. Lines that are blank or start with '#' are skipped;
surrounding quotes on the value are stripped.
"""

from __future__ import annotations

import os

_ROOT = os.path.dirname(os.path.abspath(__file__))


def load_env(path: str | None = None) -> dict:
    """Load KEY=VALUE pairs from `path` (default: repo-root .env) into os.environ
    without clobbering values already present. Returns the parsed dict. Missing
    file → empty dict (harmless)."""
    path = path or os.path.join(_ROOT, ".env")
    parsed: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                parsed[key] = val
                os.environ.setdefault(key, val)
    except FileNotFoundError:
        pass
    return parsed
