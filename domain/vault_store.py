"""Markdown vault storage for notes and journal pages.

Notes and journal pages are NOT in the DB — they are plain markdown files under
vault/, so they round-trip to disk, sync via Synology Drive, and open in Obsidian.
This module is the single source of truth for reading/writing them.

  vault/notes/<slug>.md          note with YAML frontmatter (title, tags, created, pinned)
  vault/journal/YYYY-MM-DD.md    one free-form page per day; timestamped entries
  vault/.trash/                  soft-deleted notes (kept 30 days)

Frontmatter is a deliberately tiny YAML subset (title, tags list, created, pinned)
parsed/emitted by hand so the only runtime dependency stays Flask.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import urlparse

from core.db import get_tz, now_sg

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Override the whole vault location for tests via LIFEOS_VAULT_DIR.
VAULT_DIR = os.environ.get("LIFEOS_VAULT_DIR") or os.path.join(_ROOT, "vault")
# profile.md is the distilled triage/routing context every `claude -p` surface injects.
# It always lives in the REAL repo vault (not the LIFEOS_VAULT_DIR test override).
PROFILE_PATH = os.path.join(_ROOT, "vault", "profile.md")


def read_profile() -> str:
    """Contents of vault/profile.md, or '' if absent — the single source for the
    profile injected into router/capture/proactive/library claude prompts."""
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


_LEARNED_HEADER = "# Learned rules"


def append_learned_rule(line: str, cap: int = 15) -> bool:
    """Append ONE imperative routing rule under a fenced '# Learned rules' section in
    profile.md (created at EOF if absent). Deduped, capped at `cap` bullets — returns
    False if full or the line is empty/duplicate, True on write. Only touches that
    section; the rest of profile.md is preserved verbatim."""
    line = " ".join((line or "").split()).strip().lstrip("-").strip()
    if not line:
        return False
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        content = ""
    lines = content.splitlines()
    # locate the Learned-rules section and its bullet lines
    hdr = next((i for i, l in enumerate(lines) if l.strip() == _LEARNED_HEADER), None)
    if hdr is None:
        block = ([""] if content and not content.endswith("\n\n") else []) + [_LEARNED_HEADER]
        lines += block
        hdr = len(lines) - 1
    end = len(lines)
    for j in range(hdr + 1, len(lines)):
        if lines[j].startswith("# "):
            end = j
            break
    bullets = [lines[j].strip()[2:].strip() for j in range(hdr + 1, end)
               if lines[j].strip().startswith("- ")]
    if any(line.lower() == b.lower() for b in bullets):
        return False                          # already have this rule
    if len(bullets) >= cap:
        return False                          # section full — prune before adding
    insert_at = end
    while insert_at > hdr + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1                        # keep the bullet flush under the section
    lines.insert(insert_at, f"- {line}")
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return True


_CONTACTS_HEADER = "# Contacts"
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _norm_label(label: str) -> str:
    """Lowercase alnum tokens of a contact label, for match/dedup ('Wife Jane Tan')."""
    return " ".join(re.findall(r"[a-z0-9]+", (label or "").lower()))


def upsert_contact(label: str, emails, cap: int = 40) -> bool:
    """Add or update ONE contact under a '# Contacts' section in profile.md (created at EOF
    if absent) — durable people→email(s) the assistant can use for draft_email `to` and
    create_event `guests`. Injected into every prompt (it's part of profile.md), so a saved
    contact is always in context. Merges: a matching line (same normalized label OR a shared
    email) gains the new address instead of duplicating; a new person appends a bullet. Only
    touches that section. False on no valid email or when the section is full (`cap`)."""
    clean = []
    for e in ([emails] if isinstance(emails, str) else (emails or [])):
        m = _EMAIL_RE.search(str(e))
        if m and m.group(0).lower() not in [c.lower() for c in clean]:
            clean.append(m.group(0))
    label = " ".join((label or "").split()).strip().rstrip(":").strip()
    if not clean or not label:
        return False
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        content = ""
    lines = content.splitlines()
    hdr = next((i for i, l in enumerate(lines) if l.strip() == _CONTACTS_HEADER), None)
    if hdr is None:
        lines += ([""] if content and not content.endswith("\n\n") else []) + [_CONTACTS_HEADER]
        hdr = len(lines) - 1
    end = len(lines)
    for j in range(hdr + 1, len(lines)):
        if lines[j].startswith("# "):
            end = j
            break
    nlabel = _norm_label(label)
    # find a matching existing bullet: same normalized label, or an overlapping email
    match = None
    for j in range(hdr + 1, end):
        s = lines[j].strip()
        if not s.startswith("- ") or ":" not in s:
            continue
        lbl, rest = s[2:].split(":", 1)
        existing_emails = _EMAIL_RE.findall(rest)
        if _norm_label(lbl) == nlabel or (set(e.lower() for e in existing_emails)
                                          & set(e.lower() for e in clean)):
            merged, seen = [], set()
            for e in existing_emails + clean:                  # keep order, dedup
                if e.lower() not in seen:
                    seen.add(e.lower())
                    merged.append(e)
            best_label = label if len(label) >= len(lbl.strip()) else lbl.strip()
            lines[j] = f"- {best_label}: {', '.join(merged)}"
            match = j
            break
    if match is None:
        bullets = sum(1 for j in range(hdr + 1, end)
                      if lines[j].strip().startswith("- "))
        if bullets >= cap:
            return False
        insert_at = end
        while insert_at > hdr + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"- {label}: {', '.join(clean)}")
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return True


_IDENTITY_HEADER = "# Identity"


def set_identity(text: str) -> bool:
    """Write (or replace) a fenced '# Identity' section at the TOP of profile.md — who
    Sam is + family, so the assistant can tell 'my passport' from a relative's. Only
    that section is touched; the rest of profile.md is preserved. False on empty input."""
    block = (text or "").strip()
    if not block:
        return False
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        content = ""
    lines = content.splitlines()
    section = [_IDENTITY_HEADER, block, ""]
    hdr = next((i for i, l in enumerate(lines) if l.strip() == _IDENTITY_HEADER), None)
    if hdr is None:
        new = section + (["", *lines] if lines else [])
    else:
        end = len(lines)
        for j in range(hdr + 1, len(lines)):
            if lines[j].startswith("# "):
                end = j
                break
        new = lines[:hdr] + section + lines[end:]
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(new).rstrip() + "\n")
    return True


def identity_names() -> tuple:
    """Parse the '# Identity' section → (own_tokens, family_tokens) as lowercase name-word
    sets. Own = the names on the line marked '(me)' (or a 'Name:' line); family = names on
    the other identity lines. Used to tell 'my passport' from a relative's when picking a
    document to send. ((), ()) if there's no identity section."""
    text = read_profile()
    lines = text.splitlines()
    hdr = next((i for i, l in enumerate(lines) if l.strip() == _IDENTITY_HEADER), None)
    if hdr is None:
        return set(), set()
    own, family = set(), set()
    for l in lines[hdr + 1:]:
        if l.startswith("# "):
            break
        if ":" not in l:
            continue
        key, val = l.split(":", 1)
        is_self = "(me)" in val.lower() or key.strip().lower() == "name"
        toks = {w for w in re.findall(r"[a-z0-9]+", val.lower().replace("(me)", ""))
                if len(w) >= 2}
        (own if is_self else family).update(toks)
    return own, family


def owner_display_name() -> str:
    """The owner's own name from the '# Identity' block (the '(me)' line, or a 'Name:' line),
    stripped of the '(me)' marker — for greetings. '' when there's no identity yet, so a fresh/
    unconfigured profile greets namelessly. Fully per-user (reads profile.md); nothing hardcoded —
    whoever runs the app is greeted by the name in THEIR profile."""
    text = read_profile()
    lines = text.splitlines()
    hdr = next((i for i, l in enumerate(lines) if l.strip() == _IDENTITY_HEADER), None)
    if hdr is None:
        return ""
    for l in lines[hdr + 1:]:
        if l.startswith("# "):
            break
        if ":" not in l:
            continue
        key, val = l.split(":", 1)
        if "(me)" in val.lower() or key.strip().lower() == "name":
            return re.sub(r"\(me\)", "", val, flags=re.I).strip().strip(",").strip()
    return ""


def profile_is_unconfigured() -> bool:
    """True when the profile has no real identity yet (missing file, empty, or the untouched
    starter with no '# Identity' section) — the signal for a first-run onboarding nudge. Once
    derive_identity / set_identity writes a name, this flips to False."""
    return not identity_names()[0]


def notes_dir() -> str:
    d = os.path.join(VAULT_DIR, "notes")
    os.makedirs(d, exist_ok=True)
    return d


def journal_dir() -> str:
    d = os.path.join(VAULT_DIR, "journal")
    os.makedirs(d, exist_ok=True)
    return d


def trash_dir() -> str:
    d = os.path.join(VAULT_DIR, ".trash")
    os.makedirs(d, exist_ok=True)
    return d


def audio_dir() -> str:
    d = os.path.join(VAULT_DIR, ".audio")
    os.makedirs(d, exist_ok=True)
    return d


def media_dir() -> str:
    d = os.path.join(VAULT_DIR, ".media")
    os.makedirs(d, exist_ok=True)
    return d


_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
# Stored basenames embed the ORIGINAL filename after this marker so a document tile can
# show "invoice.pdf" instead of a timestamp. Older pointers have no marker and just show
# their basename (images don't display a name anyway).
_NAME_SEP = "__"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def media_is_image(name: str) -> bool:
    """True if `name` (a pointer or basename) is an accepted image type — governs whether a
    tile renders a thumbnail or a file card, and whether the modal previews inline."""
    return os.path.splitext(media_display_name(name).lower())[1] in _IMG_EXTS


def _safe_original(name: str) -> str:
    """Sanitise an uploaded filename to a traversal-safe basename kept for display."""
    base = os.path.basename(name or "").strip()
    stem, ext = os.path.splitext(base)
    stem = _SAFE_NAME_RE.sub("-", stem).strip("-.") or "file"
    ext = _SAFE_NAME_RE.sub("", ext)
    return (stem[:50] + ext[:10])


def media_display_name(name: str) -> str:
    """The human filename for a pointer/basename: the part after `__` if present, else the
    bare basename. Traversal-safe (basename only)."""
    base = os.path.basename(name or "")
    return base.split(_NAME_SEP, 1)[1] if _NAME_SEP in base else base


def new_media_basename(original: str) -> str:
    """A unique, traversal-safe stored basename that embeds the original filename after
    `__` (so a document tile shows 'invoice.pdf'). Shared by the web upload and the
    Telegram document flow so both produce the same pointer shape."""
    orig = _safe_original(original) or "file"
    return now_sg().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4) + _NAME_SEP + orig


