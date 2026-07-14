"""Link enrichment + URL dedupe tests (findings 4 & 5).

Covers: URL normalisation, metadata fetch mocked per domain type, enrichment
application, the claude/network failure fallback, the async update path, going-forward
capture + import dedupe, and the one-time same-URL merge cleanup. Uses the shared
`client` fixture (throwaway vault); no network, no real claude — everything mocked."""

import os
import threading

from domain import capture
from domain import vault_store
from core.db import connect

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import import_common  # noqa: E402
from dedupe_notes import merge_duplicate_url_notes  # noqa: E402


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


class _FakeResp:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data or {}

    def json(self):
        return self._data


# ── URL normalisation (finding 5b) ─────────────────────────────────────────────
def test_normalize_strips_tracking_and_fragment():
    a = capture.normalize_url("https://www.instagram.com/reel/DXlGR2QDA_W/?igsh=ZXJ0YTIy")
    b = capture.normalize_url("http://instagram.com/reel/DXlGR2QDA_W?utm_source=x#comments")
    assert a == b
    assert "igsh" not in a and "utm" not in a and "#" not in a


def test_normalize_keeps_meaningful_query():
    u = capture.normalize_url("https://youtube.com/watch?v=abc123&utm_source=share")
    assert "v=abc123" in u and "utm_source" not in u


# ── metadata fetch, mocked per domain type (finding 4) ─────────────────────────
def test_fetch_youtube_uses_oembed(monkeypatch):
    calls = {}

    def fake_get(url, *a, **k):
        calls["url"] = url
        return _FakeResp(data={"title": "CPF hacks explained", "author_name": "KLI"})

    monkeypatch.setattr(capture.requests, "get", fake_get)
    meta = capture.fetch_link_metadata("https://youtu.be/xyz")
    assert "oembed" in calls["url"]
    assert meta["title"] == "CPF hacks explained" and meta["author"] == "KLI"


def test_fetch_generic_parses_og(monkeypatch):
    html = ('<meta property="og:title" content="Cost of living in SG">'
            '<meta property="og:description" content="How much you really need">')
    monkeypatch.setattr(capture.requests, "get", lambda *a, **k: _FakeResp(text=html))
    meta = capture.fetch_link_metadata("https://example.com/x")
    assert meta["title"] == "Cost of living in SG"
    assert meta["description"] == "How much you really need"


