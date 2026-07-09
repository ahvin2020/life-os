"""Notes: markdown vault gallery (pinned + recent), tag filter chips, live search,
editor with autosave, pin, and soft-delete. Notes live as files under vault/notes/."""

from __future__ import annotations

from flask import Blueprint, render_template, request, jsonify

from web_core import db, respond
import vault_store

bp = Blueprint("notes", __name__)


@bp.route("/notes")
def notes_page():
    notes = vault_store.list_notes()
    pinned = [n for n in notes if n["pinned"]]
    recent = [n for n in notes if not n["pinned"]]
    tagset = sorted({t for n in notes for t in n["tags"]})
    return render_template("notes.html", pinned=pinned, recent=recent,
                           tags=tagset, count=len(notes), active="notes")


@bp.route("/notes/new", methods=["POST"])
def note_new():
    f = request.form
    title = (f.get("title") or "Untitled").strip()
    body = f.get("body") or ""
    tags = [t.strip().lstrip("#") for t in (f.get("tags") or "").split(",") if t.strip()]
    note = vault_store.create_note(title=title, body=body, tags=tags)
    if _ajax():
        return jsonify({"status": "ok", "slug": note["slug"], "note": note})
    return respond(True, "Note created", to="/notes")


@bp.route("/notes/<slug>")
def note_get(slug):
    note = vault_store.read_note(slug)
    if not note:
        return jsonify({"status": "error", "message": "not found"}), 404
    return jsonify({"status": "ok", "note": note})


@bp.route("/notes/<slug>/save", methods=["POST"])
def note_save(slug):
    note = vault_store.read_note(slug)
    if not note:
        return jsonify({"status": "error", "message": "not found"}), 404
    f = request.form
    title = f.get("title", note["title"]).strip() or note["title"]
    body = f.get("body", note["body"])
    if "tags" in f:
        tags = [t.strip().lstrip("#") for t in f.get("tags").split(",") if t.strip()]
    else:
        tags = note["tags"]
    pinned = note["pinned"]
    if "pinned" in f:
        pinned = f.get("pinned") in ("1", "true", "on")
    saved = vault_store.write_note(slug, title, tags, body, pinned, note["created"])
    return jsonify({"status": "ok", "note": saved})


@bp.route("/notes/<slug>/pin", methods=["POST"])
def note_pin(slug):
    note = vault_store.read_note(slug)
    if not note:
        return jsonify({"status": "error", "message": "not found"}), 404
    saved = vault_store.write_note(
        slug, note["title"], note["tags"], note["body"],
        not note["pinned"], note["created"])
    return jsonify({"status": "ok", "pinned": saved["pinned"]})


@bp.route("/notes/<slug>/delete", methods=["POST"])
def note_delete(slug):
    ok = vault_store.delete_note(slug)
    if _ajax():
        return jsonify({"status": "ok" if ok else "error", "slug": slug})
    return respond(ok, "Note deleted" if ok else "Not found", to="/notes")


@bp.route("/notes/<slug>/restore", methods=["POST"])
def note_restore(slug):
    ok = vault_store.restore_note(slug)
    return jsonify({"status": "ok" if ok else "error", "slug": slug})


def _ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"
