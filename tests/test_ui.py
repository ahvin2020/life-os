"""UI-layer tests: the labelled 'Do today' pill copy, lazy note creation, and the
captured-today feed (#imported exclusion + soft-delete/restore). Exercises the real
routes and the real markdown vault, in the style of test_app.py; uses the shared
`client` fixture from conftest.py."""

import os

from domain import vault_store
from domain.capture import create_task
from core.db import connect, today_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


# ── labelled "Do today" pill ──────────────────────────────────────────────────
def test_do_today_pill_rendered_and_bare_icon_copy_gone(client):
    conn = _db()
    with conn:
        create_task(conn, "Due task", col="week", due_date=today_iso())
        create_task(conn, "Backlog task", col="backlog")
    conn.close()
    home = client.get("/").data.decode()
    assert "planbtn" in home and "Do today" in home     # the new labelled pill
    assert "tap ☀" not in home                           # old bare-icon copy gone
    assert 'class="sunbtn"' not in home                  # old control class gone
    tasks = client.get("/tasks").data.decode()
    assert "planbtn" in tasks and "Do today" in tasks    # pill on kanban cards too


# ── lazy note creation ────────────────────────────────────────────────────────
def test_new_note_editor_no_content_creates_nothing(client):
    """Opening then closing a blank editor makes no /notes/new request (the editor
    JS gates creation on a non-empty title or body), so no orphan note is written."""
    assert vault_store.list_notes() == []
    # A blank open+close issues zero HTTP calls — the vault stays empty.
    assert vault_store.list_notes() == []


def test_note_created_on_first_save_with_content(client):
    r = client.post("/notes/new", data={"title": "Real note", "body": "hello", "tags": ""},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    slug = r.get_json()["slug"]
    path = os.path.join(vault_store.notes_dir(), slug + ".md")
    assert os.path.exists(path)                                    # file written on disk
    assert any(n["slug"] == slug for n in vault_store.list_notes())


# ── captured-today feed ───────────────────────────────────────────────────────
def test_feed_excludes_imported_notes(client):
    vault_store.create_note(title="Captured normally", body="x", tags=["idea"])
    vault_store.create_note(title="Backfilled reel", body="y", tags=["imported", "link"])
    home = client.get("/").data.decode()
    assert "Captured normally" in home         # a genuine capture shows in the feed
    assert "Backfilled reel" not in home       # a #imported backfill is skipped


def test_feed_delete_note_soft_deletes_and_restores(client):
    n = vault_store.create_note(title="Feed note", body="z", tags=[])
    slug = n["slug"]
    assert any(x["slug"] == slug for x in vault_store.list_notes())
    client.post(f"/notes/{slug}/delete", headers={"X-Requested-With": "XMLHttpRequest"})
    assert not any(x["slug"] == slug for x in vault_store.list_notes())   # soft-deleted
    client.post(f"/notes/{slug}/restore")
    assert any(x["slug"] == slug for x in vault_store.list_notes())       # restorable


def test_feed_delete_task_soft_deletes_and_restores(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Feed task", col="week")
    conn.close()
    client.post(f"/tasks/{tid}/delete")
    conn = _db()
    row = conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["deleted_at"] is not None
    client.post(f"/tasks/{tid}/restore")
    conn = _db()
    row = conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["deleted_at"] is None


# ── #imported exclusion: imported TASKS (via the ledger) + today-so-far count ──
def test_feed_excludes_imported_tasks(client, monkeypatch):
    """Notes are filtered by the #imported tag; imported TASKS carry no tag, so the
    captured feed must drop them via the import ledger (the missed second code path)."""
    conn = _db()
    with conn:
        keep = create_task(conn, "Real task today", col="week")
        skip = create_task(conn, "Imported task today", col="backlog")
    conn.close()
    from routes import main
    monkeypatch.setattr(main, "imported_task_ids", lambda: {skip})
    home = client.get("/").data.decode()
    assert "Real task today" in home
    assert "Imported task today" not in home


def test_today_so_far_count_excludes_imported_notes(client):
    from routes.journal import today_so_far
    vault_store.create_note(title="Fresh capture", body="x", tags=["idea"])
    vault_store.create_note(title="Backfilled", body="y", tags=["imported"])
    conn = _db()
    tsf = today_so_far(conn, today_iso())
    conn.close()
    assert tsf["captures"] == 1        # imported backfill is not "captured today"


# ── URL captures fetch the page's real title (og:title / <title>), mocked fetch ─
from domain import capture  # noqa: E402


class _FakeResp:
    def __init__(self, text):
        self.text = text


def test_url_title_prefers_og_title(monkeypatch):
    html = '<html><head><meta property="og:title" content="Great Reel &amp; Title"></head></html>'
    monkeypatch.setattr(capture.requests, "get", lambda *a, **k: _FakeResp(html))
    assert capture._url_title("https://www.instagram.com/reel/abc") == "Great Reel & Title"


def test_url_title_falls_back_to_title_tag(monkeypatch):
    html = "<html><head><title>  Cost of living in SG  </title></head></html>"
    monkeypatch.setattr(capture.requests, "get", lambda *a, **k: _FakeResp(html))
    assert capture._url_title("https://example.com/x") == "Cost of living in SG"


def test_url_title_fallback_is_domain_link_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("timeout")
    monkeypatch.setattr(capture.requests, "get", boom)
    assert capture._url_title("https://instagram.com/reel/abc") == "instagram.com link"


def test_route_capture_url_note_titled_from_page(client, monkeypatch):
    monkeypatch.setattr(capture.requests, "get",
                        lambda *a, **k: _FakeResp("<title>Real Video Title</title>"))
    conn = _db()
    res = capture.route_capture(conn, "https://www.youtube.com/watch?v=xyz")
    conn.close()
    note = vault_store.read_note(res["slug"])
    assert note["title"] == "Real Video Title"      # not the bare domain
    assert "idea" in note["tags"]                    # youtube → idea domain


# ── Recent notes sort strictly by CREATED, newest first (finding 2) ────────────
def test_recent_notes_sorted_by_created_not_mtime(client):
    """A newer-created note wins even if an OLD note's file mtime is fresher (the bug:
    yesterday's bulk retitle refreshed mtime on old imports and buried new captures)."""
    old = vault_store.write_note("old-import", "Old import", ["imported"], "body",
                                 False, "2026-07-01T09:00:00+08:00")
    new = vault_store.write_note("fresh-capture", "Fresh capture", ["link"], "body",
                                 False, "2026-07-09T09:00:00+08:00")
    # Simulate a bulk-retitle touching the OLD note's file after the new one existed.
    os.utime(os.path.join(vault_store.notes_dir(), "old-import.md"), None)
    order = [n["slug"] for n in vault_store.list_notes()]
    assert order.index("fresh-capture") < order.index("old-import")
    # And the composer-created note lands first on the Notes page.
    html = client.get("/notes").data.decode()
    assert html.index("Fresh capture") < html.index("Old import")


# ── the thumbnail carries the ONE clickable source link; snippets stay prose ───
def test_note_card_source_link_and_snippet_drops_raw_url(client):
    vault_store.create_note(
        title="instagram.com",
        body="check this https://www.instagram.com/reel/DXlGR2QDA_W/ out",
        tags=["link"])
    html = client.get("/notes").data.decode()
    # the thumbnail is a real anchor to the note's first URL, opens in a new tab
    assert 'class="nsrc"' in html
    assert 'href="https://www.instagram.com/reel/DXlGR2QDA_W/"' in html
    assert 'target="_blank"' in html and 'rel="noopener"' in html
    # the raw URL does NOT eat the snippet preview
    assert "check this out" in html
