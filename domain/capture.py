"""Quick-capture routing — the ONE place that turns a raw line of text into a
task, note, or journal entry.

Both the web composer (POST /capture) and the future Telegram daemon call
route_capture(); factoring it here (per the build spec) guarantees the phone bot
and the web twin file things identically. create_task() is likewise the single
source of truth for inserting a task, shared by route_capture and routes_tasks.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, quote

import requests

from core.db import now_iso, today_iso
from domain import vault_store
from domain.vault_store import first_url  # single-source URL extractor (re-exported here)

_URL_RE = re.compile(r"https?://\S+", re.I)
_IDEA_DOMAINS = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")

# Query params that identify a share/campaign, not the content — dropped when
# normalising a URL so a re-shared link maps to the SAME note (no twin).
_STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "igsh", "igshid", "fbclid", "gclid", "si", "feature", "ref", "ref_src",
}

_LEDGER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "import_ledger.json")


def imported_task_ids() -> set:
    """Task ids created by the bulk-import tool (data/import_ledger.json). Tasks carry
    no #imported tag, so the ledger is the ONLY record that a task was backfilled —
    used to keep imports out of the 'captured today' feed and counts."""
    try:
        with open(_LEDGER_PATH, encoding="utf-8") as f:
            ledger = json.load(f)
    except Exception:
        return set()
    return {rec.get("id") for rec in ledger.values()
            if isinstance(rec, dict) and rec.get("destination") == "task"
            and rec.get("id") is not None}


def next_sort_order(conn, col: str, parent_id=None) -> int:
    """Bottom of the column (max + 1) so a new card lands predictably."""
    if parent_id is not None:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM tasks WHERE parent_id = ?",
            (parent_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM tasks "
            "WHERE col = ? AND parent_id IS NULL",
            (col,),
        ).fetchone()
    return int(row["m"]) + 1


def create_task(conn, title, *, col="backlog", priority=None, category=None,
                due_date=None, planned_on=None, recur_rule=None, goal_id=None,
                parent_id=None, at_top=False, media=None) -> int:
    """Insert a task (or subtask when parent_id is set). Returns the new id.
    Single source of truth for task inserts. at_top=True surfaces the new task at
    the TOP of its column — used by the capture paths (bot / composer / refile),
    where "I just sent this" should be the first thing seen, not buried at the
    bottom of a long column. Board/editor creation keeps bottom placement."""
    ts = now_iso()
    if parent_id is not None:
        col = "backlog"  # subtasks ignore column/due
        due_date = planned_on = None
    if at_top and parent_id is None:
        row = conn.execute(
            "SELECT COALESCE(MIN(sort_order), 1) AS m FROM tasks "
            "WHERE col = ? AND parent_id IS NULL", (col,)).fetchone()
        sort_order = int(row["m"]) - 1
    else:
        sort_order = next_sort_order(conn, col, parent_id)
    # week_since = the "This week" staleness clock, stamped on entering the column.
    week_since = today_iso() if col == "week" else None
    cur = conn.execute(
        """INSERT INTO tasks
             (title, col, sort_order, priority, category, due_date, planned_on,
              recur_rule, goal_id, parent_id, done, completed_at, archived_at,
              week_since, media, created, updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,?,?,?,?)""",
        (title.strip(), col, sort_order, priority, category, due_date, planned_on,
         recur_rule, goal_id, parent_id, week_since, media, ts, ts),
    )
    return cur.lastrowid


def _looks_like_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip())) or any(
        d in text.lower() for d in ("instagram.com", "youtube.com", "youtu.be",
                                    "tiktok.com", "http://", "https://"))


# ── multi-item messages ─────────────────────────────────────────────────────────
# One Telegram message can hold several captures, one per line ("paste 3 links"). A
# whole message is otherwise treated as ONE note, so line 2+ used to vanish into the
# first note's body. We split ONLY when EVERY non-empty line is independently a
# capturable unit (a URL or a prefixed item); a link-with-caption or a prose note with
# line breaks has non-unit lines, so it stays intact.
_SHORT_PREFIXES = ("t:", "n:", "i:", "j:")
# Natural prefixes the phone keyboard auto-capitalises / spells out, mapped to the
# canonical short form route_capture understands (so a split line needs no claude).
_NATURAL_PREFIX = {
    "task:": "t:", "todo:": "t:", "to-do:": "t:", "to do:": "t:",
    "note:": "n:", "idea:": "i:", "journal:": "j:", "diary:": "j:",
}


def _is_capture_unit(line: str) -> bool:
    """True if `line` on its own is a distinct capture (URL or prefixed item)."""
    low = line.strip().lower()
    if not low:
        return False
    if _looks_like_url(line):
        return True
    if low[:2] in _SHORT_PREFIXES:
        return True
    return any(low.startswith(p) for p in _NATURAL_PREFIX)


def normalize_capture_line(line: str) -> str:
    """Rewrite a natural prefix ('Note: x') to the canonical short form ('n: x') so the
    deterministic route_capture handles it; URLs and short prefixes pass through."""
    line = line.strip()
    low = line.lower()
    for nat, short in _NATURAL_PREFIX.items():
        if low.startswith(nat):
            return short + " " + line[len(nat):].strip()
    return line


def split_capture_lines(text: str):
    """Split a multi-item message into its per-line captures, or None when it's a single
    capture (fewer than 2 lines, or any line isn't a standalone unit — e.g. a link with a
    caption, or a multi-line prose note). Returned lines are normalized for route_capture."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2 or not all(_is_capture_unit(ln) for ln in lines):
        return None
    return [normalize_capture_line(ln) for ln in lines]


