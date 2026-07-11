#!/usr/bin/env python3
"""Import Kelvin's messy ~/Desktop/todo.txt into life-os (tasks / notes / journal).

DRY-RUN CONTRACT
================
    python scripts/import_todo.py            # PREVIEW ONLY → data/import_preview_todo.md
    python scripts/import_todo.py --apply    # actually writes (idempotent via ledger)

Default is a preview: it parses todo.txt, asks `claude -p` (subscription, never a
paid API) to classify each item, and writes a grouped markdown preview for Kelvin
to eyeball. It writes NOTHING into the vault/DB unless --apply is passed.

todo.txt is treated as READ-ONLY input; this script never modifies it.

Pipeline: PARSE (pure, unit-tested) → CLASSIFY (claude -p, batched) →
PREVIEW / APPLY (through capture.create_task + vault_store, idempotent ledger).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from import_common import (  # noqa: E402
    DATA_DIR, LEDGER_PATH, _ORDER, _HEADINGS,
    apply_item, load_ledger, save_ledger, ledger_key,
)
from claude_cli import call_claude  # noqa: E402  (import_common put repo root on sys.path)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODO_PATH = "/Users/kelvintan/Desktop/todo.txt"
PROFILE_PATH = os.path.join(_REPO_ROOT, "vault", "profile.md")
PREVIEW_PATH = os.path.join(DATA_DIR, "import_preview_todo.md")

_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
_URL_PREFIX = ("http://", "https://")


# ── 1. PARSE (pure, no network) ────────────────────────────────────────────────
def is_day_header(stripped: str) -> bool:
    """An UPPERCASE-only line that is exactly a weekday name is a day header
    (context for the items that follow), not an item itself."""
    return bool(stripped) and stripped.isupper() and stripped.lower() in _WEEKDAYS


def _is_url_line(stripped: str) -> bool:
    return stripped.startswith(_URL_PREFIX)


def parse_todo(text: str) -> list[dict]:
    """Turn raw todo.txt content into an ordered list of items.

    Rules (kept deliberately simple + robust for a very messy file):
      - Blank lines separate items.
      - UPPERCASE weekday lines are DAY HEADERS → tracked as day_context, not items.
      - Each non-blank line normally starts its own item (the file is mostly a flat
        dump of one idea/todo per line, incl. '-' and '=' bullets).
      - EXCEPT: a bare-URL line, or an indented line, is treated as a continuation
        and joins the item immediately above it (e.g. a title on one line with its
        reference URL on the next).

    Returns list of {raw, lines:[...], day_context}. `raw` is the verbatim
    (possibly multi-line) source text and is the idempotency key.
    """
    items: list[dict] = []
    current: dict | None = None
    current_day = ""

    def flush():
        nonlocal current
        if current is not None:
            current["raw"] = "\n".join(current["lines"])
            items.append(current)
            current = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            flush()
            continue
        if is_day_header(stripped):
            flush()
            current_day = stripped
            continue
        indented = raw_line[:1] in (" ", "\t")
        if current is not None and (_is_url_line(stripped) or indented):
            current["lines"].append(raw_line.rstrip())  # continuation → join
            continue
        flush()
        current = {"lines": [raw_line.rstrip()], "day_context": current_day}

    flush()
    return items


# ── 2. CLASSIFY (claude -p, batched) ───────────────────────────────────────────
_BATCH = 50  # items per claude -p call (well within a comfortable prompt size)

_CLASSIFY_INSTRUCTIONS = """\
You are triaging Kelvin's messy raw todo.txt dump into his personal "life-os".
Below is his triage profile, then a numbered list of items. Classify EACH item
into exactly ONE destination. This is a YouTuber's brain-dump: most lines are
video ideas, some are personal todos, some are business/sponsor tasks, some are
just links, and some are done/outdated/garbage.

Return ONLY a strict JSON array (no prose, no markdown fences), one object per
item, in the SAME ORDER, each echoing its index as "i". Shapes:
  task:      {"i":N,"type":"task","title":"...","category":"content|business|personal","priority":"high|med|low","due":null}
  note:      {"i":N,"type":"note","title":"...","tags":["..."]}
  journal:   {"i":N,"type":"journal","title":"..."}
  skip:      {"i":N,"type":"skip","reason":"looks-done|outdated|duplicate|garbage"}
  uncertain: {"i":N,"type":"uncertain","note":"why unsure"}

Routing rules:
- A concrete VIDEO IDEA (a topic to make a video/short about) → note, tags ["idea","video"].
- A bare link / a line that is mostly a URL → note, tags ["link"]; add "idea" if it is
  about investing/finance/markets or is a short-form reel (per the profile).
