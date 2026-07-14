"""Document access + the facts cache (domain/docs.py, routes/docs.py, router
find_document, the daemon document send, and the schema v7 migration).

Retrieval/scan Claude calls are mocked; the throwaway vault + a tmp root stand in for
Kelvin's synced folders.
"""

import json
import os

from ai import router
from core.db import connect, set_setting
from domain import docs, vault_store


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _root(conn, tmp_path):
    set_setting(conn, "document_roots", json.dumps([str(tmp_path)]))
    return 1                                   # index 0 is always the vault


# ── roots + search ─────────────────────────────────────────────────────────────
def test_default_roots_is_vault_only(client):
    conn = _db()
    roots = docs.document_roots(conn)
    conn.close()
    assert roots == [vault_store.VAULT_DIR]


def test_search_ranks_and_whitelists(client, tmp_path):
    conn = _db()
    _root(conn, tmp_path)
    (tmp_path / "Scoot booking Jul.pdf").write_text("x")
    (tmp_path / "cruise-invoice.pdf").write_text("x")
    (tmp_path / "notes.exe").write_text("x")          # not a doc ext
    hits = docs.search_documents(conn, "scoot booking")
    conn.close()
    names = [h["name"] for h in hits]
    assert "Scoot booking Jul.pdf" == names[0]
    assert "notes.exe" not in names                   # extension whitelist


def test_short_fragment_does_not_match_long_query_word(client, tmp_path):
    """A 2-char filename token ('ex' in 'ex-msian…') must NOT match the query word 'expire' —
    that noise used to bury the real passport doc for 'passport expire'."""
    conn = _db()
    _root(conn, tmp_path)
    (tmp_path / "ex-msian-who-left.pdf").write_text("x")
    (tmp_path / "lee jun kai passport.pdf").write_text("x")
    hits = docs.search_documents(conn, "passport expire")
    conn.close()
    names = [h["name"] for h in hits]
    assert names == ["lee jun kai passport.pdf"]        # the noise file is gone


def test_content_aware_surfaces_filename_blind_document(client, tmp_path):
    """A document whose FILENAME lacks the query word is still surfaced when its extracted
    fact matches — the facts cache acts as a content index. 'shirou-travel-2026.pdf' has no
    'passport' in its name, but its scanned fact 'Passport expiry (Xin Yi)' does."""
    from core.db import now_iso
    conn = _db()
    _root(conn, tmp_path)
    blind = tmp_path / "shirou-travel-2026.pdf"
    blind.write_text("x")
    (tmp_path / "unrelated-recipe.pdf").write_text("x")
    conn.execute(
        "INSERT INTO doc_facts(path, label, category, value, event_date, extracted_at) "
        "VALUES (?,?,?,?,?,?)",
        (str(blind), "Passport expiry (Xin Yi)", "expiry", "21 Mar 2031", "2031-03-21", now_iso()))
    conn.commit()
    names = [h["name"] for h in docs.search_documents(conn, "passport")]
    conn.close()
    assert "shirou-travel-2026.pdf" in names            # found by fact, not filename
    assert "unrelated-recipe.pdf" not in names          # no matching fact or name


def test_content_aware_dedupes_against_filename_hit(client, tmp_path):
    """A doc matched BOTH by filename and by a fact appears once, not twice."""
    from core.db import now_iso
    conn = _db()
    _root(conn, tmp_path)
    f = tmp_path / "kelvin passport.pdf"
    f.write_text("x")
    conn.execute(
        "INSERT INTO doc_facts(path, label, category, value, event_date, extracted_at) "
        "VALUES (?,?,?,?,?,?)",
        (str(f), "Passport expiry", "expiry", "2032", "2032-03-21", now_iso()))
    conn.commit()
    paths = [h["path"] for h in docs.search_documents(conn, "passport")]
    conn.close()
    assert paths.count(str(f)) == 1


def test_dropbox_widens_when_full_query_misses(client, tmp_path, monkeypatch):
    """Dropbox server-search ANDs name+content, so 'passport expire' finds nothing; the
    widener must drop the attribute word and retry with 'passport'."""
    from ai import dropbox_client
    monkeypatch.setattr(dropbox_client, "is_configured", lambda c: True)
    calls = []

    def fake_search(c, q, limit=5, client=None):
        calls.append(q)
        return [{"name": "lee jun kai passport.pdf", "dbx_path": "/p.pdf", "source": "dropbox"}] \
            if q == "passport" else []                  # only the bare noun matches
    monkeypatch.setattr(dropbox_client, "search", fake_search)
    conn = _db()
    hits = docs.search_documents(conn, "passport expire")
    conn.close()
    assert any(h.get("source") == "dropbox" for h in hits)
    assert "passport" in calls                          # it widened down to the noun


def test_local_path_downloads_a_gmail_attachment(client, monkeypatch):
    """A gmail_attachment candidate resolves by DOWNLOADING the attachment to a temp path
    (so it reads/delivers like any doc) — same shape as the Dropbox branch."""
    from ai import google_client
    seen = {}
    def fake_dl(msg_id, att_id, filename="", service=None):
        seen["args"] = (msg_id, att_id, filename)
        return "/tmp/dl-" + filename
    monkeypatch.setattr(google_client, "download_attachment", fake_dl)
    hit = {"source": "gmail_attachment", "msg_id": "m1", "attachment_id": "a1",
           "name": "Itinerary.pdf"}
    assert docs.local_path_for_hit(None, hit) == "/tmp/dl-Itinerary.pdf"
    assert seen["args"] == ("m1", "a1", "Itinerary.pdf")


def test_search_skips_dot_dirs(client, tmp_path):
    conn = _db()
    _root(conn, tmp_path)
    hidden = tmp_path / ".trash"
    hidden.mkdir()
    (hidden / "passport.pdf").write_text("x")
    hits = docs.search_documents(conn, "passport")
    conn.close()
    assert hits == []                                 # dot-dir never walked