def route_capture(conn, text: str, source: str = "web", forced: str = "auto",
                  enrich: str = "async", media: str | None = None) -> dict:
    """Classify `text` and file it immediately. Returns a dict describing where it
    went: {kind, id|slug, label, title?, tags?}. `forced` overrides prefix detection when
    the user picks a type chip ('task'|'note'|'journal'); 'auto' respects prefixes/URLs.
    `enrich` controls link enrichment: 'async' (default → web/voice) schedules the
    background rewrite; 'off' skips it so a caller (the Telegram bot) can enrich inline
    and edit its own reply. `media` is an optional comma-separated list of vault/.media
    pointers to attach to whatever the capture becomes; a lone attachment (no text) files
    as a note."""
    text = (text or "").strip()
    media = (media or "").strip() or None
    if not text and not media:
        return {"kind": "none", "label": "nothing to add"}

    body = text

    # --- explicit type chip wins over prefix sniffing ---
    if forced == "task":
        return _as_task(conn, _strip_prefix(body, "t:"), media=media)
    if forced == "journal":
        return _as_journal(_strip_prefix(body, "j:"), media=media)
    if forced == "note":
        return _as_note(conn, _strip_prefix_any(body), tags=[], media=media)

    # --- attachment with no text → a note (its filename becomes the title) ---
    if not body:
        return _as_note(conn, "", tags=[], media=media)

    # --- auto: prefixes, then URL, then unsorted note ---
    low = body.lower()
    if low.startswith("t:"):
        return _as_task(conn, _strip_prefix(body, "t:"), media=media)
    if low.startswith("n:"):
        return _as_note(conn, _strip_prefix(body, "n:"), tags=[], media=media)
    if low.startswith("i:"):
        return _as_note(conn, _strip_prefix(body, "i:"), tags=["idea"], media=media)
    if low.startswith("j:"):
        return _as_journal(_strip_prefix(body, "j:"), media=media)
    if media:
        # free-text + a file: keep them together as one note rather than guessing a task
        return _as_note(conn, body, tags=[], media=media)
    if _looks_like_url(body):
        # Dedupe by normalised URL: a re-shared link touches the existing #link note
        # instead of minting a twin (strip utm/igsh/etc first).
        url = first_url(body)
        existing = find_link_note_by_url(url) if url else None
        if existing:
            vault_store.touch_note(existing["slug"])
            if enrich == "async":
                schedule_enrichment(existing["slug"])
            tag_str = " ".join("#" + t for t in existing["tags"]) if existing["tags"] else ""
            return {"kind": "note", "slug": existing["slug"],
                    "label": "Notes" + (f" · {tag_str}" if tag_str else ""),
                    "title": existing["title"], "tags": existing["tags"], "deduped": True}
        tags = ["link"]
        if any(d in low for d in _IDEA_DOMAINS):
            tags.append("idea")
        res = _as_note(conn, body, tags=tags, title=_url_title(body))
        if enrich == "async":
            schedule_enrichment(res["slug"])   # async: capture stays instant
        return res
    # ambiguous → filed now as an unsorted note; triage refiles later (Phase 2)
    return _as_note(conn, body, tags=["unsorted"])


def _strip_prefix(text, prefix):
    return text[len(prefix):].strip() if text.lower().startswith(prefix) else text


def _strip_prefix_any(text):
    for p in ("t:", "n:", "i:", "j:"):
        if text.lower().startswith(p):
            return text[len(p):].strip()
    return text


