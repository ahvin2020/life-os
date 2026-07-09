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
import sys

from db import connect, DB_PATH, SCHEMA_VERSION

TABLES = [
    ("meta", """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """),
    # Goals must exist before tasks (tasks.goal_id references it).
    ("goals", """
        CREATE TABLE IF NOT EXISTS goals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            period       TEXT NOT NULL CHECK(period IN ('week','month')),
            period_start TEXT NOT NULL,
            kind         TEXT NOT NULL CHECK(kind IN ('rollup','number')),
            target_num   REAL,
            current_num  REAL DEFAULT 0,
            archived_at  TEXT,
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
    """Schema migrations gated on meta.schema_version. Nothing to do at v1 — this
    is the hook future versions extend (mirrors the invoicing migrate() pattern)."""
    return []


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
