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
# Never spawn a background claude enrichment thread during tests; the enrichment
# functions are exercised directly (with mocks) in test_enrich.py.
os.environ["LIFEOS_ENRICH_LINKS"] = "0"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_google_token(tmp_path, monkeypatch):
    """Point the Google OAuth token at a throwaway path so tests never observe a REAL
    connected token on the dev machine (which would flip is_configured() to True and
    break the 'not connected' assertions). Tests that need a token write to this path."""
    from ai import google_client
    monkeypatch.setattr(google_client, "_TOKEN", str(tmp_path / "google_token.json"))


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

    from core import db_init
    from core import web_core
    web_core._DB_PATH = os.environ["LIFEOS_DB_PATH"]
    db_init.init_db(os.environ["LIFEOS_DB_PATH"])

    # register blueprints once
    from routes import main, tasks, notes, journal, goals, settings, docs, design
    for mod in (main, tasks, notes, journal, goals, settings, docs, design):
        if mod.bp.name not in web_core.app.blueprints:
            web_core.app.register_blueprint(mod.bp)

    return web_core.make_test_client()
