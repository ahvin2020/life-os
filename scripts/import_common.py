"""Shared plumbing for the one-off import scripts (import_todo.py, import_sheet.py).

DRY-RUN CONTRACT
================
Both import scripts default to a *preview* (no writes). Only `--apply` mutates
anything, and even then every write goes through the existing single-source
helpers (capture.create_task, vault_store.create_note / append_journal_entry) —
these scripts never touch the DB schema or write markdown by hand.

IDEMPOTENCY
===========
Every applied item is keyed by a hash of its EXACT raw source text (plus an
optional `role` suffix, so one source row that fans out into both a task AND a
companion note gets two independent ledger keys). The key is looked up in
data/import_ledger.json BEFORE anything is created; if present, the item is
skipped. This makes re-running `--apply` safe: nothing is duplicated.

UNDO
====
- Notes / journal: tagged/marked so they are findable (#imported / source=imported).
- Tasks: the tasks table has no tag column, so imported tasks are NOT mutated —
  they carry a normal title. Undo for tasks = read the ledger (destination=="task")
  and delete those ids. The ledger is the record of what this tool created.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

# Make the repo root importable so the product helpers resolve when these scripts
# are run directly from scripts/ or imported by the test-suite.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from db import now_iso, today_iso  # noqa: E402
import vault_store  # noqa: E402
from capture import create_task  # noqa: E402

DATA_DIR = os.path.join(_REPO_ROOT, "data")
LEDGER_PATH = os.path.join(DATA_DIR, "import_ledger.json")

# The single tag stamped on every imported note so a bulk import is reversible /
# reviewable in the Notes view.
IMPORTED_TAG = "imported"

# Preview grouping order + section headings, shared by import_todo / import_sheet.
_ORDER = ["task", "note", "journal", "skip", "uncertain"]
_HEADINGS = {"task": "Tasks", "note": "Notes", "journal": "Journal",
             "skip": "Skip", "uncertain": "Uncertain"}


# ── ledger ────────────────────────────────────────────────────────────────────
def ledger_key(raw: str, role: str = "") -> str:
    """Stable idempotency key for a raw source string.

    `role` disambiguates a single source row that produces more than one
    destination (e.g. a video row → task + companion note)."""
    digest = hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:16]
    return f"{digest}:{role}" if role else digest


def load_ledger(path: str = LEDGER_PATH) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_ledger(ledger: dict, path: str = LEDGER_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)


# ── apply one classified item ─────────────────────────────────────────────────
def apply_item(conn, raw: str, result: dict, ledger: dict, *, role: str = "") -> dict:
    """Write a single classified item through the product helpers, idempotently.

    `result` is the normalized classification dict:
      {"type": "task",     "title", "category", "priority", "due"}
      {"type": "note",     "title", "tags":[...], "body"}
      {"type": "journal",  "title", "body"}
      {"type": "skip",     "reason"}
      {"type": "uncertain","note"}

    Returns {"status": "created"|"already"|"skipped", "destination", ...}.
    The ledger check happens BEFORE any create, so re-runs never duplicate — this
    matters for notes/journal because create_note always mints a fresh slug.
    """
    key = ledger_key(raw, role)
    if key in ledger:
        rec = ledger[key]
        return {"status": "already", "destination": rec.get("destination"), "key": key}

    t = (result or {}).get("type")

    if t == "task":
        with conn:
            tid = create_task(
                conn,
                result.get("title") or "Untitled task",
                col="week",
                priority=result.get("priority"),
                category=result.get("category"),
                due_date=result.get("due"),
            )
        ledger[key] = {"destination": "task", "id": tid, "imported_at": now_iso()}
        return {"status": "created", "destination": "task", "id": tid, "key": key}

    if t == "note":
        tags = list(dict.fromkeys([*(result.get("tags") or []), IMPORTED_TAG]))
        body = result.get("body") if result.get("body") is not None else raw
        # Going-forward URL dedupe: a re-shared #link URL touches the existing note
        # instead of minting a twin (normalised, so utm/igsh tails don't fool it).
        from capture import find_link_note_by_url, first_url
        url = first_url(body)
        if "link" in tags and url:
            dup = find_link_note_by_url(url)
            if dup:
                vault_store.touch_note(dup["slug"])
                ledger[key] = {"destination": "note", "slug": dup["slug"],
                               "imported_at": now_iso(), "deduped": True}
                return {"status": "already", "destination": "note",
                        "slug": dup["slug"], "key": key, "deduped": True}
        note = vault_store.create_note(
            title=result.get("title") or "Untitled",
            body=body,
            tags=tags,
        )
        ledger[key] = {"destination": "note", "slug": note["slug"], "imported_at": now_iso()}
        return {"status": "created", "destination": "note", "slug": note["slug"], "key": key}

    if t == "journal":
        text = result.get("body") or result.get("title") or raw
        vault_store.append_journal_entry(today_iso(), text, source="imported")
        ledger[key] = {"destination": "journal", "day": today_iso(), "imported_at": now_iso()}
        return {"status": "created", "destination": "journal", "day": today_iso(), "key": key}

    # skip / uncertain / anything unknown → not written
    return {"status": "skipped", "destination": t, "key": key}