- An actionable personal errand/admin (book, reply, renew, transfer, find, buy) → task,
  category personal (or business if it names a sponsor/Moomoo/Longbridge/IBKR/money owed,
  or content if it's about producing a specific video: script/edit/thumbnail/post/film).
- A reflection/quote/diary-like line → journal.
- Lines that look already-done (contain "DONE", "done", "May done"), outdated, duplicated,
  or are pure noise/garbage → skip with the reason.
- If you genuinely cannot tell → uncertain (goes to Kelvin for a manual call). Prefer a real
  bucket over uncertain when reasonable.

Pick tags from: idea, video, link, research, business, content, personal.
"""


def _build_prompt(profile: str, batch: list[dict], start_index: int) -> str:
    lines = []
    for offset, item in enumerate(batch):
        idx = start_index + offset
        ctx = f" (day: {item['day_context']})" if item.get("day_context") else ""
        raw_one_line = item["raw"].replace("\n", " ⏎ ")
        lines.append(f"[{idx}]{ctx} {raw_one_line}")
    return (
        _CLASSIFY_INSTRUCTIONS
        + "\n=== PROFILE ===\n" + profile.strip()
        + "\n\n=== ITEMS ===\n" + "\n".join(lines)
        + "\n\nReturn the JSON array now."
    )


def _claude_available() -> bool:
    # Probe through the single-source CLI wrapper (call_claude returns "" on any
    # failure, non-empty stdout on success).
    return bool(call_claude("say hi", timeout=60))


def _run_claude(prompt: str) -> str | None:
    # call_claude returns "" on failure; normalise to None for the callers here.
    return call_claude(prompt, timeout=300) or None


def _extract_json_array(text: str):
    """Pull the first JSON array out of a claude response (tolerates fences/prose)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end > start:
            candidate = text[start:end + 1]
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except Exception:
        return None


def classify(items: list[dict], profile: str, *, verbose: bool = True) -> tuple[list[dict], bool]:
    """Return (results, degraded). One classification dict per item, in order.
    Degrades gracefully: if `claude -p` is unavailable or a batch is unparseable,
    those items become {"type":"uncertain", ...} so nothing is dropped/misfiled.
    `degraded` is True when the CLI was unavailable (every item UNCERTAIN)."""
    results: list[dict | None] = [None] * len(items)

    if not _claude_available():
        if verbose:
            print("  ! claude -p unavailable — marking ALL items UNCERTAIN", file=sys.stderr)
        return [{"type": "uncertain", "note": "claude -p unavailable"} for _ in items], True

    for start in range(0, len(items), _BATCH):
        batch = items[start:start + _BATCH]
        if verbose:
            print(f"  classifying items {start}..{start + len(batch) - 1} "
                  f"of {len(items)} via claude -p", file=sys.stderr)
        prompt = _build_prompt(profile, batch, start)
        raw_out = _run_claude(prompt)
        arr = _extract_json_array(raw_out) if raw_out else None
        if not isinstance(arr, list):
            for j in range(len(batch)):
                results[start + j] = {"type": "uncertain", "note": "batch unparseable"}
            continue
        # align by echoed index when possible, else by position
        by_index = {}
        for obj in arr:
            if isinstance(obj, dict) and isinstance(obj.get("i"), int):
                by_index[obj["i"]] = obj
        for j in range(len(batch)):
            idx = start + j
            obj = by_index.get(idx)
            if obj is None and idx - start < len(arr) and isinstance(arr[idx - start], dict):
                obj = arr[idx - start]
            results[start + j] = _normalize(obj)

    return [r or {"type": "uncertain", "note": "no result"} for r in results], False


_VALID_TYPES = {"task", "note", "journal", "skip", "uncertain"}
_VALID_CAT = {"content", "business", "personal"}
_VALID_PRI = {"high", "med", "low"}


def _normalize(obj) -> dict:
    """Coerce a model object into a valid classification dict; junk → uncertain."""
    if not isinstance(obj, dict) or obj.get("type") not in _VALID_TYPES:
        return {"type": "uncertain", "note": "unparseable item"}
    t = obj["type"]
    if t == "task":
        cat = obj.get("category") if obj.get("category") in _VALID_CAT else None
        pri = obj.get("priority") if obj.get("priority") in _VALID_PRI else None
        return {"type": "task", "title": (obj.get("title") or "").strip() or "Untitled task",
                "category": cat, "priority": pri, "due": obj.get("due") or None}
    if t == "note":
        tags = [str(x).lstrip("#") for x in (obj.get("tags") or [])] or ["idea"]
        return {"type": "note", "title": (obj.get("title") or "").strip() or "Untitled",
                "tags": tags}
    if t == "journal":
        return {"type": "journal", "title": (obj.get("title") or "").strip() or "Journal entry"}
    if t == "skip":
        return {"type": "skip", "reason": obj.get("reason") or "garbage"}
    return {"type": "uncertain", "note": obj.get("note") or "unsure"}


# ── 3. PREVIEW ─────────────────────────────────────────────────────────────────
# _ORDER / _HEADINGS are shared via import_common.


def _proposed_str(r: dict) -> str:
    t = r["type"]
    if t == "task":
        bits = [f"category={r.get('category')}", f"priority={r.get('priority')}"]
        if r.get("due"):
            bits.append(f"due={r['due']}")
        return f"TASK — \"{r.get('title')}\" ({', '.join(bits)})"
    if t == "note":
        tags = " ".join("#" + x for x in r.get("tags", []))
        return f"NOTE — \"{r.get('title')}\" [{tags}]"
    if t == "journal":
        return f"JOURNAL — \"{r.get('title')}\""
    if t == "skip":
        return f"SKIP — {r.get('reason')}"
    return f"UNCERTAIN — {r.get('note')}"


def build_preview(items: list[dict], results: list[dict], *, degraded: bool) -> str:
    counts = {k: 0 for k in _ORDER}
    for r in results:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    total = len(items)

    out = ["# Import preview — todo.txt", ""]
    out.append(f"Source: `{TODO_PATH}` (read-only)")
    if degraded:
        out.append("")
        out.append("> **DEGRADED**: `claude -p` was unavailable — every item is "
                   "UNCERTAIN and needs a manual call.")
    out.append("")
    out.append("## Counts")
    out.append(f"- **{counts['task']}** tasks / **{counts['note']}** notes / "
               f"**{counts['journal']}** journal / **{counts['skip']}** skip / "
               f"**{counts['uncertain']}** uncertain / **{total}** total")
    out.append("")
    out.append("This is a DRY-RUN preview. Nothing was written. "
               "Run with `--apply` to import (idempotent).")

    pairs = list(zip(items, results))
    for kind in _ORDER:
        group = [(it, r) for it, r in pairs if r["type"] == kind]
        out.append("")
        out.append(f"## {_HEADINGS[kind]} ({len(group)})")
        if not group:
            out.append("_none_")
            continue
        for it, r in group:
            raw_lines = it["raw"].splitlines() or [""]
            out.append("")
            for i, ln in enumerate(raw_lines):
                prefix = "- " if i == 0 else "  " + "  "
                out.append(f"{prefix}`{ln}`")
            out.append(f"  → {_proposed_str(r)}")
    return "\n".join(out) + "\n"


# ── 4. APPLY ───────────────────────────────────────────────────────────────────
def apply_all(items: list[dict], results: list[dict], *, verbose: bool = True) -> dict:
    from db import connect
    conn = connect()
    ledger = load_ledger()
    stats = {"created": 0, "already": 0, "skipped": 0}
    try:
        for it, r in zip(items, results):
            res = apply_item(conn, it["raw"], r, ledger)
            stats[res["status"]] = stats.get(res["status"], 0) + 1
        save_ledger(ledger)
    finally:
        conn.close()
    if verbose:
        print(f"  applied: {stats}", file=sys.stderr)
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import todo.txt into life-os (dry-run by default).")
    ap.add_argument("--apply", action="store_true",
                    help="actually write items (idempotent). Default = preview only.")
    ap.add_argument("--todo", default=TODO_PATH, help="path to todo.txt (read-only)")
    args = ap.parse_args(argv)

    with open(args.todo, encoding="utf-8") as f:
        text = f.read()
    with open(PROFILE_PATH, encoding="utf-8") as f:
        profile = f.read()

    items = parse_todo(text)
    print(f"parsed {len(items)} items from {args.todo}", file=sys.stderr)

    results, degraded = classify(items, profile)

    os.makedirs(DATA_DIR, exist_ok=True)
    preview = build_preview(items, results, degraded=degraded)
    with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
        f.write(preview)
    print(f"wrote preview → {PREVIEW_PATH}", file=sys.stderr)

    if args.apply:
        print("--apply: writing through create_task / vault_store …", file=sys.stderr)
        apply_all(items, results)
    else:
        print("dry-run: nothing written (pass --apply to import)", file=sys.stderr)


if __name__ == "__main__":
    main()
