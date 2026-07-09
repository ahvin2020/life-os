"""UI-layer tests: the labelled 'Do today' pill copy, lazy note creation, and the
captured-today feed (#imported exclusion + soft-delete/restore). Exercises the real
routes and the real markdown vault, in the style of test_app.py; uses the shared
`client` fixture from conftest.py."""

import os

import vault_store
from capture import create_task
from db import connect, today_iso


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
