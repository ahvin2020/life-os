"""Attachment garbage collection: vault_store.purge_orphan_attachments — the daily sweep
that reclaims .media/.audio files nothing points at anymore, once past the 30-day undo
window. Locks the invariants the delete/undo model depends on:

  • a file referenced by a live note / journal entry / task is NEVER removed;
  • a still-restorable trashed note keeps its attachment until its window lapses (undo-safe);
  • a just-captured orphan is shielded by the file-age guard (journal-delete undo grace);
  • pre-existing orphans (from past deletions that never cleaned up) are reclaimed;
  • a non-image DOCUMENT filed via the bot (a note with `media:` frontmatter) is covered —
    the alignment with the photo/document capture feature.
"""

import os
import time

from core.db import connect, now_sg
from domain import vault_store as vs
from domain.capture import create_task, route_capture

_OLD = time.time() - 40 * 86400          # older than the 30-day window (age guard fails)


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _mk(folder, name, ts=_OLD):
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(b"x")
    os.utime(path, (ts, ts))
    return path


def _exists(folder, name):
    return os.path.exists(os.path.join(folder, name))


def test_sweep_keeps_referenced_removes_orphans(client):
    conn = _db()
    md, ad = vs.media_dir(), vs.audio_dir()

    _mk(md, "orphan-old.jpg")                                   # (1) unreferenced + old → gone
    _mk(ad, "orphan-old.oga")                                   # (2) audio orphan → gone
    _mk(md, "recent-orphan.jpg", ts=time.time())               # (3) unreferenced but fresh → kept

    _mk(md, "kept-note.jpg")
    vs.create_note("Has photo", "body", media="vault/.media/kept-note.jpg")
    _mk(md, "kept-journal.jpg")
    vs.append_journal_entry(now_sg().date().isoformat(), "trip",
                            media="vault/.media/kept-journal.jpg")
    _mk(md, "kept-task.jpg")
    tid = create_task(conn, "task with file")
    conn.execute("UPDATE tasks SET media=? WHERE id=?",
                 ("vault/.media/kept-task.jpg", tid))
    conn.commit()

    removed = vs.purge_orphan_attachments(conn, days=30)

    assert removed == 2
    assert not _exists(md, "orphan-old.jpg")
    assert not _exists(ad, "orphan-old.oga")
    assert _exists(md, "recent-orphan.jpg")                     # age guard shields undo
    assert _exists(md, "kept-note.jpg")
    assert _exists(md, "kept-journal.jpg")
    assert _exists(md, "kept-task.jpg")


def test_deleted_note_attachment_survives_undo_window_then_reclaimed(client):
    conn = _db()
    md = vs.media_dir()
    _mk(md, "note-file.jpg")
    vs.create_note("Doomed", "body", media="vault/.media/note-file.jpg")

    vs.delete_note("doomed")                                    # soft-delete → trash
    vs.purge_orphan_attachments(conn, days=30)
    assert _exists(md, "note-file.jpg"), "trashed-within-window keeps its attachment"

    # age the trash file's deletion stamp past the window → reference lapses
    tdir = vs.trash_dir()
    for name in os.listdir(tdir):
        parts = name.split(".")
        parts[-2] = "20200101000000"
        os.rename(os.path.join(tdir, name), os.path.join(tdir, ".".join(parts)))
    vs.purge_orphan_attachments(conn, days=30)
    assert not _exists(md, "note-file.jpg"), "after the undo window, the file is reclaimed"


def test_non_image_document_note_is_covered(client):
    """A non-image document (PDF) filed by the bot lands as a note with `media:`
    frontmatter (capture.route_capture, forced note) — the sweep's note scan must keep it,
    so the photo/document capture feature and the GC stay aligned."""
    conn = _db()
    md = vs.media_dir()
    base = vs.new_media_basename("invoice.pdf")
    _mk(md, base)                                               # old mtime — only the note ref protects it
    route_capture(conn, "", source="telegram", forced="note",
                  enrich="off", media="vault/.media/" + base)

    removed = vs.purge_orphan_attachments(conn, days=30)
    assert removed == 0
    assert _exists(md, base), "a document filed as a note must not be swept"