def save_media_file(storage) -> str | None:
    """Save an uploaded file (any type — a Werkzeug FileStorage) into vault/.media/ and
    return its `vault/.media/<name>` pointer. The stored basename embeds the original
    filename after `__` so documents keep a readable name. None if there's no filename."""
    orig = getattr(storage, "filename", "") or ""
    if not _safe_original(orig):
        return None
    base = new_media_basename(orig)
    storage.save(os.path.join(media_dir(), base))
    return "vault/.media/" + base


def media_file_path(name: str):
    """Absolute path of a media file by basename (path-traversal-guarded). None if absent."""
    p = os.path.join(media_dir(), os.path.basename(name or ""))
    return p if os.path.exists(p) else None


def media_items(media_str: str) -> list:
    """Split a comma-separated media value (frontmatter or tasks.media) into display items
    [{name, url, is_image}] — name is the original filename, url the /media/<basename>
    serve route, is_image governs thumbnail-vs-file-tile rendering."""
    out = []
    for p in (media_str or "").split(","):
        p = p.strip()
        if p:
            base = os.path.basename(p)
            out.append({"name": media_display_name(base), "url": "/media/" + base,
                        "is_image": media_is_image(base)})
    return out


# ── slug + frontmatter ────────────────────────────────────────────────────────
def slugify(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (title or "").lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s or "note"


def _unique_slug(base: str) -> str:
    base = slugify(base)
    d = notes_dir()
    slug = base
    i = 2
    while os.path.exists(os.path.join(d, slug + ".md")):
        slug = f"{base}-{i}"
        i += 1
    return slug


def _emit_frontmatter(title, tags, created, pinned, audio=None, media=None, archived=False) -> str:
    tags_str = "[" + ", ".join(tags) + "]"
    out = (
        "---\n"
        f"title: {title}\n"
        f"tags: {tags_str}\n"
        f"created: {created}\n"
        f"pinned: {'true' if pinned else 'false'}\n"
    )
    if archived:
        out += "archived: true\n"    # note consumed/cleared (hidden from the main grid)
    if audio:
        out += f"audio: {audio}\n"   # pointer to the original voice recording
    if media:
        out += f"media: {media}\n"   # pointer to the source image (photo capture)
    return out + "---\n"


def _parse_frontmatter(text: str):
    """Return (meta_dict, body). Tolerates a missing frontmatter block."""
    meta = {"title": "", "tags": [], "created": "", "pinned": False, "audio": "", "media": "", "archived": False}
    if text.startswith("---"):
        parts = text.split("\n")
        # find closing ---
        end = None
        for i in range(1, len(parts)):
            if parts[i].strip() == "---":
                end = i
                break
        if end is not None:
            for line in parts[1:end]:
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if key == "tags":
                    val = val.strip("[]")
                    meta["tags"] = [t.strip().lstrip("#") for t in val.split(",") if t.strip()]
                elif key in ("pinned", "archived"):
                    meta[key] = val.lower() in ("true", "1", "yes")
                elif key in ("title", "created", "audio", "media"):
                    meta[key] = val
            body = "\n".join(parts[end + 1:]).lstrip("\n")
            return meta, body
    return meta, text


# ── notes ─────────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s<>\")]+")


def first_url(body: str):
    """The first http(s) URL in a text, or None. Trailing sentence punctuation
    (`).,;'"`) is trimmed so the same link tokenizes identically everywhere — this is
    the ONE first_url in the codebase (capture re-exports it) so URL dedupe can't drift."""
    m = _URL_RE.search(body or "")
    return m.group(0).rstrip(").,;'\"") if m else None


def _domain_of(body: str):
    url = first_url(body)
    if not url:
        return None
    try:
        host = urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return None


def _note_from_path(path: str) -> dict:
    slug = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    meta, body = _parse_frontmatter(raw)
    # snippet is a content PREVIEW: raw URLs ate the card's 3-line clamp with
    # querystring garbage and duplicated the domain chip (the clickable link)
    snippet = re.sub(r"\s+", " ", re.sub(r"https?://\S+", " ", body)).strip()[:220]
    mtime = datetime.fromtimestamp(os.path.getmtime(path), get_tz())
    return {
        "slug": slug,
        "title": meta["title"] or slug,
        "tags": meta["tags"],
        "created": meta["created"],
        "pinned": meta["pinned"],
        "archived": meta.get("archived") or False,
        "audio": meta.get("audio") or "",
        "media": meta.get("media") or "",
        "media_items": media_items(meta.get("media") or ""),
        "body": body,
        "snippet": snippet,
        "domain": _domain_of(body),
        "url": first_url(body),
        "updated": mtime.date().isoformat(),
        "updated_ts": os.path.getmtime(path),
    }


def list_notes() -> list:
    d = notes_dir()
    out = []
    for name in os.listdir(d):
        if name.endswith(".md"):
            try:
                out.append(_note_from_path(os.path.join(d, name)))
            except Exception:
                continue
    # "Recent" = strictly newest-CREATED first (stable tiebreak by slug). Sorting by
    # file mtime broke this: a bulk retitle refreshed `updated` on old imported notes
    # and pushed them above genuinely new captures. `created` is a fixed +08:00 ISO
    # string, so lexical order == chronological order.
    out.sort(key=lambda n: (n["created"] or "", n["slug"]), reverse=True)
    return out


def read_note(slug: str):
    path = os.path.join(notes_dir(), slug + ".md")
    if not os.path.exists(path):
        return None
    return _note_from_path(path)


def write_note(slug, title, tags, body, pinned, created=None, audio=None, media=None,
               archived=None) -> dict:
    path = os.path.join(notes_dir(), slug + ".md")
    if created is None or audio is None or media is None or archived is None:
        existing = read_note(slug)
        if created is None:
            created = existing["created"] if existing else now_sg().isoformat(timespec="seconds")
        if audio is None:                       # preserve an existing audio pointer
            audio = existing["audio"] if existing else None
        if media is None:                       # preserve an existing media pointer
            media = existing["media"] if existing else None
        if archived is None:                    # preserve archived state
            archived = existing["archived"] if existing else False
    content = _emit_frontmatter(title, tags, created, pinned, audio, media, archived) + (body or "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return _note_from_path(path)


def create_note(title, body="", tags=None, pinned=False, audio=None, media=None) -> dict:
    tags = tags or []
    title = (title or "Untitled").strip()
    slug = _unique_slug(title)
    created = now_sg().isoformat(timespec="seconds")
    return write_note(slug, title, tags, body, pinned, created, audio, media, archived=False)


def set_archived(slug: str, archived: bool):
    """Toggle a note's archived state (single-note write). Archived notes drop out of the
    main grid + Spaces into the Archived shelf — the 'cleared it' half of the Shuffle."""
    n = read_note(slug)
    if not n:
        return None
    return write_note(slug, n["title"], n["tags"], n["body"], n["pinned"],
                      n["created"], n["audio"] or None, n["media"] or None, archived=archived)


def notes_on_this_day(today_iso: str) -> list:
    """Notes captured 1 month / 6 months / 1 year ago today — anniversary resurfacing.
    Returns [{span, note}] newest span first. `today_iso` is YYYY-MM-DD in app tz."""
    try:
        y, m, d = (int(x) for x in today_iso.split("-"))
    except Exception:
        return []

    def shift(years=0, months=0):
        yy, mm = y - years, m - months
        while mm <= 0:
            mm += 12
            yy -= 1
        return yy, mm

    wanted = [("1 month ago", shift(months=1)),
              ("6 months ago", shift(months=6)),
              ("1 year ago", shift(years=1))]
    by_ym = {}
    for n in list_notes():
        c = n.get("created") or ""
        if len(c) >= 10:
            try:
                cy, cm, cd = int(c[:4]), int(c[5:7]), int(c[8:10])
            except ValueError:
                continue
            if cd == d:
                by_ym.setdefault((cy, cm), n)  # first (newest) per year-month
    out = []
    for span, ym in wanted:
        if ym in by_ym:
            out.append({"span": span, "note": by_ym[ym]})
    return out


def _rewrite_tags_line(slug: str, mutate) -> bool:
    """Rewrite ONLY the frontmatter `tags:` line of a note, leaving title/created/pinned/
    audio/media and the body BYTE-IDENTICAL. `mutate(current_tags)` returns the new tag
    list, or None for a no-op. Returns True iff the file was modified. Kept surgical so it
    can never disturb note order (Notes sorts by `created`) or other fields."""
    path = os.path.join(notes_dir(), slug + ".md")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return False
    for i in range(1, end):
        key, sep, val = lines[i].partition(":")
        if sep and key.strip() == "tags":
            current = [t.strip().lstrip("#") for t in val.strip().strip("[]").split(",") if t.strip()]
            new = mutate(current)
            if new is None:
                return False
            lines[i] = "tags: [" + ", ".join(new) + "]"
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))        # every other byte preserved verbatim
            return True
    return False                                 # no tags line in frontmatter


