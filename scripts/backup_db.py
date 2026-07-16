#!/usr/bin/env python3
"""Nightly SQLite backup for Life OS.

Uses sqlite3's online .backup (a consistent snapshot even while the DB is live in WAL
mode) to write a timestamped copy into <db dir>/backups/, prunes to the most recent KEEP
files, and stamps settings.backup_last_ran so the sidebar 'backup' health dot goes green.

That is the WHOLE job. Making a consistent snapshot of a live WAL database is the one
thing only this app can do; getting the bytes off the box is Synology's (Cloud Sync /
Hyper Backup, pointed at the backups dir). There used to be an "offsite location" that
shutil.copy2'd the snapshot somewhere else, and it was a worse duplicate of Cloud Sync:
it silently no-op'd for days when unset while the health dot stayed green, and any path
you gave it was created blind — inside the container that meant the ephemeral /app,
wiped by every deploy, with the copy reporting success. Cut 2026-07-16. Don't re-add:
the app cannot verify a destination is durable, and a backup that lies is worse than
none.

Run under launchd daily at 03:00 (deploy/com.kelvin.lifeos.backup.plist) or manually:
    python3 scripts/backup_db.py

The backup dir is overridable for tests via LIFEOS_BACKUP_DIR.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone

# Make the repo root importable when run directly from scripts/ or by the test-suite.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.db import DB_PATH, now_iso, connect, get_setting  # noqa: E402

KEEP = 7  # default retention (most recent N) — overridable via settings.backup_keep
_PREFIX, _SUFFIX = "app-", ".db"


def _setting(db_path, key):
    """Read one settings value (or None). Best-effort; never raises."""
    try:
        conn = connect(db_path)
        v = get_setting(conn, key)
        conn.close()
        return v or None
    except Exception:
        return None


def backup_dir(db_path: str = DB_PATH) -> str:
    """Where the local snapshots live. Defaults to a 'backups/' folder NEXT TO the DB —
    on the Mac that's repo/data/backups (unchanged); in the NAS container the DB is on
    the mounted /data volume, so backups land in /data/backups and survive a redeploy
    (the old repo-root default resolved to the ephemeral /app inside the image). Env
    LIFEOS_BACKUP_DIR still wins (tests)."""
    d = os.environ.get("LIFEOS_BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(d, exist_ok=True)
    return d


def _online_backup(src_path: str, dst_path: str) -> None:
    """Consistent copy via sqlite's online backup API (safe under WAL / concurrent use)."""
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def prune(directory: str, keep: int = KEEP) -> list:
    """Delete all but the most recent `keep` app-*.db files. Backup names are UTC
    timestamps, so a reverse lexicographic sort is newest-first. Returns removed paths."""
    files = sorted(
        (f for f in os.listdir(directory) if f.startswith(_PREFIX) and f.endswith(_SUFFIX)),
        reverse=True)
    removed = []
    for name in files[keep:]:
        path = os.path.join(directory, name)
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def stamp_heartbeat(db_path: str = DB_PATH) -> None:
    """Record settings.backup_last_ran (UTC ISO) → drives the sidebar 'backup' dot."""
    conn = connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('backup_last_ran', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_iso(),))
    conn.close()


def run_backup(db_path: str = DB_PATH) -> dict:
    """Full nightly job: online backup → prune → heartbeat. The snapshot lands next to the
    DB, which on the NAS is the persisted /data volume. Retention (backup_keep) is
    user-overridable. Replication off the box is Cloud Sync's job, not this script's."""
    keepv = _setting(db_path, "backup_keep")
    keep = int(keepv) if (keepv and str(keepv).isdigit()) else KEEP
    local_base = backup_dir(db_path)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{_PREFIX}{ts}{_SUFFIX}"
    local = os.path.join(local_base, name)
    _online_backup(db_path, local)
    pruned_local = prune(local_base, keep)

    stamp_heartbeat(db_path)
    return {"backup": local, "pruned_local": len(pruned_local)}


def main() -> None:
    print(run_backup())


if __name__ == "__main__":
    main()
