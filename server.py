#!/usr/bin/env python3
"""Life OS web server — thin entry point.

Mirrors youtube-assistant/executors/invoicing/server.py: argparse --port/--db,
point web_core._DB_PATH at the chosen DB, init/migrate the schema, register each
routes_*.bp blueprint, run.

Run: python3 server.py [--port 5070] [--db data/app.db]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from envload import load_env
load_env()   # so the web UI sees the same .env secrets the daemon does (e.g. Telegram token)

from core.web_core import app, DB_PATH
from core import db_init
from routes import main, tasks, notes, journal, goals, settings, docs, design

for _bpmod in (main, tasks, notes, journal, goals, settings, docs, design):
    app.register_blueprint(_bpmod.bp)


# ── nightly backup scheduler (in-process) ─────────────────────────────────────
# On the Mac a launchd plist runs scripts/backup_db.py; the NAS container has no
# launchd, so the web process schedules the backup itself. It runs in the APP
# container only (the capture container never imports server.py), so there is no
# double-run on the shared DB. Fires once per app-tz day at/after 03:00, guarded by
# the same settings.backup_last_ran heartbeat the sidebar dot reads.
def _backup_ran_today(last_iso, now) -> bool:
    """True if backup_last_ran (UTC ISO) falls on the app-tz 'today'."""
    if not last_iso:
        return False
    try:
        d = (datetime.strptime(str(last_iso)[:19], "%Y-%m-%dT%H:%M:%S")
             .replace(tzinfo=timezone.utc).astimezone(now.tzinfo).date())
    except ValueError:
        return False
    return d == now.date()


def _backup_loop(db_path: str, log) -> None:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
    from core.db import connect, now_sg, get_setting
    import backup_db
    while True:
        try:
            now = now_sg()
            if now.hour >= 3:                       # nightly window: 03:00 app-tz onward
                conn = connect(db_path)
                last = get_setting(conn, "backup_last_ran")
                conn.close()
                if not _backup_ran_today(last, now):
                    res = backup_db.run_backup(db_path)
                    log(f"backup written: {res.get('backup')}")
        except Exception as e:                       # never let the scheduler die
            log(f"backup scheduler error: {e}")
        time.sleep(1800)                             # re-check every 30 min


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5070)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    # db() in web_core reads module-level _DB_PATH — set it there so --db is honoured.
    from core import web_core as _wc
    _wc._DB_PATH = args.db
    db_init.init_db(args.db)

    # Warm note thumbnails in the background so the first Notes browse is instant instead
    # of fetching og:images on demand. Daemon thread — never blocks startup or shutdown.
    import threading
    from domain import thumbs
    threading.Thread(target=lambda: thumbs.warm_recent(), daemon=True).start()

    # Self-reload on settled code changes — same mechanism as the capture daemon, so a
    # file sync (Synology Drive on the NAS, an editor save on the Mac) picks up without a
    # manual restart. (Jinja templates already hot-reload under debug=True; this covers .py.)
    # We OWN port 5070, so exit-and-respawn (releases the socket) rather than execv, whose
    # inherited bound fd would fail to re-bind. Relies on the supervisor to respawn:
    # launchd KeepAlive here, the container restart policy on the NAS.
    import reloader
    _code_baseline = reloader.snapshot()
    threading.Thread(
        target=lambda: reloader.watch_loop(
            _code_baseline, lambda m: print(f"[web] {m}", flush=True), restart=reloader.exit_and_respawn
        ),
        daemon=True,
    ).start()

    # Nightly SQLite backup — self-scheduled so the NAS container needs no launchd.
    threading.Thread(
        target=lambda: _backup_loop(args.db, lambda m: print(f"[web] {m}", flush=True)),
        daemon=True,
    ).start()

    print(f"Life OS running at http://localhost:{args.port}")
    print(f"Database: {args.db}")
    # Bind 0.0.0.0 so the published port is reachable inside Docker (localhost-only
    # binding = ERR_EMPTY_RESPONSE from outside the container). debug off in the served
    # app — the Werkzeug debugger must never sit on a reachable port.
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
