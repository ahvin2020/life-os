"""Link thumbnail resolution + cache for note cards.

Real preview images for saved links, fetched the way WhatsApp/iMessage do it: request
the page as a CRAWLER (facebookexternalhit) and read the og:image tag. Instagram
login-walls a browser user-agent but still serves Open Graph tags to crawlers; YouTube
has a deterministic thumbnail URL; ordinary article links serve og:image to anyone.

Images are downloaded ONCE into a vault/.thumbs/ SIDECAR and served from there — the note
.md files are never touched (CLAUDE.md data-safety: no bulk vault rewrites). A failed or
image-less link is negative-cached so we don't refetch it every page load; the miss is
retried after a week in case the page later gains a preview image.

Stdlib only (urllib) — the runtime stays Flask-only.
"""

from __future__ import annotations

import html
import os
import re
import time
import urllib.request

from domain import vault_store

# The crawler UA is the whole trick: a browser UA gets Instagram's login-walled JS shell
# with no og:image; facebookexternalhit gets the real Open Graph tags (verified 2026-07-10).
_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
_TIMEOUT = 12
_MAX_BYTES = 6_000_000
_MIN_BYTES = 500
_MISS_TTL = 7 * 86400  # retry a missing thumbnail after a week

_YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def thumbs_dir() -> str:
    d = os.path.join(vault_store.VAULT_DIR, ".thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(slug: str) -> str:
    return os.path.join(thumbs_dir(), slug + ".thumb")


def _miss_path(slug: str) -> str:
    return os.path.join(thumbs_dir(), slug + ".miss")


def youtube_id(url: str):
    if not url:
        return None
    m = _YT_RE.search(url)
    return m.group(1) if m else None


def note_kind(note: dict) -> str:
    """video | photo | link | text — drives the card layout and whether a thumb is fetched.

    A note with a `media:` pointer already has a real captured image on disk (photo). A
    YouTube URL is a video; an Instagram /reel is a video, other Instagram is a photo;
    any other URL is a generic link; no URL at all is a prose text note.
    """
    url = note.get("url")
    dom = (note.get("domain") or "").lower()
    if note.get("media"):
        return "photo"
    if not url:
        return "text"
    if youtube_id(url) or "youtu" in dom:
        return "video"
    if "instagram.com" in dom:
        return "video" if "/reel" in url else "photo"
    return "link"


def has_thumb(note: dict) -> bool:
    """True if this note kind can carry an image (so the template asks for one)."""
    return note_kind(note) in ("video", "photo", "link")


def _og_image(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        raw = r.read(2_000_000)  # Open Graph tags live in <head>; cap the read
    text = raw.decode("utf-8", "ignore")
    for pat in (
        r'property=["\']og:image["\']\s+content=["\']([^"\']+)',
        r'name=["\']twitter:image["\']\s+content=["\']([^"\']+)',
        r'property=["\']og:image:url["\']\s+content=["\']([^"\']+)',
    ):
        m = re.search(pat, text)
        if m:
            return html.unescape(m.group(1))
    return None


def _thumb_source(note: dict):
    url = note.get("url")
    vid = youtube_id(url)
    if vid:
        # maxresdefault is HD 16:9 but 404s on some videos; hqdefault always exists.
        return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    if url:
        try:
            return _og_image(url)
        except Exception:
            return None
    return None


def _download(src: str):
    try:
        req = urllib.request.Request(src, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = r.read(_MAX_BYTES + 1)
    except Exception:
        return None
    if not (_MIN_BYTES <= len(data) <= _MAX_BYTES):
        return None
    if not (data[:2] == b"\xff\xd8"                       # jpeg
            or data[:8] == b"\x89PNG\r\n\x1a\n"            # png
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):  # webp
        return None
    return data


def _media_path(note: dict):
    """Absolute path of an already-captured image (media: pointer), if it exists."""
    media = note.get("media")
    if not media:
        return None
    media = media.split(",")[0].strip()             # first image is the card thumbnail
    rel = media.split("vault/", 1)[-1].lstrip("/")  # "vault/.media/x.jpg" -> ".media/x.jpg"
    p = os.path.join(vault_store.VAULT_DIR, rel)
    return p if os.path.exists(p) else None


def resolve(slug: str, note: dict, force: bool = False):
    """Return a filesystem path to slug's thumbnail, fetching + caching on first call.

    None when the note has no obtainable image. Photo captures serve their real image
    directly; link/video notes fetch og:image once and cache it. Negative results are
    remembered for a week so dead/image-less links aren't refetched every request.
    """
    mp = _media_path(note)
    if mp:
        return mp

    cp = _cache_path(slug)
    if os.path.exists(cp):
        return cp

    miss = _miss_path(slug)
    if not force and os.path.exists(miss) and (time.time() - os.path.getmtime(miss)) < _MISS_TTL:
        return None

    src = _thumb_source(note)
    data = _download(src) if src else None
    if not data:
        open(miss, "w").close()  # negative-cache the miss
        return None

    tmp = cp + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, cp)  # atomic publish
    if os.path.exists(miss):
        try:
            os.remove(miss)
        except OSError:
            pass
    return cp


def warm_recent(limit: int = 250) -> int:
    """Best-effort pre-fetch of thumbnails for the most recent notes so the first browse
    is instant instead of fetching on demand. Safe to call repeatedly — resolve() skips
    anything already cached or negative-cached. Meant to run in a background daemon
    thread on startup; returns how many notes it resolved."""
    try:
        notes = vault_store.list_notes()
    except Exception:
        return 0
    done = 0
    for n in notes:
        if done >= limit:
            break
        if note_kind(n) == "text":
            continue
        try:
            resolve(n["slug"], n)
        except Exception:
            pass
        done += 1
    return done


def content_type(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:2] == b"\xff\xd8":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"
