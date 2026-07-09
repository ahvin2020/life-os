"""Pytest fixtures: point the whole stack at a throwaway DB + vault so tests never
touch the real data. Env vars must be set BEFORE importing the app modules (db.py
and vault_store.py read them at import time)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="lifeos-test-")
os.environ["LIFEOS_DB_PATH"] = os.path.join(_TMP, "app.db")
os.environ["LIFEOS_VAULT_DIR"] = os.path.join(_TMP, "vault")

import pytest  # noqa: E402


@pytest.fixture()
def client():
    """Fresh DB + vault per test, with a CSRF-aware Flask test client."""
    import shutil
    # wipe any prior state
    for p in (os.environ["LIFEOS_DB_PATH"], os.environ["LIFEOS_DB_PATH"] + "-wal",
              os.environ["LIFEOS_DB_PATH"] + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    if os.path.isdir(os.environ["LIFEOS_VAULT_DIR"]):
        shutil.rmtree(os.environ["LIFEOS_VAULT_DIR"])

    import db_init
    import web_core
    web_core._DB_PATH = os.environ["LIFEOS_DB_PATH"]
    db_init.init_db(os.environ["LIFEOS_DB_PATH"])

    # register blueprints once
    import routes_main, routes_tasks, routes_notes, routes_journal, routes_goals
    for mod in (routes_main, routes_tasks, routes_notes, routes_journal, routes_goals):
        if mod.bp.name not in web_core.app.blueprints:
            web_core.app.register_blueprint(mod.bp)

    return web_core.make_test_client()
