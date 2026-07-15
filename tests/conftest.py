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
# Its own tree, outside the test vault AND pytest's tmp_path — see _isolate_profile.
_PROFILE_TMP = tempfile.mkdtemp(prefix="lifeos-profile-")
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


@pytest.fixture(autouse=True)
def _isolate_profile(monkeypatch):
    """vault_store.PROFILE_PATH deliberately ignores LIFEOS_VAULT_DIR and points at the REAL
    repo vault, so any test touching set_identity/append_learned_rule/upsert_contact would
    rewrite Sam's actual profile.md. Redirect it for every test — per-test monkeypatch
    discipline isn't a guard, it's a thing to forget once. Tests that assert on profile
    contents still patch it to their own path; this is the floor, not a fixture to use.

    Deliberately NOT under tmp_path or the test vault: tests point document_roots at those
    and count what the scan finds, and a stray profile.md would be counted as a document."""
    from domain import vault_store
    prof = os.path.join(_PROFILE_TMP, "profile.md")
    with open(prof, "w", encoding="utf-8") as f:          # fresh starter per test
        f.write("# profile.md — triage context\n## Who I am\n- TODO\n")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", prof)


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    """Every real-data log path the suite can reach, pointed at tmp.

    - `router._RAW_LOG` — the router appends every inbound message to data/capture_raw.log
      BEFORE calling claude (the safety rail).
    - `capture_daemon._LOG_PATH` — the daemon log. NOT hypothetical: a passing
      test_daemon_fallback_schedules_sweep wrote "router fell back to #unsorted: some
      ambiguous rambling" into the real 320KB operational log, which is the log Sam reads
      when the daemon misbehaves. Test fiction in a debugging log is worse than noise.
    - `capture_daemon._RAW_LOG_PATH` — the daemon's own crash-rail copy. It deliberately
      does NOT import router (it must survive a router import failure), so it needs its
      own patch rather than sharing router._RAW_LOG.
    - `triage/run_triage._PROFILE_PATH` — a second path to the REAL vault/profile.md that
      _isolate_profile doesn't cover; ensure_profile() CREATES it, so on a fresh clone or
      CI the suite would write into the real vault.
    """
    from ai import router
    monkeypatch.setattr(router, "_RAW_LOG", str(tmp_path / "capture_raw.log"))

    import capture_daemon
    monkeypatch.setattr(capture_daemon, "_LOG_PATH", str(tmp_path / "capture_daemon.log"))
    monkeypatch.setattr(capture_daemon, "_RAW_LOG_PATH", str(tmp_path / "daemon_raw.log"))

    from triage import run_triage
    monkeypatch.setattr(run_triage, "_PROFILE_PATH", str(tmp_path / "triage_profile.md"))


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

    # A fresh DB + vault is an unconfigured first run, so the one-time onboarding nudge
    # ("what should I call you?") would append itself to the FIRST router reply of every
    # test and break exact-reply assertions. Mark it offered; the tests that exercise the
    # nudge itself (test_profile_loop) clear the flag first.
    from core.db import connect, set_setting
    with connect(os.environ["LIFEOS_DB_PATH"]) as _c:
        set_setting(_c, "onboarding_offered", "1")
    _c.close()

    # register blueprints once
    from routes import main, tasks, notes, journal, goals, settings, docs, design
    for mod in (main, tasks, notes, journal, goals, settings, docs, design):
        if mod.bp.name not in web_core.app.blueprints:
            web_core.app.register_blueprint(mod.bp)

    return web_core.make_test_client()