def _as_task(conn, text, media=None) -> dict:
    priority = None
    if text.rstrip().endswith("!"):
        priority = "high"
        text = text.rstrip()[:-1].strip()
    title = text or "Untitled task"
    with conn:
        tid = create_task(conn, title, col="week", priority=priority,
                          at_top=True, media=media)
    label = "Tasks" + (" · high" if priority else "")
    return {"kind": "task", "id": tid, "label": label, "title": title,
            "priority": priority}


def _as_note(conn, text, tags, title=None, media=None) -> dict:
    tags = tags or []
    if title is None:
        first = text.strip().splitlines()[0] if text.strip() else ""
        # an attachment-only capture is titled by its (first) filename
        if not first and media:
            first = vault_store.media_display_name(media.split(",")[0])
        title = first[:60] or "Attachment"
    note = vault_store.create_note(title=title, body=text, tags=tags, media=media)
    tag_str = " ".join("#" + t for t in tags) if tags else ""
    label = "Notes" + (f" · {tag_str}" if tag_str else "")
    return {"kind": "note", "slug": note["slug"], "label": label,
            "title": note["title"], "tags": tags}


def _as_journal(text, media=None) -> dict:
    day = today_iso()
    vault_store.append_journal_entry(day, text, source="", media=media)
    return {"kind": "journal", "id": day, "label": "today's Journal"}


_OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', re.I)
_OG_TITLE_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:title["\']', re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _url_title(text):
    """Title for a URL capture: the page's og:title (or <title>), fetched with a 3s
    timeout. Falls back to '<domain> link' so a note is never titled a bare domain."""
    m = _URL_RE.search(text)
    url = m.group(0) if m else text
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].strip()
    fallback = f"{host} link" if host else "link"
    if not m:
        return fallback
    try:
        # reuse the single og:/<title> scraper (same 3s timeout) — no duplicate fetch/parse
        title = (_fetch_og(url, 3).get("title") or "").strip()
        if title:
            return title[:120]
    except Exception:
        pass
    return fallback


# ── link enrichment ────────────────────────────────────────────────────────────
# When a URL is captured the raw note is just the link ("instagram.com" / "Add to
# note https://…"). Enrichment fetches page metadata best-effort, then makes ONE
# claude -p call combining that metadata + the user's accompanying words + profile.md
# to produce {title, summary, tags}, and rewrites the note. It runs ASYNC after the
# instant save so capture stays instant; any network/claude failure leaves the note
# untouched (same never-block-capture contract as triage).
_ADD_TO_NOTE_RE = re.compile(r"^\s*add to note\s*", re.I)


