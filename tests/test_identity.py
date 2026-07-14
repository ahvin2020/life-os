"""Auto-derived identity: gather signals → propose a block → confirm → write to profile."""

import json
import os

from core.db import connect, get_setting
from domain import identity, vault_store, docs
from ai import router, google_client


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def test_propose_builds_from_signals(client, monkeypatch):
    monkeypatch.setattr(google_client, "is_configured", lambda: True)
    monkeypatch.setattr(google_client, "gmail_address", lambda: "junkai@example.com")
    monkeypatch.setattr(identity, "person_doc_names",
                        lambda c, limit=30: ["lee jun kai passport.pdf", "lee xin yi passport.pdf"])
    captured = {}
    def fake(p): captured["p"] = p; return "Name: Lee Jun Kai (me)\nChild: Lee Xin Yi?"
    conn = _db(); block = identity.propose(conn, claude_fn=fake); conn.close()
    assert "Lee Jun Kai" in block
    assert "junkai@example.com" in captured["p"] and "lee xin yi" in captured["p"].lower()


def test_propose_empty_without_signals(client, monkeypatch):
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    monkeypatch.setattr(identity, "person_doc_names", lambda c, limit=30: [])
    conn = _db()
    assert identity.propose(conn, claude_fn=lambda p: "x") == ""
    conn.close()


def test_set_identity_writes_then_replaces(client, monkeypatch, tmp_path):
    prof = tmp_path / "profile.md"
    prof.write_text("# Learned rules\n- do X\n")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    assert vault_store.set_identity("Name: Kelvin\nWife: Mei Fang?")
    txt = prof.read_text()
    assert "# Identity" in txt and "Name: Kelvin" in txt and "# Learned rules" in txt  # preserved
    vault_store.set_identity("Name: Kelvin Tan")            # replace, not duplicate
    txt = prof.read_text()
    assert txt.count("# Identity") == 1 and "Name: Kelvin Tan" in txt


def test_router_derive_identity_pends_confirmation(client, monkeypatch):
    monkeypatch.setattr(identity, "propose", lambda c, fn: "Name: Lee Jun Kai (me)")
    conn = _db()
    reply, _ = router.apply_action(conn, {"action": "derive_identity"}, {"today": "2026-07-14"})
    pend = json.loads(get_setting(conn, "pending_action") or "{}")
    conn.close()
    assert "Lee Jun Kai" in reply and pend["kind"] == "profile_identity"


def test_confirm_writes_identity(client, monkeypatch, tmp_path):
    prof = tmp_path / "profile.md"; prof.write_text("")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    conn = _db()
    out = router.execute_pending(conn, {"kind": "profile_identity", "payload": {"block": "Name: Kelvin"}})
    conn.close()
    assert "Saved" in out and "Name: Kelvin" in prof.read_text()


def test_identity_names_splits_own_from_family(client, monkeypatch, tmp_path):
    prof = tmp_path / "profile.md"
    prof.write_text("# Identity\nName: Lee Jun Kai (me)\nWife: Ong Mei Fang\n"
                    "Daughter: Lee Xin Yi\n\n# Learned rules\n- x\n")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    own, family = vault_store.identity_names()
    assert {"jun", "kai"} <= own                 # his given names
    assert {"ong", "xin", "yi"} <= family        # relatives' names
    assert "learned" not in own and "rules" not in own   # stops at the next section


def test_telegram_photo_attaches_to_task_and_journal(client):
    """A photo routed to create_task / append_journal carries its media pointer (web+bot
    parity — the pointer only reached notes before)."""
    conn = _db()
    ctx = {"today": "2026-07-14", "media_pointer": "vault/.media/x.jpg"}
    router.apply_action(conn, {"action": "create_task", "title": "receipt"}, ctx)
    row = conn.execute("SELECT media FROM tasks WHERE title='receipt'").fetchone()
    assert row["media"] == "vault/.media/x.jpg"
    router.apply_action(conn, {"action": "append_journal", "text": "beach"}, ctx)
    conn.close()
    page = vault_store.read_journal("2026-07-14")
    assert page["entries"][-1]["media_items"]
