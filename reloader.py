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
"""
from __future__ import annotations

import os
import sys
import time

SETTLE_S = 15                                   # a change must be this quiet before we act
_ROOT = os.path.dirname(os.path.abspath(__file__))
_SKIP_DIRS = {"__pycache__", "node_modules"}    # dot-dirs (.venv/.git/.trash) pruned separately


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


def should_reload(baseline: float) -> bool:
    """True once a .py newer than `baseline` has settled (no edit for SETTLE_S)."""
    latest = code_mtime()
    return latest > baseline and (time.time() - latest) >= SETTLE_S


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
