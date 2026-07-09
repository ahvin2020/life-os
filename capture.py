"""Quick-capture routing — the ONE place that turns a raw line of text into a
task, note, or journal entry.

Both the web composer (POST /capture) and the future Telegram daemon call
route_capture(); factoring it here (per the build spec) guarantees the phone bot
and the web twin file things identically. create_task() is likewise the single
source of truth for inserting a task, shared by route_capture and routes_tasks.
"""

from __future__ import annotations

import re

from db import now_iso, today_iso
import vault_store

_URL_RE = re.compile(r"https?://\S+", re.I)
_IDEA_DOMAINS = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")


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


def _url_title(text):
    m = _URL_RE.search(text)
    url = m.group(0) if m else text
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return host or "link"