def normalize_url(url: str) -> str:
    """Canonical key for dedupe: lowercase host, drop 'www.', strip tracking params
    (utm_*/igsh/fbclid/…), drop the fragment, and trim a trailing slash. Two shares of
    the same link (with different igsh=/utm= tails) collapse to one key."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url.strip().lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
         if k.lower() not in _STRIP_PARAMS]
    query = urlencode(sorted(q))
    path = p.path.rstrip("/")
    # scheme collapsed to https so http/https of the same resource share one key
    return urlunparse(("https", host, path, "", query, ""))


def find_link_note_by_url(url: str):
    """The existing #link note whose first URL normalises to the same key as `url`
    (earliest-created wins), or None. Powers going-forward URL dedupe."""
    if not url:
        return None
    key = normalize_url(url)
    matches = []
    for n in vault_store.list_notes():
        if "link" not in (n["tags"] or []):
            continue
        nu = n.get("url") or first_url(n["body"])
        if nu and normalize_url(nu) == key:
            matches.append(n)
    if not matches:
        return None
    matches.sort(key=lambda n: (n["created"] or "", n["slug"]))
    return matches[0]


_OG_DESC_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']', re.I)
_OG_DESC_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']og:description["\']', re.I)


def fetch_link_metadata(url: str, timeout: int = 5) -> dict:
    """Best-effort page metadata: {title, author, description, site}. YouTube uses the
    unauthenticated oEmbed endpoint; everything else parses og:/<title>. Instagram &
    TikTok are attempted the same way but usually block — we degrade to {} silently.
    Never raises."""
    if not url:
        return {}
    low = url.lower()
    try:
        if "youtube.com" in low or "youtu.be" in low:
            return _fetch_youtube_oembed(url, timeout)
        return _fetch_og(url, timeout)
    except Exception:
        return {}


def _fetch_youtube_oembed(url: str, timeout: int) -> dict:
    api = "https://www.youtube.com/oembed?url=" + quote(url, safe="") + "&format=json"
    resp = requests.get(api, timeout=timeout,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; LifeOS/1.0)"})
    data = resp.json()
    return {
        "title": (data.get("title") or "").strip(),
        "author": (data.get("author_name") or "").strip(),
        "description": "",
        "site": "youtube",
    }


def _fetch_og(url: str, timeout: int) -> dict:
    resp = requests.get(url, timeout=timeout, allow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; LifeOS/1.0)"})
    page = resp.text or ""
    mt = _OG_TITLE_RE.search(page) or _OG_TITLE_RE2.search(page) or _TITLE_RE.search(page)
    md = _OG_DESC_RE.search(page) or _OG_DESC_RE2.search(page)
    title = _html.unescape(re.sub(r"\s+", " ", mt.group(1)).strip()) if mt else ""
    desc = _html.unescape(re.sub(r"\s+", " ", md.group(1)).strip()) if md else ""
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return {"title": title[:200], "author": "", "description": desc[:500], "site": host}


def _user_words(body: str, url: str) -> str:
    """The user's own accompanying text (their 'reason'): the note body minus the URL
    and the 'Add to note' capture boilerplate."""
    text = (body or "")
    if url:
        text = text.replace(url, " ")
    text = _URL_RE.sub(" ", text)          # drop any other URLs too
    text = _ADD_TO_NOTE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _enrich_prompt(url: str, meta: dict, words: str, profile: str) -> str:
    meta_lines = "\n".join(f"{k}: {v}" for k, v in meta.items() if v) or "(none available)"
    return (
        "You enrich a saved link for a personal notes app. Using the fetched page "
        "metadata, the user's own words (their REASON for saving it), and their "
        "profile, produce STRICT JSON: "
        '{"title": "...", "summary": "one line, why this matters to THEM", '
        '"tags": ["lowercase", "no-hash"]}. '
        "Title: concise, human (never a bare domain). Summary: <=140 chars, specific. "
        "Tags: 2-4, reuse the profile's vocabulary. Respond with ONLY the JSON.\n\n"
        f"=== URL ===\n{url}\n\n"
        f"=== FETCHED METADATA ===\n{meta_lines}\n\n"
        f"=== USER'S WORDS (their reason) ===\n{words or '(none)'}\n\n"
        f"=== vault/profile.md ===\n{profile}\n"
    )


def _parse_enrichment(raw: str) -> dict | None:
    from ai.claude_cli import extract_json
    data = extract_json(raw, "object")
    if data is None:
        return None
    title = (data.get("title") or "").strip()
    summary = (data.get("summary") or "").strip()
    tags = [str(t).lstrip("#").strip().lower() for t in (data.get("tags") or []) if str(t).strip()]
    if not title and not summary:
        return None
    return {"title": title, "summary": summary, "tags": tags}


def _default_claude(prompt: str) -> str:
    from ai.claude_cli import call_claude
    return call_claude(prompt, timeout=45)


def _enrich(slug: str, *, fetch_fn=None, claude_fn=None) -> tuple | None:
    """Shared enrichment work: fetch page metadata, make ONE claude call, and rewrite the
    note title/tags/body = URL + summary + user's words + fetched description. Returns
    (saved_note, data) where data={title, summary, tags}, or None if there's no URL /
    claude fails (note left untouched — the never-block-capture contract)."""
    note = vault_store.read_note(slug)
    if not note:
        return None
    url = first_url(note["body"])
    if not url:
        return None
    meta = (fetch_fn or fetch_link_metadata)(url) or {}
    words = _user_words(note["body"], url)
    prompt = _enrich_prompt(url, meta, words, vault_store.read_profile())
    try:
        raw = (claude_fn or _default_claude)(prompt)
    except Exception:
        raw = ""
    data = _parse_enrichment(raw)
    if not data:
        return None   # never-block-capture: leave the raw note exactly as it was
    title = data["title"] or note["title"]
    tags = list(dict.fromkeys((note["tags"] or []) + data["tags"]))
    parts = [url]
    if data["summary"]:
        parts.append(data["summary"])
    if words:
        parts.append(words)
    if meta.get("description") and meta["description"] not in (words, data["summary"]):
        parts.append(meta["description"])
    body = "\n\n".join(parts)
    saved = vault_store.write_note(slug, title, tags, body, note["pinned"], note["created"])
    return saved, data


def enrich_note(slug: str, *, fetch_fn=None, claude_fn=None) -> dict | None:
    """Enrich ONE captured link note in place. Returns the saved note, or None if there's
    no URL / claude fails (note untouched). Used by the async web/voice path."""
    res = _enrich(slug, fetch_fn=fetch_fn, claude_fn=claude_fn)
    return res[0] if res else None


def enrich_link(slug: str, *, fetch_fn=None, claude_fn=None) -> tuple:
    """Telegram sibling of enrich_note: returns (saved_note, summary) so the bot reply can
    surface the one-line 'why it matters'. (None, "") when enrichment didn't run."""
    res = _enrich(slug, fetch_fn=fetch_fn, claude_fn=claude_fn)
    if not res:
        return None, ""
    saved, data = res
    return saved, (data.get("summary") or "")


