"""Tests for scripts/backup_db.py — the nightly SQLite backup job.

Covers the two bits with real logic: prune (keep the most recent N by timestamped
name) and the heartbeat that turns the sidebar 'backup' dot green. conftest.py points
LIFEOS_DB_PATH at a throwaway DB; backup dirs are redirected to tmp via env vars so
the real data/backups is never touched.
"""

import os
import sys
from datetime import datetime, timezone

# make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import backup_db  # noqa: E402
from db import connect  # noqa: E402
from web_core import health_status  # noqa: E402


def _touch(directory, name):
    with open(os.path.join(directory, name), "w") as f:
        f.write("x")


# ── prune ─────────────────────────────────────────────────────────────────────
def test_prune_keeps_most_recent_seven(tmp_path):
    d = str(tmp_path)
    # 10 timestamped backups (lexicographic order == chronological)
    names = [f"app-2026010{i}-000000.db" for i in range(10)]  # app-20260100.. .app-20260109
    for n in names:
        _touch(d, n)
    removed = backup_db.prune(d, keep=7)
    remaining = sorted(f for f in os.listdir(d) if f.startswith("app-"))
    assert len(remaining) == 7
    assert len(removed) == 3
    # the 7 newest (highest names) survive; the 3 oldest are gone
    assert remaining == sorted(names)[3:]


def test_prune_ignores_non_backup_files(tmp_path):
    d = str(tmp_path)
    _touch(d, "app-20260101-000000.db")
    _touch(d, "notes.md")
    _touch(d, "README")
    backup_db.prune(d, keep=7)
    assert os.path.exists(os.path.join(d, "notes.md"))     # untouched
    assert os.path.exists(os.path.join(d, "README"))


def test_prune_noop_when_under_limit(tmp_path):
    d = str(tmp_path)
    for i in range(3):
        _touch(d, f"app-2026010{i}-000000.db")
    assert backup_db.prune(d, keep=7) == []
    assert len([f for f in os.listdir(d) if f.startswith("app-")]) == 3


# ── heartbeat ─────────────────────────────────────────────────────────────────
def test_stamp_heartbeat_sets_backup_last_ran(client):
    db_path = os.environ["LIFEOS_DB_PATH"]
    backup_db.stamp_heartbeat(db_path)
    conn = connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key='backup_last_ran'").fetchone()
    assert row is not None and row["value"]
    # the health dot reads this key and should report 'ok' right after a run
    status = health_status(conn)
    conn.close()
    assert status["backup"] == "ok"


# ── full job (backup + prune + mirror + heartbeat) ────────────────────────────
def test_run_backup_writes_prunes_mirrors_and_heartbeats(client, tmp_path, monkeypatch):
    db_path = os.environ["LIFEOS_DB_PATH"]
    local = tmp_path / "backups"
    synced = tmp_path / "data-backups"
    local.mkdir()
    synced.mkdir()
    monkeypatch.setenv("LIFEOS_BACKUP_DIR", str(local))
    monkeypatch.setenv("LIFEOS_SYNCED_BACKUP_DIR", str(synced))
    # pre-seed 8 stale backups so prune has to fire (result keeps 7 incl. the new one)
    for i in range(8):
        _touch(str(local), f"app-2025010{i}-000000.db")

    result = backup_db.run_backup(db_path)

    assert os.path.exists(result["backup"])                       # a fresh snapshot exists
    assert os.path.exists(result["synced"])                       # mirrored offsite
    # the online-backup copy is a valid, openable SQLite DB with our schema
    b = connect(result["backup"])
    assert b.execute("SELECT COUNT(*) FROM settings").fetchone()[0] >= 0
    b.close()
    assert len([f for f in os.listdir(local) if f.startswith("app-")]) == 7   # pruned to 7
    conn = connect(db_path)
    assert health_status(conn)["backup"] == "ok"                  # dot went green
    conn.close()
