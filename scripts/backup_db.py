#!/usr/bin/env python3
"""Nightly SQLite backup for Life OS.

Uses sqlite3's online .backup (a consistent snapshot even while the DB is live in WAL
mode) to write a timestamped copy into data/backups/, prunes to the most recent KEEP
files, mirrors that copy into a Synology-synced data-backups/ folder at the repo root
(offsite copy — gitignored), and stamps settings.backup_last_ran so the sidebar
'backup' health dot goes green.

Run under launchd daily at 03:00 (deploy/com.kelvin.lifeos.backup.plist) or manually:
    python3 scripts/backup_db.py

Dirs are overridable for tests via LIFEOS_BACKUP_DIR / LIFEOS_SYNCED_BACKUP_DIR.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# Make the repo root importable when run directly from scripts/ or by the test-suite.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from db import DB_PATH, now_iso, connect  # noqa: E402

KEEP = 7  # retain the most recent N backups in each folder
_PREFIX, _SUFFIX = "app-", ".db"


def backup_dir() -> str:
    d = os.environ.get("LIFEOS_BACKUP_DIR") or os.path.join(_REPO_ROOT, "data", "backups")
    os.makedirs(d, exist_ok=True)
    return d


def synced_dir() -> str:
    """Synology-synced mirror at the repo root (gitignored) — the offsite copy."""
    d = os.environ.get("LIFEOS_SYNCED_BACKUP_DIR") or os.path.join(_REPO_ROOT, "data-backups")
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
    """Full nightly job: online backup → prune local → mirror offsite → prune offsite →
    heartbeat. Returns a summary dict."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{_PREFIX}{ts}{_SUFFIX}"
    local = os.path.join(backup_dir(), name)
    _online_backup(db_path, local)
    pruned_local = prune(backup_dir())
    synced = os.path.join(synced_dir(), name)
    shutil.copy2(local, synced)
    pruned_synced = prune(synced_dir())
    stamp_heartbeat(db_path)
    return {"backup": local, "synced": synced,
            "pruned_local": len(pruned_local), "pruned_synced": len(pruned_synced)}


def main() -> None:
    print(run_backup())


if __name__ == "__main__":
    main()