def add_tag(slug: str, tag: str) -> bool:
    """Append ONE tag to a note's frontmatter `tags:` list. Idempotent — a tag already
    present (or a missing file / no frontmatter) is a no-op returning False. Used by the
    one-time clustering script."""
    tag = (tag or "").strip().lstrip("#")
    if not tag:
        return False
    return _rewrite_tags_line(slug, lambda cur: None if tag in cur else cur + [tag])


def remove_tag(slug: str, tag: str) -> bool:
    """Inverse of add_tag — drop ONE tag from the frontmatter `tags:` list. No-op (False)
    if the tag isn't present. Used by cluster_undo.py."""
    tag = (tag or "").strip().lstrip("#")
    if not tag:
        return False
    return _rewrite_tags_line(slug, lambda cur: None if tag not in cur else [t for t in cur if t != tag])


def touch_note(slug: str):
    """Re-save a note unchanged (bumps file mtime) — used when a re-shared URL maps to
    an existing note, so the capture registers a 'touch' instead of minting a twin."""
    n = read_note(slug)
    if not n:
        return None
    return write_note(slug, n["title"], n["tags"], n["body"], n["pinned"],
                      n["created"], n["audio"] or None, n["media"] or None)


def delete_note(slug: str) -> bool:
    """Soft-delete: move the file into vault/.trash/ (restorable for 30 days)."""
    src = os.path.join(notes_dir(), slug + ".md")
    if not os.path.exists(src):
        return False
    stamp = now_sg().strftime("%Y%m%d%H%M%S")
    dst = os.path.join(trash_dir(), f"{slug}.{stamp}.md")
    os.rename(src, dst)
    return True


