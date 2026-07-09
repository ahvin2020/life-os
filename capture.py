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

import requests

from db import now_iso, today_iso
import vault_store

_URL_RE = re.compile(r"https?://\S+", re.I)
_IDEA_DOMAINS = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")

_LEDGER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
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
                parent_id=None) -> int:
    """Insert a task (or subtask when parent_id is set). Returns the new id.
    Single source of truth for task inserts."""
    ts = now_iso()
    if parent_id is not None:
        col = "backlog"  # subtasks ignore column/due
        due_date = planned_on = None
    sort_order = next_sort_order(conn, col, parent_id)
    cur = conn.execute(
        """INSERT INTO tasks
             (title, col, sort_order, priority, category, due_date, planned_on,
              recur_rule, goal_id, parent_id, done, completed_at, archived_at,
              created, updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,?,?)""",
        (title.strip(), col, sort_order, priority, category, due_date, planned_on,
         recur_rule, goal_id, parent_id, ts, ts),
    )
    return cur.lastrowid


def _looks_like_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip())) or any(
        d in text.lower() for d in ("instagram.com", "youtube.com", "youtu.be",
                                    "tiktok.com", "http://", "https://"))


def route_capture(conn, text: str, source: str = "web", forced: str = "auto") -> dict:
    """Classify `text` and file it immediately. Returns a dict describing where it
    went: {kind, id|slug, label, tags?}. `forced` overrides prefix detection when the
    user picks a type chip ('task'|'note'|'journal'); 'auto' respects prefixes/URLs."""
    text = (text or "").strip()
    if not text:
        return {"kind": "none", "label": "nothing to add"}

    body = text

    # --- explicit type chip wins over prefix sniffing ---
    if forced == "task":
        return _as_task(conn, _strip_prefix(body, "t:"))
    if forced == "journal":
        return _as_journal(_strip_prefix(body, "j:"))
    if forced == "note":
        return _as_note(conn, _strip_prefix_any(body), tags=[])

    # --- auto: prefixes, then URL, then unsorted note ---
    low = body.lower()
    if low.startswith("t:"):
        return _as_task(conn, _strip_prefix(body, "t:"))
    if low.startswith("n:"):
        return _as_note(conn, _strip_prefix(body, "n:"), tags=[])
    if low.startswith("i:"):
        return _as_note(conn, _strip_prefix(body, "i:"), tags=["idea"])
    if low.startswith("j:"):
        return _as_journal(_strip_prefix(body, "j:"))
    if _looks_like_url(body):
        tags = ["link"]
        if any(d in low for d in _IDEA_DOMAINS):
            tags.append("idea")
        return _as_note(conn, body, tags=tags, title=_url_title(body))
    # ambiguous → filed now as an unsorted note; triage refiles later (Phase 2)
    return _as_note(conn, body, tags=["unsorted"])


def _strip_prefix(text, prefix):
    return text[len(prefix):].strip() if text.lower().startswith(prefix) else text


def _strip_prefix_any(text):
    for p in ("t:", "n:", "i:", "j:"):
        if text.lower().startswith(p):
            return text[len(p):].strip()
    return text


def _as_task(conn, text) -> dict:
    priority = None
    if text.rstrip().endswith("!"):
        priority = "high"
        text = text.rstrip()[:-1].strip()
    with conn:
        tid = create_task(conn, text or "Untitled task", col="week", priority=priority)
    label = "Tasks" + (" · high" if priority else "")
    return {"kind": "task", "id": tid, "label": label}


def _as_note(conn, text, tags, title=None) -> dict:
    tags = tags or []
    if title is None:
        first = text.strip().splitlines()[0] if text.strip() else "Untitled"
        title = first[:60]
    note = vault_store.create_note(title=title, body=text, tags=tags)
    tag_str = " ".join("#" + t for t in tags) if tags else ""
    label = "Notes" + (f" · {tag_str}" if tag_str else "")
    return {"kind": "note", "slug": note["slug"], "label": label, "tags": tags}


def _as_journal(text) -> dict:
    day = today_iso()
    vault_store.append_journal_entry(day, text, source="")
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
        resp = requests.get(
            url, timeout=3, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LifeOS/1.0)"})
        page = resp.text or ""
        mt = _OG_TITLE_RE.search(page) or _OG_TITLE_RE2.search(page) or _TITLE_RE.search(page)
        title = _html.unescape(re.sub(r"\s+", " ", mt.group(1)).strip()) if mt else ""
        if title:
            return title[:120]
    except Exception:
        pass
    return fallback


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
                          due_date=due_date)
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
