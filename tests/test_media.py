"""Image attachments: the shared /media upload+serve infra and its three surfaces
(notes frontmatter, journal entry, tasks.media column)."""

import base64
import io
import os

from core.db import connect
from domain import vault_store

# a 1x1 PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _upload(client, name="shot.png"):
    return client.post("/media/upload",
                       data={"file": (io.BytesIO(_PNG), name)},
                       content_type="multipart/form-data",
                       headers={"X-Requested-With": "XMLHttpRequest"})


def test_upload_saves_and_serves(client):
    r = _upload(client)
    j = r.get_json()
    assert j["status"] == "ok" and j["pointer"].startswith("vault/.media/")
    # the returned URL serves the bytes back
    got = client.get(j["url"])
    assert got.status_code == 200 and got.data == _PNG


def test_upload_accepts_a_document(client):
    """Attachments broadened beyond images — a document (PDF/txt) is now saved too, keeping
    its readable name; only a missing filename is rejected."""
    r = client.post("/media/upload",
                    data={"file": (io.BytesIO(b"a document"), "invoice.txt")},
                    content_type="multipart/form-data",
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["status"] == "ok" and j["pointer"].endswith("invoice.txt")


def test_upload_rejects_missing_filename(client):
    r = client.post("/media/upload",
                    data={"file": (io.BytesIO(b"nope"), "")},
                    content_type="multipart/form-data",
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 400


def test_media_serve_traversal_guarded(client):
    assert client.get("/media/..%2f..%2fapp.db").status_code == 404


def test_download_forces_attachment_with_real_name(client):
    p = client.post("/media/upload",
                    data={"file": (io.BytesIO(b"pdf-bytes"), "Jan Invoice.pdf")},
                    content_type="multipart/form-data",
                    headers={"X-Requested-With": "XMLHttpRequest"}).get_json()["pointer"]
    url = "/media/" + p.split("/")[-1]
    r = client.get(url + "?download=1")
    assert r.status_code == 200
    # sanitised original name is preserved for the download
    assert "Jan-Invoice.pdf" in r.headers.get("Content-Disposition", "")


def test_media_helpers_name_and_image_flag():
    ptr = "vault/.media/20260714-120000-abcd1234__Jan Report.PDF".replace(" ", "-")
    assert vault_store.media_display_name(ptr) == "Jan-Report.PDF"
    assert vault_store.media_is_image(ptr) is False
    assert vault_store.media_is_image("vault/.media/20260714-1-uX.jpg") is True
    # legacy pointer with no "__" marker → whole basename is the name
    assert vault_store.media_display_name("vault/.media/20260714-1-uX.jpg") == "20260714-1-uX.jpg"


def test_capture_with_media_attaches_to_note(client):
    """A web capture carrying an attachment files as a note with the media pointer."""
    p = _upload(client, "photo.png").get_json()["pointer"]
    r = client.post("/capture", data={"text": "beach trip", "type": "auto", "media": p},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    j = r.get_json()
    assert j["kind"] == "note"
    assert len(vault_store.read_note(j["slug"])["media_items"]) == 1


def test_capture_attachment_only_becomes_titled_note(client):
    """No text, just a file → a note titled by the filename (a lone attachment is enough)."""
    p = client.post("/media/upload",
                    data={"file": (io.BytesIO(b"x"), "passport.pdf")},
                    content_type="multipart/form-data",
                    headers={"X-Requested-With": "XMLHttpRequest"}).get_json()["pointer"]
    j = client.post("/capture", data={"text": "", "type": "auto", "media": p},
                    headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    assert j["kind"] == "note" and j["title"] == "passport.pdf"


def test_capture_empty_and_no_media_rejected(client):
    r = client.post("/capture", data={"text": "", "type": "auto", "media": ""},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 400


def test_note_carries_multiple_images(client):
    p1 = _upload(client, "a.png").get_json()["pointer"]
    p2 = _upload(client, "b.png").get_json()["pointer"]
    r = client.post("/notes/new", data={"title": "Trip", "body": "x", "tags": "",
                                        "media": p1 + "," + p2},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    slug = r.get_json()["slug"]
    note = vault_store.read_note(slug)
    assert len(note["media_items"]) == 2
    assert note["media_items"][0]["url"].startswith("/media/")


def test_task_carries_and_clears_images(client):
    from domain.tasks_core import task_dict
    p = _upload(client, "receipt.png").get_json()["pointer"]
    tid = client.post("/tasks/new", data={"title": "Receipt task", "media": p},
                      headers={"X-Requested-With": "XMLHttpRequest"}).get_json()["id"]
    conn = _db(); t = task_dict(conn, conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()); conn.close()
    assert t["media"] == p
    client.post(f"/tasks/{tid}/edit", data={"media": ""}, headers={"X-Requested-With": "XMLHttpRequest"})
    conn = _db(); row = conn.execute("SELECT media FROM tasks WHERE id=?", (tid,)).fetchone(); conn.close()
    assert not (row["media"] or "")


def test_journal_entry_carries_images(client):
    p1 = _upload(client, "j1.png").get_json()["pointer"]
    p2 = _upload(client, "j2.png").get_json()["pointer"]
    day = "2026-07-14"
    r = client.post("/journal/entry", data={"text": "beach day", "day": day, "media": p1 + "," + p2},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    page = vault_store.read_journal(day)
    e = page["entries"][-1]
    assert e["text"] == "beach day" and len(e["media_items"]) == 2


def test_journal_image_only_entry_allowed(client):
    p = _upload(client, "solo.png").get_json()["pointer"]
    day = "2026-07-15"
    r = client.post("/journal/entry", data={"text": "", "day": day, "media": p},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    assert len(vault_store.read_journal(day)["entries"][-1]["media_items"]) == 1


def test_note_save_clears_media(client):
    p1 = _upload(client, "a.png").get_json()["pointer"]
    slug = client.post("/notes/new", data={"title": "N", "body": "", "tags": "", "media": p1},
                       headers={"X-Requested-With": "XMLHttpRequest"}).get_json()["slug"]
    assert len(vault_store.read_note(slug)["media_items"]) == 1
    client.post(f"/notes/{slug}/save", data={"title": "N", "body": "", "tags": "", "media": ""},
                headers={"X-Requested-With": "XMLHttpRequest"})
    assert vault_store.read_note(slug)["media_items"] == []