def _enrich_enabled() -> bool:
    """Async enrichment is on unless LIFEOS_ENRICH_LINKS=0 (the test suite sets 0 so
    captures never spawn a background claude call)."""
    return os.environ.get("LIFEOS_ENRICH_LINKS", "1") != "0"


def schedule_enrichment(slug: str) -> None:
    """Kick enrich_note in a daemon thread so the capture returns instantly. No-op when
    disabled. All errors are swallowed — enrichment must never break a capture."""
    if not slug or not _enrich_enabled():
        return

    def _run():
        try:
            enrich_note(slug)
        except Exception:
            pass

    threading.Thread(target=_run, name=f"enrich-{slug}", daemon=True).start()


# ── refiling helpers (shared by the Change button + Claude triage) ─────────────
# These are the ONE place that moves an item note↔task or retags it, so the web
# refile endpoint and the triage runner never duplicate the mutation logic.
def list_unsorted_notes() -> list:
    """Notes still tagged #unsorted (what triage looks at)."""
    return [n for n in vault_store.list_notes() if "unsorted" in (n["tags"] or [])]


def retag_note(slug: str, tags: list) -> dict | None:
    """Replace a note's tag set (drops #unsorted when real tags are supplied)."""
    note = vault_store.read_note(slug)
    if not note:
        return None
    tags = [t.lstrip("#") for t in (tags or [])]
    saved = vault_store.write_note(slug, note["title"], tags, note["body"],
                                   note["pinned"], note["created"])
    tag_str = " ".join("#" + t for t in tags) if tags else ""
    return {"kind": "note", "slug": slug,
            "label": "Notes" + (f" · {tag_str}" if tag_str else ""), "tags": tags}


def convert_note_to_task(conn, slug: str, *, title=None, category=None,
                         priority=None, due_date=None) -> dict | None:
    """Turn a captured note into a task (triage 'to_task' + the Change button).
    Creates the task via create_task(), then soft-deletes the source note."""
    note = vault_store.read_note(slug)
    if not note:
        return None
    with conn:
        tid = create_task(conn, title or note["title"] or "Untitled task",
                          col="week", priority=priority, category=category,
                          due_date=due_date, at_top=True)
    vault_store.delete_note(slug)
    bits = [b for b in ("high" if priority == "high" else None, category,
                        ("due " + due_date) if due_date else None) if b]
    label = "Tasks" + (" · " + " · ".join(bits) if bits else "")
    return {"kind": "task", "id": tid, "label": label, "from_slug": slug}


def convert_note_to_journal(slug: str) -> dict | None:
    """Move a captured note into today's journal page (triage 'to_journal' + Change)."""
    note = vault_store.read_note(slug)
    if not note:
        return None
    vault_store.append_journal_entry(today_iso(), note["body"] or note["title"], source="")
    vault_store.delete_note(slug)
    return {"kind": "journal", "id": today_iso(), "label": "today's Journal",
            "from_slug": slug}


def convert_task_to_journal(conn, task_id: int) -> dict | None:
    """Move a task into today's journal page, then delete the task row."""
    r = conn.execute("SELECT * FROM tasks WHERE id=? AND parent_id IS NULL",
                     (task_id,)).fetchone()
    if not r:
        return None
    vault_store.append_journal_entry(today_iso(), r["title"], source="")
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"kind": "journal", "id": today_iso(), "label": "today's Journal",
            "from_task": task_id}


def convert_task_to_note(conn, task_id: int, tags=None) -> dict | None:
    """Turn a task back into a note (the Change button, misfiled task→note).
    Reads the task, writes a note, then deletes the task row."""
    r = conn.execute("SELECT * FROM tasks WHERE id=? AND parent_id IS NULL",
                     (task_id,)).fetchone()
    if not r:
        return None
    tags = [t.lstrip("#") for t in (tags or ["unsorted"])]
    note = vault_store.create_note(title=r["title"], body=r["title"], tags=tags)
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    tag_str = " ".join("#" + t for t in tags) if tags else ""
    return {"kind": "note", "slug": note["slug"],
            "label": "Notes" + (f" · {tag_str}" if tag_str else ""),
            "tags": tags, "from_task": task_id}