def restore_note(slug: str) -> bool:
    """Restore the most recently trashed copy of slug back into notes/."""
    d = trash_dir()
    matches = sorted(
        [n for n in os.listdir(d) if n.startswith(slug + ".") and n.endswith(".md")],
        reverse=True,
    )
    if not matches:
        return False
    src = os.path.join(d, matches[0])
    dst = os.path.join(notes_dir(), slug + ".md")
    os.rename(src, dst)
    return True


# ── attachment garbage collection ─────────────────────────────────────────────
def _attachment_basenames(*pointers) -> set:
    """Basenames of the .media/.audio files referenced by a set of comma-separated
    frontmatter pointers (`vault/.media/a.jpg,b.jpg` → {'a.jpg', 'b.jpg'})."""
    out = set()
    for p in pointers:
        for tok in (p or "").split(","):
            tok = tok.strip()
            if tok:
                out.add(os.path.basename(tok))
    return out


def _referenced_attachments(conn, undo_cutoff: datetime) -> set:
    """Every attachment basename still pointed at by something restorable: live notes,
    every journal entry, any task row still in the DB (a soft-deleted task can be undone),
    and notes trashed within the undo window (their `.md` is still restorable)."""
    ref = set()
    # Read notes DIRECTLY (not via list_notes, which swallows per-file errors): a file we
    # can't read — a Synology mid-sync partial write, a transient lock — must RAISE so the
    # caller aborts the GC, never treat it as "no attachments" and orphan a live original.
    ndir = notes_dir()
    for name in os.listdir(ndir):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(ndir, name), encoding="utf-8") as f:
            meta, _ = _parse_frontmatter(f.read())
        ref |= _attachment_basenames(meta.get("audio"), meta.get("media"))
    for d in list_journal_days():
        page = read_journal(d["day"])
        if not page:
            continue
        for e in page["entries"]:
            ref |= _attachment_basenames(e.get("audio"), e.get("media"))
    try:
        for row in conn.execute("SELECT media FROM tasks WHERE media IS NOT NULL AND media != ''"):
            ref |= _attachment_basenames(row[0])
    except sqlite3.Error:
        pass
    # notes still inside their trash-undo window keep their attachments alive
    tdir = trash_dir()
    for name in os.listdir(tdir):
        if not name.endswith(".md"):
            continue
        m = re.search(r"\.(\d{14})\.md$", name)
        if m:                                    # deletion stamp in the filename
            try:
                stamp = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=get_tz())
                if stamp < undo_cutoff:
                    continue                     # window lapsed → no longer a reference
            except ValueError:
                pass
        with open(os.path.join(tdir, name), encoding="utf-8") as f:   # raises → GC aborts
            meta, _ = _parse_frontmatter(f.read())
        ref |= _attachment_basenames(meta.get("audio"), meta.get("media"))
    return ref


