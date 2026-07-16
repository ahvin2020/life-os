"""Profile-suggestion loop: correction-signal capture, append_learned_rule (writes to a
TMP profile — NEVER the real one), the weekly suggester, and the confirm→append path.

IMPORTANT: vault_store.PROFILE_PATH points at the REAL repo vault (it ignores
LIFEOS_VAULT_DIR), so every test here monkeypatches it to a tmp file.
"""

import os

import capture_daemon as cd
from ai import router, proactive
from domain import vault_store
from core.db import connect, record_correction, recent_corrections


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


class FakeTG:
    def __init__(self):
        self.sent = []
    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text); return {"ok": True}


def test_record_and_read_corrections(client):
    conn = _db()
    record_correction(conn, "refile", "note->task")
    record_correction(conn, "clarify", "which task?")
    got = recent_corrections(conn, 7)
    conn.close()
    assert len(got) == 2 and got[0]["kind"] == "refile"


def test_append_learned_rule_creates_section_and_dedupes(client, tmp_path, monkeypatch):
    prof = tmp_path / "profile.md"
    prof.write_text("# Profile\n\nSam is a YouTuber.\n")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    assert vault_store.append_learned_rule("gym captures → personal, high priority")
    body = prof.read_text()
    assert "# Learned rules" in body and "gym captures" in body
    # dedupe: same rule again is a no-op
    assert vault_store.append_learned_rule("gym captures → personal, high priority") is False


def test_append_learned_rule_cap(client, tmp_path, monkeypatch):
    prof = tmp_path / "p.md"
    prof.write_text("# Learned rules\n" + "".join(f"- rule {i}\n" for i in range(15)))
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    assert vault_store.append_learned_rule("one more") is False    # full at 15


def test_upsert_contact_creates_section_and_merges(client, tmp_path, monkeypatch):
    prof = tmp_path / "profile.md"
    prof.write_text("# Identity\nWife: Jane Tan\n")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    # first email → new bullet under a new # Contacts section
    assert vault_store.upsert_contact("Wife Jane Tan", ["jane.tan@example.com"])
    body = prof.read_text()
    assert "# Contacts" in body and "jane.tan@example.com" in body
    # second email, same label → MERGES into one line (not a duplicate person)
    assert vault_store.upsert_contact("Wife Jane Tan", ["jane.t@example.net"])
    body = prof.read_text()
    assert body.count("jane.tan@example.com") == 1
    assert "jane.t@example.net" in body
    assert body.count("- Wife Jane Tan") == 1        # merged, single bullet
    # a garbage save (no valid email) is rejected
    assert vault_store.upsert_contact("Nobody", ["not-an-email"]) is False
    # the identity section is preserved
    assert "# Identity" in prof.read_text()


def test_upsert_contact_replace_can_remove_an_address(client, tmp_path, monkeypatch):
    """Merging is right for "her other email is X" and WRONG for "remove X" — and for a while
    merging was the only mode. Asked to drop two of five addresses, the bot offered to re-save
    the person with the remaining three, merged them straight back into the same five, and said
    "✅ Saved". The write succeeded; it just couldn't do what was asked. replace=True SETS."""
    prof = tmp_path / "profile.md"
    prof.write_text("# Identity\nMum: Jane Tan\n")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    keep, drop = ["mum1@example.com", "mum2@example.net"], ["old@example.org", "gone@example.com"]
    assert vault_store.upsert_contact("Mum", keep + drop)
    assert all(e in prof.read_text() for e in keep + drop)

    # the ask: "remove those two" → re-save with ONLY the keepers
    assert vault_store.upsert_contact("Mum", keep, replace=True)
    body = prof.read_text()
    assert all(e in body for e in keep)
    for e in drop:
        assert e not in body, f"{e} survived a replace — this is the original bug"
    assert body.count("- Mum") == 1                  # still one bullet, not a duplicate person
    assert "# Identity" in body                      # other sections untouched

    # a plain (merge) save must NOT resurrect them
    assert vault_store.upsert_contact("Mum", ["mum3@example.com"])
    assert all(e not in prof.read_text() for e in drop)

    # replace with an empty list removes the person outright
    assert vault_store.upsert_contact("Mum", [], replace=True)
    assert "- Mum" not in prof.read_text()
    # ...and forgetting someone who was never listed reports failure, not a phantom success
    assert vault_store.upsert_contact("Ghost", [], replace=True) is False


def test_brief_says_free_day_vs_cant_see_calendar(client, monkeypatch):
    """A clear day and a dead connector are different facts. Both rendered "(not connected)",
    so the brief could not tell "nothing scheduled" from "I couldn't look" — the same conflation
    that had the bot reporting no cruise booking about mail it never searched."""
    from ai import proactive, google_client
    from core.db import connect
    import os
    conn = connect(os.environ["LIFEOS_DB_PATH"])

    # connected + genuinely empty → a FACT the brief may rely on
    monkeypatch.setattr(google_client, "is_configured", lambda: True)
    monkeypatch.setattr(google_client, "calendar_today", lambda d, service=None: [])
    monkeypatch.setattr(google_client, "gmail_highlights", lambda *a, **k: [])
    text = proactive.build_brief_context(conn)["text"]
    assert "nothing scheduled today" in text and "NOT CHECKED" not in text

    # not connected → must NOT read as a clear day
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    text = proactive.build_brief_context(conn)["text"]
    assert "NOT CHECKED" in text and "NOT evidence of absence" in text
    assert "nothing scheduled today" not in text
    conn.close()


