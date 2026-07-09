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
from datetime import datetime
from urllib.parse import urlparse

from db import TZ, now_sg

_ROOT = os.path.dirname(os.path.abspath(__file__))
# Override the whole vault location for tests via LIFEOS_VAULT_DIR.
VAULT_DIR = os.environ.get("LIFEOS_VAULT_DIR") or os.path.join(_ROOT, "vault")


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


def _emit_frontmatter(title, tags, created, pinned, audio=None, media=None) -> str:
    tags_str = "[" + ", ".join(tags) + "]"
    out = (
        "---\n"
        f"title: {title}\n"
        f"tags: {tags_str}\n"
        f"created: {created}\n"
        f"pinned: {'true' if pinned else 'false'}\n"
    )
    if audio:
        out += f"audio: {audio}\n"   # pointer to the original voice recording
    if media:
        out += f"media: {media}\n"   # pointer to the source image (photo capture)
    return out + "---\n"


def _parse_frontmatter(text: str):
    """Return (meta_dict, body). Tolerates a missing frontmatter block."""
    meta = {"title": "", "tags": [], "created": "", "pinned": False, "audio": "", "media": ""}
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
                elif key == "pinned":
                    meta["pinned"] = val.lower() in ("true", "1", "yes")
                elif key in ("title", "created", "audio", "media"):
                    meta[key] = val
            body = "\n".join(parts[end + 1:]).lstrip("\n")
            return meta, body
    return meta, text


# ── notes ─────────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s<>\")]+")


def first_url(body: str):
    """The first http(s) URL in a note body, or None."""
    m = _URL_RE.search(body or "")
    return m.group(0) if m else None


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
    snippet = re.sub(r"\s+", " ", body).strip()[:220]
    mtime = datetime.fromtimestamp(os.path.getmtime(path), TZ)
    return {
        "slug": slug,
        "title": meta["title"] or slug,
        "tags": meta["tags"],
        "created": meta["created"],
        "pinned": meta["pinned"],
        "audio": meta.get("audio") or "",
        "media": meta.get("media") or "",
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


def write_note(slug, title, tags, body, pinned, created=None, audio=None, media=None) -> dict:
    path = os.path.join(notes_dir(), slug + ".md")
    if created is None or audio is None or media is None:
        existing = read_note(slug)
        if created is None:
            created = existing["created"] if existing else now_sg().isoformat(timespec="seconds")
        if audio is None:                       # preserve an existing audio pointer
            audio = existing["audio"] if existing else None
        if media is None:                       # preserve an existing media pointer
            media = existing["media"] if existing else None
    content = _emit_frontmatter(title, tags, created, pinned, audio, media) + (body or "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return _note_from_path(path)


def create_note(title, body="", tags=None, pinned=False, audio=None, media=None) -> dict:
    tags = tags or []
    title = (title or "Untitled").strip()
    slug = _unique_slug(title)
    created = now_sg().isoformat(timespec="seconds")
    return write_note(slug, title, tags, body, pinned, created, audio, media)


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
            cur = {"time": m.group(1), "source": (m.group(2) or "").strip(), "text": ""}
        elif line.startswith("# "):
            continue  # page title header
        elif cur is not None:
            cur["text"] += line + "\n"
    if cur:
        cur["text"] = cur["text"].strip()
        entries.append(cur)
    return entries


def append_journal_entry(day: str, text: str, source: str = "") -> dict:
    """Append a timestamped '## HH:MM' entry to the day's page, creating it if needed."""
    path = journal_path(day)
    now = now_sg()
    hhmm = now.strftime("%H:%M")
    header = f"## {hhmm}" + (f" · {source}" if source else "")
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