def purge_orphan_attachments(conn, days: int = 30) -> int:
    """Delete .media/.audio files nothing points at anymore, once past the `days` undo
    window. A file is removed only when it is referenced by no live note, journal entry,
    task, or still-restorable trashed note AND its own mtime is older than `days` — so a
    just-captured attachment survives even if its journal entry was deleted (that entry's
    Undo can still restore the pointer). Undo-safe and idempotent; also reclaims orphans
    left by past deletions (which never cleaned up their files). Returns the count removed."""
    undo_cutoff = now_sg() - timedelta(days=days)
    try:
        referenced = _referenced_attachments(conn, undo_cutoff)
    except Exception:
        # A vault file couldn't be read (mid-sync, lock, encoding glitch) → the reference
        # set is incomplete. Skip this run entirely rather than risk deleting an attachment
        # whose owner we simply failed to scan. GC is non-critical; a later run cleans up.
        return 0
    age_cutoff = undo_cutoff.timestamp()
    removed = 0
    for folder in (media_dir(), audio_dir()):
        for name in os.listdir(folder):
            if name in referenced:
                continue
            path = os.path.join(folder, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < age_cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    return removed


# ── journal ───────────────────────────────────────────────────────────────────
def journal_path(day: str) -> str:
    return os.path.join(journal_dir(), f"{day}.md")


def read_journal(day: str):
    """Return {day, entries:[{time, source, text}], raw} or None if no page yet."""
    path = journal_path(day)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    return {"day": day, "entries": _parse_journal_entries(raw), "raw": raw}


_ENTRY_RE = re.compile(r"^##\s+(\d{1,2}:\d{2})(?:\s*·\s*(.+))?\s*$")


def _parse_journal_entries(raw: str) -> list:
    entries = []
    cur = None
    for line in raw.splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            if cur:
                cur["text"] = cur["text"].strip()
                entries.append(cur)
            # The header slot carries ` · `-separated tokens: a plain source label, a
            # voice recording (`audio:…`), and/or attached images (`media:a.jpg,b.jpg`).
            # Split them back into their own fields so the label stays clean and the page
            # can offer playback / a photo gallery.
            src = audio = media = ""
            for tok in (m.group(2) or "").split("·"):
                tok = tok.strip()
                if not tok:
                    continue
                if tok.startswith("audio:"):
                    audio = tok[len("audio:"):].strip()
                elif tok.startswith("media:"):
                    media = tok[len("media:"):].strip()
                else:
                    src = tok
            cur = {"time": m.group(1), "source": src, "text": "", "audio": audio,
                   "media": media, "media_items": media_items(media)}
        elif line.startswith("# "):
            continue  # page title header
        elif cur is not None:
            cur["text"] += line + "\n"
    if cur:
        cur["text"] = cur["text"].strip()
        entries.append(cur)
    return entries


def append_journal_entry(day: str, text: str, source: str = "", audio: str | None = None,
                         media: str | None = None) -> dict:
    """Append a timestamped '## HH:MM' entry to the day's page, creating it if needed.
    A voice recording rides in the header slot as `audio:<pointer>` and attached images
    as `media:<a,b>` (both parsed back out by _parse_journal_entries)."""
    path = journal_path(day)
    now = now_sg()
    hhmm = now.strftime("%H:%M")
    tokens = []
    if source:
        tokens.append(source)
    if audio:
        tokens.append("audio:" + audio)
    if media:
        tokens.append("media:" + media)
    slot = " · ".join(tokens)
    header = f"## {hhmm}" + (f" · {slot}" if slot else "")
    block = f"\n{header}\n{text.strip()}\n"
    if not os.path.exists(path):
        # Friendly page title, e.g. "# Thursday 9 July 2026"
        try:
            d = datetime.strptime(day, "%Y-%m-%d")
            title = d.strftime("# %A %-d %B %Y")
        except Exception:
            title = f"# {day}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(title + "\n" + block)
    else:
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
    return read_journal(day)


def save_journal_raw(day: str, raw: str) -> dict:
    path = journal_path(day)
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    return read_journal(day)


def _entry_spans(raw: str):
    """Line-level spans of every '## HH:MM' section, so we can rewrite ONE entry while
    leaving every other byte untouched. Returns (lines[keepends], spans) where each
    span = {time, head (header line idx), end (exclusive line idx of the section)}."""
    lines = raw.splitlines(keepends=True)
    heads = []
    for i, line in enumerate(lines):
        if _ENTRY_RE.match(line.rstrip("\n")):
            heads.append((i, _ENTRY_RE.match(line.rstrip("\n")).group(1)))
    spans = []
    for j, (i, t) in enumerate(heads):
        end = heads[j + 1][0] if j + 1 < len(heads) else len(lines)
        spans.append({"time": t, "head": i, "end": end})
    return lines, spans


def _locate_entry(spans, time: str, occurrence: int):
    """The span for the `occurrence`-th (0-based) section whose header time == `time`.
    Disambiguates duplicate HH:MM headings within a day. None if out of range."""
    matches = [s for s in spans if s["time"] == time]
    if 0 <= occurrence < len(matches):
        return matches[occurrence]
    return None


def edit_journal_entry(day: str, time: str, occurrence: int, new_text: str):
    """Rewrite the body of ONE '## HH:MM' entry in place, preserving its header and
    every other section byte-for-byte. Returns the refreshed page, or None if the
    day/entry doesn't exist."""
    page = read_journal(day)
    if not page:
        return None
    lines, spans = _entry_spans(page["raw"])
    span = _locate_entry(spans, time, occurrence)
    if span is None:
        return None
    header = lines[span["head"]]
    if not header.endswith("\n"):
        header += "\n"
    body = (new_text or "").strip()
    new_block = [header] + ([body + "\n"] if body else [])
    new_lines = lines[:span["head"]] + new_block + lines[span["end"]:]
    return save_journal_raw(day, "".join(new_lines))


def delete_journal_entry(day: str, time: str, occurrence: int):
    """Remove ONE '## HH:MM' section (header + body) entirely, preserving all others
    byte-for-byte. Returns the refreshed page, or None if not found."""
    page = read_journal(day)
    if not page:
        return None
    lines, spans = _entry_spans(page["raw"])
    span = _locate_entry(spans, time, occurrence)
    if span is None:
        return None
    new_lines = lines[:span["head"]] + lines[span["end"]:]
    return save_journal_raw(day, "".join(new_lines))


def list_journal_days() -> list:
    """Newest-first list of {day, preview} for every journal page on disk."""
    d = journal_dir()
    days = []
    for name in os.listdir(d):
        if re.match(r"\d{4}-\d{2}-\d{2}\.md$", name):
            day = name[:-3]
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    raw = f.read()
            except Exception:
                continue
            # preview = first non-header, non-blank line
            preview = ""
            for line in raw.splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    preview = s[:120]
                    break
            days.append({"day": day, "preview": preview})
    days.sort(key=lambda x: x["day"], reverse=True)
    return days
