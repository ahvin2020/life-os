"""Notes: markdown vault gallery, tag filter chips, live search,
editor with autosave and soft-delete. Notes live as files under vault/notes/."""

from __future__ import annotations

from flask import Blueprint, render_template, request, jsonify, send_file, abort

from web_core import db, respond, is_ajax
import vault_store
import thumbs

bp = Blueprint("notes", __name__)

# Purpose Spaces: the 715 captures serve several jobs at once, so route them into a few
# tag-derived shelves instead of one flat feed (a note can appear in more than one). Keys
# are stable slugs used as the client-side filter value; the bulk idea/imported/link/ig
# tags are deliberately NOT space members — they're noise, not shelves.
SPACES = [
    ("videos", "For videos", ["content", "creator-craft", "video"]),
    ("investing", "Investing & learning",
     ["market-investing", "cpf-epf-retirement", "ai-investing-tools", "brokers-platforms",
      "options-trading", "banks-cards", "property-housing", "research", "sg-my-money-culture",
      "business"]),
    ("inspiration", "Inspiration", ["inspiration", "life-misc"]),
]
_SPACE_TAGS = {key: set(tags) for key, _label, tags in SPACES}


def _note_spaces(tags) -> list:
    """Which space keys a note belongs to, by tag intersection."""
    ts = set(tags)
    return [key for key, tagset in _SPACE_TAGS.items() if ts & tagset]


@bp.route("/notes")
def notes_page():
    import db
    show_archived = request.args.get("archived") == "1"
    all_notes = vault_store.list_notes()
    for n in all_notes:                   # enrich for the card template
        n["kind"] = thumbs.note_kind(n)
        n["spaces"] = _note_spaces(n["tags"])
    live = [n for n in all_notes if not n["archived"]]
    archived_ct = sum(1 for n in all_notes if n["archived"])
    shown = [n for n in all_notes if n["archived"]] if show_archived else live
    recent = shown
    space_counts = [(key, label, sum(1 for n in live if key in n["spaces"]))
                    for key, label, _tags in SPACES]
    flashbacks = vault_store.notes_on_this_day(db.today_iso())
    for f in flashbacks:
        f["note"]["kind"] = thumbs.note_kind(f["note"])
    return render_template("notes.html", recent=recent,
                           chips=_tag_chips(live), spaces=space_counts,
                           flashbacks=flashbacks, archived_ct=archived_ct,
                           show_archived=show_archived,
                           count=len(live), active="notes")


@bp.route("/notes/<slug>/archive", methods=["POST"])
def note_archive(slug):
    """Toggle archived. Body param `archived` = 1|0 (default: archive)."""
    val = request.form.get("archived", "1") in ("1", "true", "on")
    saved = vault_store.set_archived(slug, val)
    if not saved:
        return jsonify({"status": "error", "message": "not found"}), 404
    return jsonify({"status": "ok", "archived": saved["archived"]})


@bp.route("/notes/shuffle")
def notes_shuffle():
    """Serendipity: a peaceful pass over older un-archived notes, one at a time, to keep
    or archive. Deterministic 'random' (no Math.random dependency): oldest-touched first,
    so it naturally resurfaces things you haven't looked at in a while."""
    live = [n for n in vault_store.list_notes() if not n["archived"]]
    for n in live:
        n["kind"] = thumbs.note_kind(n)
    live.sort(key=lambda n: n["updated_ts"])   # least-recently-touched first
    deck = live[:20]
    return render_template("shuffle.html", deck=deck, total=len(live), active="notes")


@bp.route("/notes/thumb/<slug>")
def note_thumb(slug):
    """Serve a note's cached preview image, fetching it on first request. Lazy-loaded by
    the card grid; cached in vault/.thumbs/ so repeat views are instant. 404 (handled by
    the <img> onerror -> glyph fallback) when the link has no obtainable image."""
    note = vault_store.read_note(slug)
    if not note:
        abort(404)
    path = thumbs.resolve(slug, note)
    if not path:
        abort(404)
    resp = send_file(path, mimetype=thumbs.content_type(path))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/notes/<slug>/audio")
def note_audio(slug):
    """Serve a voice note's original recording so it can be played back in the note
    editor. The `audio:` frontmatter pointer is trusted only for its basename (no path
    traversal); the file always lives in vault/.audio/. 404 when the note has no audio."""
    import os
    note = vault_store.read_note(slug)
    if not note or not note.get("audio"):
        abort(404)
    path = os.path.join(vault_store.audio_dir(), os.path.basename(note["audio"]))
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype="audio/ogg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


def _tag_chips(notes) -> list:
    """Filter chips as (tag, count), most-used first. A tag on >80% of notes matches
    almost everything — a filter that filters nothing — so it's dropped (idea/imported/
    link/ig all qualify), leaving the topic tags as the browsable shelves."""
    from collections import Counter
    total = len(notes)
    counts = Counter(t for n in notes for t in n["tags"])
    threshold = total * 0.8
    chips = [(t, c) for t, c in counts.items() if c <= threshold]
    chips.sort(key=lambda tc: (-tc[1], tc[0]))
    return chips


@bp.route("/notes/new", methods=["POST"])
def note_new():
    f = request.form
    title = (f.get("title") or "Untitled").strip()
    body = f.get("body") or ""
    tags = [t.strip().lstrip("#") for t in (f.get("tags") or "").split(",") if t.strip()]
    note = vault_store.create_note(title=title, body=body, tags=tags)
    if is_ajax():
        return jsonify({"status": "ok", "slug": note["slug"], "note": note})
    return respond(True, "Note created", to="/notes")


@bp.route("/notes/ask", methods=["POST"])
def notes_ask():
    """Semantic library question over the saved idea notes (the same engine the bot
    uses). READ-ONLY. Renders the ranked 3–5 picks as REAL note cards (same `note_card`
    macro as the grid, so Ask results look identical to browsing), each carrying Claude's
    one-line `why`. `fallback` flags the deterministic recency answer used when Claude
    is unavailable."""
    import library
    q = (request.form.get("q") or "").strip()
    if not q:
        return jsonify({"status": "ok", "q": q, "html": "", "count": 0, "fallback": False})
    results, fallback = library.rank_notes(q)
    by_slug = {n["slug"]: n for n in vault_store.list_notes()}
    cards = []
    for r in results:                     # rehydrate full note dicts for the card macro
        n = by_slug.get(r["slug"])
        if not n:
            continue
        n = dict(n)
        n["kind"] = thumbs.note_kind(n)
        n["spaces"] = _note_spaces(n["tags"])
        n["why"] = r["why"]
        cards.append(n)
    html = render_template("_ask_cards.html", cards=cards) if cards else ""
    return jsonify({"status": "ok", "q": q, "html": html, "count": len(cards),
                    "fallback": fallback})


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
    saved = vault_store.write_note(slug, title, tags, body, note["pinned"], note["created"])
    return jsonify({"status": "ok", "note": saved})


@bp.route("/notes/<slug>/delete", methods=["POST"])
def note_delete(slug):
    ok = vault_store.delete_note(slug)
    if is_ajax():
        return jsonify({"status": "ok" if ok else "error", "slug": slug})
    return respond(ok, "Note deleted" if ok else "Not found", to="/notes")


@bp.route("/notes/<slug>/restore", methods=["POST"])
def note_restore(slug):
    ok = vault_store.restore_note(slug)
    return jsonify({"status": "ok" if ok else "error", "slug": slug})