def test_router_remember_contact_writes_profile(client, tmp_path, monkeypatch):
    import json
    prof = tmp_path / "profile.md"
    prof.write_text("# Identity\nWife: Jane Tan\n")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    conn = _db()
    res = router.route(conn, "remember my wife's other email is jane.t@example.net",
                       claude_fn=lambda p: json.dumps(
                           {"action": "remember_contact", "label": "Wife Jane Tan",
                            "emails": ["jane.t@example.net"]}))
    conn.close()
    assert res["applied"] == ["remember_contact"]
    assert "jane.t@example.net" in prof.read_text()


def test_first_run_onboarding_offers_once(client, tmp_path, monkeypatch):
    import json
    prof = tmp_path / "profile.md"
    prof.write_text("# profile.md — triage context\n## Who I am\n- TODO\n")   # starter, no identity
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    conn = _db()
    # conftest marks the nudge offered so it can't leak into other tests' replies; this is
    # the test that owns the nudge, so put the DB back to a genuine first run.
    conn.execute("DELETE FROM settings WHERE key='onboarding_offered'"); conn.commit()
    fn = lambda p: json.dumps({"action": "answer", "text": "You have 2 tasks."})
    r1 = router.route(conn, "how many tasks?", claude_fn=fn)
    assert "call me" in r1["reply"]                            # nudged for a name on first message
    r2 = router.route(conn, "how many tasks?", claude_fn=fn)
    assert "call me" not in r2["reply"]                        # never nags again
    # once an identity exists, no nudge even if the guard were cleared
    conn.execute("DELETE FROM settings WHERE key='onboarding_offered'"); conn.commit()
    vault_store.set_identity("Name: Sam (me)")
    r3 = router.route(conn, "how many tasks?", claude_fn=fn)
    conn.close()
    assert "set up my profile" not in r3["reply"]


def test_web_onboarding_banner_inline_save_and_dismiss(client, tmp_path, monkeypatch):
    from core.db import get_setting, delete_setting
    prof = tmp_path / "profile.md"
    prof.write_text("## Who I am\n- TODO\n")          # unconfigured: no # Identity
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    assert 'id="onboard"' in client.get("/").data.decode()       # nameless new user → shows
    # saving the name inline sets it and hides the banner; greeting now shows it
    assert client.post("/onboarding/name", data={"name": "Sam"}).status_code == 200
    conn = _db(); assert get_setting(conn, "display_name") == "Sam"; conn.close()
    home = client.get("/").data.decode()
    assert 'id="onboard"' not in home and "Sam" in home
    # nameless again → banner returns; ✕ dismiss hides it for good
    conn = _db(); delete_setting(conn, "display_name"); conn.close()
    assert 'id="onboard"' in client.get("/").data.decode()
    assert client.post("/onboarding/dismiss").status_code == 200
    assert 'id="onboard"' not in client.get("/").data.decode()


def test_router_set_name_saves_display_name(client):
    import json
    from core.db import get_setting
    conn = _db()
    res = router.route(conn, "call me Sam",
                       claude_fn=lambda p: json.dumps({"action": "set_name", "name": "Sam"}))
    assert res["applied"] == ["set_name"] and "Sam" in res["reply"]
    assert get_setting(conn, "display_name") == "Sam"
    conn.close()


def test_refile_records_correction(client):
    # a note refiled to a task on the Today feed logs a correction signal
    vault_store.create_note(title="Buy milk", body="", tags=["unsorted"])
    slug = vault_store.list_notes()[0]["slug"]
    client.post("/capture/refile", data={"kind": "note", "ref": slug, "to": "task"})
    conn = _db()
    got = recent_corrections(conn, 7)
    conn.close()
    assert any(s["kind"] == "refile" for s in got)


def test_suggester_proposes_and_arms_pending(client, tmp_path, monkeypatch):
    prof = tmp_path / "profile.md"
    prof.write_text("# Profile\n")
    monkeypatch.setattr(vault_store, "PROFILE_PATH", str(prof))
    conn = _db()
    for i in range(3):
        record_correction(conn, "refile", f"note->task {i}")
    tg = FakeTG()
    fired = proactive.maybe_suggest_profile_rule(
        conn, tg, "123",
        claude_fn=lambda p: '{"rule": "gym captures → personal category"}')
    assert fired and any("Add this to your profile" in s for s in tg.sent)
    assert router.peek_pending(conn)["kind"] == "profile_append"
    # confirming appends the rule to the tmp profile
    msg = router.execute_pending(conn, router.peek_pending(conn))
    conn.close()
    assert "gym captures" in prof.read_text() and "Added" in msg


def test_suggester_needs_three_signals(client):
    conn = _db()
    record_correction(conn, "refile", "one")
    fired = proactive.maybe_suggest_profile_rule(conn, FakeTG(), "123",
                                                 claude_fn=lambda p: '{"rule": "x"}')
    conn.close()
    assert fired is False                              # <3 signals → no suggestion