def test_fetch_instagram_block_degrades_gracefully(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("login required")

    monkeypatch.setattr(capture.requests, "get", boom)
    assert capture.fetch_link_metadata("https://instagram.com/reel/abc") == {}


# ── enrichment application (finding 4) ─────────────────────────────────────────
def test_enrich_note_rewrites_title_summary_tags(client):
    note = vault_store.create_note(
        title="instagram.com",
        body="Add to note\n\nhttps://www.instagram.com/reel/DXlGR2QDA_W/?igsh=abc",
        tags=["link", "idea"])
    fetch = lambda url: {"title": "Passive income reel", "description": "5 ways", "site": "instagram.com"}
    claude = lambda prompt: '{"title": "5 passive income ideas", "summary": "Reel Sam saved on side income", "tags": ["income", "idea"]}'
    saved = capture.enrich_note(note["slug"], fetch_fn=fetch, claude_fn=claude)
    assert saved["title"] == "5 passive income ideas"
    assert "income" in saved["tags"] and "link" in saved["tags"]   # union kept #link
    assert "Reel Sam saved on side income" in saved["body"]
    assert "instagram.com/reel/DXlGR2QDA_W" in saved["body"]       # URL retained


def test_enrich_prompt_includes_metadata_words_and_profile(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(vault_store, "read_profile", lambda: "PROFILE-MARKER")
    vault_store.create_note(title="x", body="watch this https://youtu.be/z later for the CPF video",
                            tags=["link"])
    slug = vault_store.list_notes()[0]["slug"]

    def claude(prompt):
        captured["prompt"] = prompt
        return '{"title": "t", "summary": "s", "tags": ["content"]}'

    capture.enrich_note(slug, fetch_fn=lambda u: {"title": "CPF video", "site": "youtube"},
                        claude_fn=claude)
    p = captured["prompt"]
    assert "CPF video" in p            # fetched metadata
    assert "later for the CPF video" in p  # user's own words
    assert "PROFILE-MARKER" in p       # profile.md


# ── failure fallback (finding 4) ───────────────────────────────────────────────
def test_enrich_note_claude_failure_leaves_note_untouched(client):
    note = vault_store.create_note(title="instagram.com", body="https://instagram.com/reel/q",
                                   tags=["link"])
    def boom(prompt):
        raise RuntimeError("claude down")
    assert capture.enrich_note(note["slug"], fetch_fn=lambda u: {}, claude_fn=boom) is None
    after = vault_store.read_note(note["slug"])
    assert after["title"] == "instagram.com" and after["body"].strip() == "https://instagram.com/reel/q"


def test_enrich_note_junk_json_is_fallback(client):
    note = vault_store.create_note(title="x", body="https://example.com/a", tags=["link"])
    assert capture.enrich_note(note["slug"], fetch_fn=lambda u: {}, claude_fn=lambda p: "sorry no") is None


def test_enrich_note_no_url_is_noop(client):
    note = vault_store.create_note(title="plain", body="just a thought", tags=[])
    assert capture.enrich_note(note["slug"], fetch_fn=lambda u: {}, claude_fn=lambda p: "{}") is None


# ── async update path (finding 4) ──────────────────────────────────────────────
def test_schedule_enrichment_runs_in_background(client, monkeypatch):
    done = threading.Event()
    seen = {}

    def fake_enrich(slug, **k):
        seen["slug"] = slug
        done.set()

    monkeypatch.setattr(capture, "enrich_note", fake_enrich)
    monkeypatch.setenv("LIFEOS_ENRICH_LINKS", "1")
    capture.schedule_enrichment("some-slug")
    assert done.wait(2.0) and seen["slug"] == "some-slug"


def test_schedule_enrichment_disabled_is_noop(client, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(capture, "enrich_note", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setenv("LIFEOS_ENRICH_LINKS", "0")
    capture.schedule_enrichment("x")
    assert called["n"] == 0


# ── capture dedupe (finding 5b) ────────────────────────────────────────────────
def test_capture_same_url_touches_not_twins(client):
    conn = _db()
    r1 = capture.route_capture(conn, "https://www.instagram.com/reel/DUP/?igsh=one")
    r2 = capture.route_capture(conn, "https://instagram.com/reel/DUP?utm_source=two")
    conn.close()
    assert r2.get("deduped") is True
    assert r1["slug"] == r2["slug"]
    link_notes = [n for n in vault_store.list_notes() if "link" in n["tags"]]
    assert len(link_notes) == 1


# ── import dedupe (finding 5b) ─────────────────────────────────────────────────
def test_import_dedupes_link_notes_by_url(client):
    conn = _db()
    ledger = {}
    r1 = import_common.apply_item(
        conn, "reel A", {"type": "note", "tags": ["link"], "body": "https://insta.com/p/Z?igsh=a"}, ledger)
    r2 = import_common.apply_item(
        conn, "reel A reshared", {"type": "note", "tags": ["link"], "body": "https://www.insta.com/p/Z?utm_source=b"}, ledger)
    conn.close()
    assert r1["status"] == "created"
    assert r2.get("deduped") is True and r2["slug"] == r1["slug"]
    assert len([n for n in vault_store.list_notes() if "link" in n["tags"]]) == 1


# ── one-time merge cleanup (finding 5a) ────────────────────────────────────────
def test_merge_duplicate_url_notes(client):
    vault_store.write_note("keep", "Sheet", ["link"], "https://docs.google.com/x/edit#gid=1",
                           False, "2026-07-01T10:00:00+08:00")
    vault_store.write_note("dup1", "Sheet copy", ["link", "budget"],
                           "https://docs.google.com/x/edit#gid=99\n\nquarterly numbers",
                           False, "2026-07-03T10:00:00+08:00")
    vault_store.write_note("dup2", "Sheet copy 2", ["link"], "https://www.docs.google.com/x/edit/",
                           False, "2026-07-04T10:00:00+08:00")
    vault_store.write_note("other", "Unrelated", ["link"], "https://example.com/y", False,
                           "2026-07-02T10:00:00+08:00")

    rep = merge_duplicate_url_notes(dry_run=False)
    assert rep["groups"] == 1 and rep["removed"] == 2
    assert vault_store.read_note("keep") is not None        # earliest-created survives
    assert vault_store.read_note("dup1") is None            # soft-deleted
    assert vault_store.read_note("dup2") is None
    assert vault_store.read_note("other") is not None       # different URL untouched
    survivor = vault_store.read_note("keep")
    assert "budget" in survivor["tags"]                     # tags unioned
    assert "quarterly numbers" in survivor["body"]          # differing body concatenated


def test_merge_dry_run_changes_nothing(client):
    vault_store.write_note("a", "A", ["link"], "https://one.com/x", False, "2026-07-01T10:00:00+08:00")
    vault_store.write_note("b", "B", ["link"], "https://one.com/x", False, "2026-07-02T10:00:00+08:00")
    rep = merge_duplicate_url_notes(dry_run=True)
    assert rep["groups"] == 1 and rep["removed"] == 1
    assert vault_store.read_note("a") and vault_store.read_note("b")   # nothing deleted
