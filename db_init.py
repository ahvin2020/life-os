#!/usr/bin/env python3
"""Initialise the Life OS SQLite database.

Idempotent — safe to run on every startup. Pattern lifted from
youtube-assistant/executors/invoicing/db_init.py: a TABLES list of
(name, CREATE-IF-NOT-EXISTS SQL), a meta-table schema_version, an idempotent
init_db(), and a migrate() hook gated on schema_version (a no-op at v1) that
warns loudly if the on-disk DB was written by a newer build.

Usage:
    python3 db_init.py [--db <path>]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from db import connect, DB_PATH, SCHEMA_VERSION, today_iso

TABLES = [
    ("meta", """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """),
    # Goals must exist before tasks (tasks.goal_id references it).
    # A goal is a TITLE; everything else is optional. `timeframe` supersedes the
    # legacy `period`/`kind` pair (kept for non-destructive migration; `kind` is
    # DEPRECATED — behaviour now derives from which measure/end_date/task fields
    # exist, see routes_goals.goal_progress). No hard CHECK on `timeframe` so legacy
    # NULLs and future values are tolerated — it's validated in Python.
    ("goals", """
        CREATE TABLE IF NOT EXISTS goals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            period       TEXT NOT NULL CHECK(period IN ('week','month')),
            period_start TEXT NOT NULL,
            kind         TEXT NOT NULL CHECK(kind IN ('rollup','number')),
            target_num   REAL,
            current_num  REAL DEFAULT 0,
            timeframe    TEXT DEFAULT 'week',
            end_date     TEXT,
            unit         TEXT,
            achieved_at  TEXT,
            archived_at  TEXT,
            deleted_at   TEXT,
            created      TEXT NOT NULL
        )
    """),
    # 'column' is a SQL keyword — named 'col' per the spec to avoid quoting.
    ("tasks", """
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            col          TEXT NOT NULL DEFAULT 'backlog'
                           CHECK(col IN ('backlog','week','done')),
            sort_order   INTEGER NOT NULL DEFAULT 0,
            priority     TEXT CHECK(priority IN ('high','med','low') OR priority IS NULL),
            category     TEXT CHECK(category IN ('content','business','personal') OR category IS NULL),
            due_date     TEXT,
            planned_on   TEXT,
            recur_rule   TEXT,
            goal_id      INTEGER REFERENCES goals(id) ON DELETE SET NULL,
            parent_id    INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            done         INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            archived_at  TEXT,
            deleted_at   TEXT,
            reschedule_count INTEGER NOT NULL DEFAULT 0,
            week_since   TEXT,
            created      TEXT NOT NULL,
            updated      TEXT NOT NULL
        )
    """),
    ("settings", """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_col ON tasks(col)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id)",
]


def migrate(conn) -> list:
    """Schema migrations gated on meta.schema_version. Idempotent, non-destructive.
    Runs before the CREATE-IF-NOT-EXISTS pass, so on a brand-new DB (no meta table)
    it is a no-op and the fresh DDL already has the latest columns."""
    applied = []
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return applied                       # brand-new DB — nothing to migrate
    try:
        disk = int(row[0]) if row else 0
    except (TypeError, ValueError):
        disk = 0

    # v2: soft-delete for tasks (undo, not confirmation).
    if 0 < disk < 2:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "deleted_at" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN deleted_at TEXT")
            applied.append("v2: tasks.deleted_at")

    # v3: a goal is just a TITLE — flexible timeframe + optional measure/milestone.
    # Add the new columns (idempotent) and backfill timeframe from the legacy period.
    if 0 < disk < 3:
        gcols = [r[1] for r in conn.execute("PRAGMA table_info(goals)").fetchall()]
        for col, ddl in (("timeframe", "TEXT"), ("end_date", "TEXT"),
                         ("unit", "TEXT"), ("achieved_at", "TEXT")):
            if col not in gcols:
                conn.execute(f"ALTER TABLE goals ADD COLUMN {col} {ddl}")
                applied.append(f"v3: goals.{col}")
        conn.execute("UPDATE goals SET timeframe=period WHERE timeframe IS NULL")
        applied.append("v3: goals.timeframe backfilled from period")

    # v4: postpone counter — feeds the backlog-intelligence "postponed N×" signal.
    # Incremented when a task's due_date moves later or a set planned_on is cleared.
    if 0 < disk < 4 and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "reschedule_count" not in cols:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN reschedule_count INTEGER NOT NULL DEFAULT 0")
            applied.append("v4: tasks.reschedule_count")

    # v5: week_since — the "This week" staleness clock. Stamped when a task enters
    # the week column, cleared when it leaves; feeds the board's "Nd" stale badge.
    # Backfill = today (the real entry date is unknowable; start counting now).
    if 0 < disk < 5 and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "week_since" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN week_since TEXT")
            conn.execute("UPDATE tasks SET week_since=? WHERE col='week' AND done=0",
                         (today_iso(),))
            applied.append("v5: tasks.week_since (+backfill)")

    # v6: soft-delete for goals (undo, not confirmation — parity with tasks/notes).
    # Task links survive: goal_id's ON DELETE SET NULL never fires on a soft delete.
    if 0 < disk < 6 and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'").fetchone():
        gcols = [r[1] for r in conn.execute("PRAGMA table_info(goals)").fetchall()]
        if "deleted_at" not in gcols:
            conn.execute("ALTER TABLE goals ADD COLUMN deleted_at TEXT")
            applied.append("v6: goals.deleted_at")
    return applied


def init_db(db_path: str = DB_PATH) -> dict:
    """Create and/or migrate the schema at db_path. Single source of truth for the
    Life OS schema; called by the CLI and by the web server at startup. Idempotent."""
    conn = connect(db_path)
    created = []
    with conn:
        migrated = migrate(conn)
        for name, ddl in TABLES:
            conn.execute(ddl)
            created.append(name)
        for idx_sql in INDEXES:
            conn.execute(idx_sql)
        # Warn if the on-disk DB was written by a newer build than this code.
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is not None:
            try:
                disk_ver = int(row[0])
            except (TypeError, ValueError):
                disk_ver = None
            if disk_ver is not None and disk_ver > SCHEMA_VERSION:
                print(
                    f"WARNING: database schema_version={disk_ver} is NEWER than this "
                    f"code's SCHEMA_VERSION={SCHEMA_VERSION} — the DB was written by a "
                    f"newer build; this code may not understand its schema.",
                    file=sys.stderr,
                )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    conn.close()
    return {
        "status": "ok",
        "db_path": db_path,
        "schema_version": SCHEMA_VERSION,
        "migrated": migrated,
        "created_tables": created,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()
    result = init_db(args.db)
    print(result)


if __name__ == "__main__":
    main()