# ── traversal guard ────────────────────────────────────────────────────────────
def test_resolve_doc_blocks_traversal(client, tmp_path):
    conn = _db()
    _root(conn, tmp_path)
    key = docs._root_key(str(tmp_path))
    (tmp_path / "ok.pdf").write_text("x")
    assert docs.resolve_doc(conn, key, "ok.pdf")               # real file inside root
    assert docs.resolve_doc(conn, key, "../../etc/hosts") is None
    assert docs.resolve_doc(conn, key, "/etc/hosts") is None
    assert docs.resolve_doc(conn, "deadbeef00", "ok.pdf") is None   # unknown root key
    conn.close()


def test_web_route_serves_and_404s(client, tmp_path):
    conn = _db()
    _root(conn, tmp_path)
    key = docs._root_key(str(tmp_path))
    (tmp_path / "policy.pdf").write_text("hello")
    conn.close()
    ok = client.get(f"/docs/{key}/policy.pdf")
    assert ok.status_code == 200 and b"hello" in ok.data
    assert client.get(f"/docs/{key}/../../etc/hosts").status_code == 404


# ── info extraction (the slow live read) ───────────────────────────────────────
def test_extract_info_uses_read_tool_with_data_rail(client, tmp_path, monkeypatch):
    box = {}
    def fake_claude(prompt, timeout=60, tools="", add_dir=None):
        box["prompt"] = prompt
        box["tools"] = tools
        box["add_dir"] = add_dir
        return "Expires 12 Mar 2029."
    monkeypatch.setattr(docs, "call_claude", fake_claude)
    out = docs.extract_info(str(tmp_path / "passport.pdf"), "when does it expire?")
    assert out == "Expires 12 Mar 2029."
    assert box["tools"] == "Read"                     # only Read granted
    assert "DATA" in box["prompt"]                    # injection rail present


# ── router find_document ───────────────────────────────────────────────────────
def test_router_find_document_file_mode(client, tmp_path):
    conn = _db()
    _root(conn, tmp_path)
    (tmp_path / "Tenancy agreement.pdf").write_text("x")
    out = router.route(conn, "send me the tenancy agreement", claude_fn=lambda p: json.dumps(
        {"action": "find_document", "query": "tenancy agreement", "mode": "file", "question": None}))
    conn.close()
    assert out["applied"] == ["find_document"]
    assert out["document"].endswith("Tenancy agreement.pdf")   # path handed to the daemon


def test_router_find_document_link_mode(client, tmp_path):
    conn = _db()
    _root(conn, tmp_path)
    key = docs._root_key(str(tmp_path))
    (tmp_path / "Insurance.pdf").write_text("x")
    out = router.route(conn, "link me the insurance policy", claude_fn=lambda p: json.dumps(
        {"action": "find_document", "query": "insurance", "mode": "link", "question": None}))
    conn.close()
    assert f"/docs/{key}/Insurance.pdf" in out["reply"]
    assert out["document"] is None


def test_router_find_document_none(client, tmp_path):
    conn = _db()
    _root(conn, tmp_path)
    out = router.route(conn, "send me my will", claude_fn=lambda p: json.dumps(
        {"action": "find_document", "query": "last will testament", "mode": "file", "question": None}))
    conn.close()
    assert "No document" in out["reply"] and out["document"] is None


# ── facts cache: scan + instant answer ─────────────────────────────────────────
def test_scan_extracts_facts_and_dedupes(client, tmp_path):
    conn = _db()
    _root(conn, tmp_path)
    (tmp_path / "scoot.pdf").write_text("x")
    calls = []
    def fake(prompt):
        calls.append(prompt)
        return json.dumps({"facts": [
            {"label": "Scoot booking", "category": "booking", "value": "ref ABC123", "date": None},
            {"label": "Passport", "category": "expiry", "value": "expires", "date": "2029-03-12"}]})
    got = docs.scan_documents(conn, claude_fn=fake)
    assert len(got) == 2
    # second scan: file already seen (same mtime) → no re-read
    again = docs.scan_documents(conn, claude_fn=fake)
    assert again == [] and len(calls) == 1
    conn.close()


def test_query_and_answer_from_facts(client, tmp_path):
    conn = _db()
    with conn:
        conn.execute("INSERT INTO doc_facts(path,label,category,value,event_date,extracted_at) "
                     "VALUES ('/x','Scoot booking','booking','ref ABC123',NULL,'2026-07-13T00:00:00Z')")
    # question-shaped → instant answer
    ans = docs.answer_from_facts(conn, "what's my scoot booking number?")
    assert ans and "ABC123" in ans
    # not question-shaped (a capture) → None, so it won't hijack normal messages
    assert docs.answer_from_facts(conn, "book the scoot flight") is None
    conn.close()


def test_upcoming_renewals_window(client):
    conn = _db()
    with conn:
        conn.execute("INSERT INTO doc_facts(path,label,category,value,event_date,extracted_at) "
                     "VALUES ('/p','Passport','expiry','expires','2026-08-01','t')")
        conn.execute("INSERT INTO doc_facts(path,label,category,value,event_date,extracted_at) "
                     "VALUES ('/q','Lease','renewal','renew','2030-01-01','t')")
    near = docs.upcoming_renewals(conn, "2026-07-13", lead_days=180)
    conn.close()
    assert [r["label"] for r in near] == ["Passport"]     # far-future lease excluded


# ── schema v7 migration ────────────────────────────────────────────────────────
def test_migration_creates_doc_facts(client):
    conn = _db()
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='doc_facts'").fetchone()
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    conn.close()
    assert row is not None and int(ver[0]) >= 7
