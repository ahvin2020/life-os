"""Journal per-entry management — byte-preserving edit/delete of a single
'## HH:MM' section (today AND past days), duplicate-timestamp disambiguation, and
the Undo restore path. Backend/vault_store + route level."""

import os

from domain import vault_store
from core.db import connect

DAY = "2026-07-09"
RAW = (
    "# Thursday 9 July 2026\n"
    "\n"
    "## 09:00 · voice\n"
    "Morning workout done.\n"
    "\n"
    "## 13:45\n"
    "Lunch with the team.\n"
    "\n"
    "## 13:45\n"
    "Second entry, same minute.\n"
    "\n"
    "## 20:00\n"
    "Evening reflection.\n"
)


def _seed():
    return vault_store.save_journal_raw(DAY, RAW)


# ── vault_store: edit rewrites ONLY the target, preserving other sections ──────
def test_edit_rewrites_only_target_section(client):
    _seed()
    page = vault_store.edit_journal_entry(DAY, "20:00", 0, "Rewrote the evening note.")
    entries = {(e["time"], e["text"]) for e in page["entries"]}
    assert ("20:00", "Rewrote the evening note.") in entries
    # every OTHER section is byte-for-byte intact
    assert "Morning workout done." in page["raw"]
    assert "Lunch with the team." in page["raw"]
    assert "Second entry, same minute." in page["raw"]
    assert page["raw"].count("## 13:45") == 2                  # duplicates untouched
    assert "## 09:00 · voice" in page["raw"]                    # header + source preserved


def test_delete_removes_only_target_section(client):
    _seed()
    page = vault_store.delete_journal_entry(DAY, "09:00", 0)
    assert "Morning workout done." not in page["raw"]
    assert "## 09:00" not in page["raw"]
    # the other three entries survive
    times = [e["time"] for e in page["entries"]]
    assert times == ["13:45", "13:45", "20:00"]
    assert "Evening reflection." in page["raw"]


# ── duplicate HH:MM disambiguation via occurrence index ───────────────────────
def test_duplicate_timestamp_disambiguation(client):
    _seed()
    # edit the SECOND 13:45 only
    page = vault_store.edit_journal_entry(DAY, "13:45", 1, "CHANGED second.")
    dupes = [e["text"] for e in page["entries"] if e["time"] == "13:45"]
    assert dupes == ["Lunch with the team.", "CHANGED second."]  # first intact, second changed

    # delete the FIRST 13:45 only
    page = vault_store.delete_journal_entry(DAY, "13:45", 0)
    dupes = [e["text"] for e in page["entries"] if e["time"] == "13:45"]
    assert dupes == ["CHANGED second."]                          # only the first was removed


def test_edit_missing_entry_returns_none(client):
    _seed()
    assert vault_store.edit_journal_entry(DAY, "07:00", 0, "x") is None
    assert vault_store.delete_journal_entry(DAY, "13:45", 5) is None   # out-of-range occurrence


# ── Undo restores byte-for-byte via the raw snapshot ──────────────────────────
def test_undo_restore_roundtrips(client):
    _seed()
    before = vault_store.read_journal(DAY)["raw"]
    vault_store.delete_journal_entry(DAY, "20:00", 0)
    assert "Evening reflection." not in vault_store.read_journal(DAY)["raw"]
    vault_store.save_journal_raw(DAY, before)                    # the Undo path
    assert vault_store.read_journal(DAY)["raw"] == RAW           # exact restore


# ── route level: prev_raw returned, 404 on missing, past days too ─────────────
def test_entry_save_route_returns_prev_raw(client):
    _seed()
    r = client.post(f"/journal/{DAY}/entry/13:45/save",
                    data={"idx": "0", "text": "Lunch rewritten."})
    assert r.status_code == 200
    j = r.get_json()
    assert j["status"] == "ok"
    assert j["prev_raw"] == RAW                                  # snapshot for Undo
    assert "Lunch rewritten." in j["raw"]
    assert "Second entry, same minute." in j["raw"]             # sibling preserved


def test_entry_delete_route_and_404(client):
    _seed()
    r = client.post(f"/journal/{DAY}/entry/09:00/delete", data={"idx": "0"})
    assert r.status_code == 200 and "Morning workout done." not in r.get_json()["raw"]
    # missing entry → 404
    r2 = client.post(f"/journal/{DAY}/entry/09:00/delete", data={"idx": "0"})
    assert r2.status_code == 404


# ── voice journal entries carry a playable recording ──────────────────────────
def test_voice_entry_stores_and_parses_audio_pointer(client):
    ptr = "vault/.audio/voice-20260709-090000.oga"
    page = vault_store.append_journal_entry(DAY, "Morning workout done.", audio=ptr)
    e = page["entries"][0]
    assert e["audio"] == ptr and e["source"] == ""      # pointer split out of the slot
    assert f"## {e['time']} · audio:{ptr}" in page["raw"]  # header carries it verbatim
    # editing the body leaves the recording pointer intact (header preserved)
    page = vault_store.edit_journal_entry(DAY, e["time"], 0, "Edited body.")
    assert page["entries"][0]["audio"] == ptr


def test_journal_entry_audio_route_serves_and_404s(client):
    ptr = "vault/.audio/voice-20260709-181500.oga"
    vault_store.append_journal_entry(DAY, "Gym felt great.", audio=ptr)
    # drop a real file where the route resolves the basename
    os.makedirs(vault_store.audio_dir(), exist_ok=True)
    with open(os.path.join(vault_store.audio_dir(), os.path.basename(ptr)), "wb") as f:
        f.write(b"OggS-fake")
    e = vault_store.read_journal(DAY)["entries"][0]
    r = client.get(f"/journal/{DAY}/entry/{e['time']}/audio?i=0")
    assert r.status_code == 200 and r.headers["Content-Type"] == "audio/ogg"
    # an entry with no recording → 404
    r2 = client.get(f"/journal/{DAY}/entry/00:00/audio?i=0")
    assert r2.status_code == 404
