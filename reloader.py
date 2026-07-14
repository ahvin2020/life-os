"""Self-reload on settled source changes — the sync IS the deploy channel.

Both long-running processes (capture_daemon, the web server) receive new code by
file sync (Synology Drive on the NAS, editor saves on the Mac) with nothing to
restart them. A stale process keeps old modules in sys.modules and dies on
ImportError the moment a new code path imports a name a cached old module lacks
(this stranded two bot messages on 2026-07-11: extract_json / days_ago_iso).

Each process watches its own .py files and os.execv's into fresh code once a change
has SETTLED (no .py touched for SETTLE_S) — the guard against re-execing mid-sync
into a half-written, internally inconsistent code set. Same PID; supervisor-
independent, so it behaves identically under launchd (Mac) and Docker (NAS).

It also self-heals the one thing a .py watch misses: connecting an integration while
we run. That drops a NEW token/secret file and often pip-installs deps — neither
touches our .py, so the stale process stays blind (a Google-calendar lookup answered
"no event" for an event that WAS in gcal, 2026-07-14). So a trigger file (below)
that newly APPEARS since start also forces a reload, re-execing into a process that
re-imports the fresh deps and re-reads state.
"""
from __future__ import annotations

import os
import sys
import time

SETTLE_S = 15                                   # a change must be this quiet before we act
_ROOT = os.path.dirname(os.path.abspath(__file__))
_SKIP_DIRS = {"__pycache__", "node_modules"}    # dot-dirs (.venv/.git/.trash) pruned separately

# Non-.py files whose first APPEARANCE means an integration was just connected. We watch
# APPEARANCE (absent→present since start), never every write: connecting via the Settings
# OAuth callback writes the token, and _creds() REWRITES it on each ~hourly refresh — mtime-
# watching it would reexec the daemon every hour. An allowlist, because data/ is otherwise
# pruned on purpose (app.db churns constantly and must never trigger a reload).
_TRIGGER_FILES = (
    os.path.join(_ROOT, "data", "google_token.json"),
    os.path.join(_ROOT, "data", "google_client_secret.json"),
)


def code_mtime() -> float:
    """Newest mtime across the app's own .py files (venv/git/data/vault pruned)."""
    latest = 0.0
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                try:
                    m = os.path.getmtime(os.path.join(dirpath, f))
                except OSError:
                    continue
                if m > latest:
                    latest = m
    return latest


def _present_triggers() -> frozenset:
    """The trigger files that exist right now."""
    return frozenset(p for p in _TRIGGER_FILES if os.path.exists(p))


def snapshot() -> tuple:
    """Baseline for should_reload: (newest .py mtime, trigger files present now). A later
    reload fires on a settled .py change OR a trigger file that has appeared since this."""
    return (code_mtime(), _present_triggers())


def should_reload(baseline) -> bool:
    """True once a change past `baseline` has settled (no touch for SETTLE_S). `baseline`
    is a snapshot() tuple; a bare float is still accepted (watches .py only) for back-compat.

    Fires on either a .py newer than baseline, or a trigger file that has newly APPEARED
    since baseline — the signal that an integration (e.g. Google) was just connected while
    we were running. Both are gated on SETTLE_S so we never reexec mid-write."""
    if isinstance(baseline, tuple):
        py_baseline, present0 = baseline
    else:                                        # legacy float: watch .py only, no trigger arm
        py_baseline, present0 = baseline, _present_triggers()
    now = time.time()
    latest = code_mtime()
    if latest > py_baseline and (now - latest) >= SETTLE_S:
        return True
    for p in _present_triggers() - present0:     # trigger files that appeared since start
        try:
            if (now - os.path.getmtime(p)) >= SETTLE_S:
                return True
        except OSError:
            continue
    return False


def reexec(on_reload=None) -> None:
    """Replace this process with a fresh interpreter running the same argv. Same PID,
    supervisor-independent. Safe ONLY for processes that hold no listening socket —
    an inherited bound fd would survive execv and make the fresh image fail to re-bind
    (EADDRINUSE). The capture daemon (outbound long-poll, no socket) uses this."""
    if on_reload:
        try:
            on_reload()
        except Exception:
            pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


def exit_and_respawn(on_reload=None) -> None:
    """Clean-exit so the supervisor (launchd KeepAlive / Docker restart policy) starts
    us fresh. For processes that OWN a listening socket (the web server): a full exit
    releases the port, which execv would not. Non-zero code so on-failure policies also
    respawn; launchd KeepAlive=true respawns regardless."""
    if on_reload:
        try:
            on_reload()
        except Exception:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(3)


def watch_loop(baseline: float, log, on_reload=None, interval: float = 2.0, restart=reexec) -> None:
    """Block forever, restarting on a settled change. For a background daemon thread.
    `restart` is reexec (default, socketless) or exit_and_respawn (socket-owning)."""
    while True:
        time.sleep(interval)
        if should_reload(baseline):
            log("code change detected — reloading")
            restart(on_reload)
