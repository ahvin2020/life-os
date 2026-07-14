#!/usr/bin/env python3
"""Import Sam's YouTube-projects Google Sheet into life-os.

DRY-RUN CONTRACT
================
    python scripts/import_sheet.py           # PREVIEW ONLY → data/import_preview_sheet.md
    python scripts/import_sheet.py --apply    # actually writes (idempotent via ledger)

The sheet is now PUBLIC ("anyone with the link"), so we fetch each tab as CSV via
its export URL using only the stdlib (urllib) — no google-api dependency, no auth.
The NETWORK fetch is a thin separate function; all row→destination logic is pure
and unit-tested with fixture rows (no network in tests).

Tabs have DIFFERENT shapes; each tab's shape is detected from its content:
  - "ideas"   : a video-ideas list  [title | need-by | script | files | notes | type]
                → each titled row becomes a note #video #idea (links go in the body).
  - "tracker" : an in-flight editor tracker [title | status | gdoc | editor+fee]
                with LONG/SHORT VIDEO + "image-2" (done-awaiting-release) sections
                → in-flight row = task(content) + companion note #video #wip;
                  done-awaiting-release row = note #video #release-queue (NO task —
                  release scheduling lives in youtube-assistant);
                  blank-status title-only row = note #video #idea.
  - "unknown" : finance/analytics tabs (payment models, commission calcs) → skipped.

Nothing is written unless --apply is passed.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from import_common import (  # noqa: E402
    DATA_DIR, _ORDER, _HEADINGS, apply_item, load_ledger, save_ledger,
)

SHEET_ID = "1TjH4atuJYUgLjw22h_H1XK02v5l9zccgvwp9ysOb7fM"
PRIMARY_GID = "299819378"
PREVIEW_PATH = os.path.join(DATA_DIR, "import_preview_sheet.md")

# Section labels that flip the tracker into "done, awaiting release" mode.
_DONE_SECTION_RE = re.compile(r"image-?2|done|await|release|posted|uploaded", re.I)
_SECTION_LABELS_RE = re.compile(r"^(long video|short video|image-?2|video|shorts|done|todo)$", re.I)
_STATUS_WORDS = ("editing", "reviewing", "review", "not passed", "scripting",
                 "recording", "filming", "wip", "in progress")


# ── network (thin, kept out of the pure mapping) ───────────────────────────────
def _csv_url(gid: str) -> str:
    return (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
            f"/export?format=csv&gid={gid}")


def fetch_tab_csv(gid: str, timeout: int = 30) -> str:
    """Fetch one tab as CSV text. Raises on network/permission error."""
    req = urllib.request.Request(_csv_url(gid), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def list_tab_gids(timeout: int = 30) -> list[str]:
    """Scrape the tab gids from the sheet's htmlview page (primary first)."""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/htmlview"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "replace")
    gids = list(dict.fromkeys(re.findall(r"gid=(\d+)", html)))
    if PRIMARY_GID in gids:  # keep primary first
        gids = [PRIMARY_GID] + [g for g in gids if g != PRIMARY_GID]
    return gids or [PRIMARY_GID]


def parse_csv(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text))]


# ── shape detection (pure) ─────────────────────────────────────────────────────
def detect_shape(rows: list[list[str]]) -> str:
    """Classify a tab as 'ideas' | 'tracker' | 'unknown' from its content."""
    if not rows:
        return "unknown"
    header = [c.strip().lower() for c in rows[0]]
    header_join = " ".join(header)
    if "need by" in header_join and ("script" in header_join or "type" in header_join
                                     or "extra notes" in header_join):
        return "ideas"
    # tracker: status words or section dividers show up in the body
    flat = " ".join(c.strip().lower() for row in rows for c in row)
    if any(w in flat for w in _STATUS_WORDS) or "long video" in flat or "short video" in flat:
        # avoid misfiring on the ideas tab (which also mentions "not passed to editor")
        if "need by" not in header_join:
            return "tracker"
    return "unknown"


# ── row mapping (pure, unit-tested) ────────────────────────────────────────────
def _cell(row: list[str], i: int) -> str:
    return row[i].strip() if i < len(row) and row[i] is not None else ""


def _body_lines(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in pairs if v)


def map_ideas_row(row: list[str]) -> dict:
    """[title | need-by | script | project-files | extra-notes | type] → note/skip."""
    title = _cell(row, 0)
    if not title:
        return {"type": "skip", "reason": "no-title (reference-only or blank row)"}
    if _SECTION_LABELS_RE.match(title):
        return {"type": "skip", "reason": "section-label"}
    body = _body_lines([
        ("Need by", _cell(row, 1)),
        ("Script", _cell(row, 2)),
        ("Project files", _cell(row, 3)),
        ("Notes/reference", _cell(row, 4)),
        ("Type", _cell(row, 5)),
    ])
    return {"type": "note", "title": title, "tags": ["video", "idea"],
            "body": (title + "\n\n" + body).strip(), "role": "note"}


def is_done_section_label(label: str) -> bool:
    return bool(_DONE_SECTION_RE.search(label or ""))


def map_tracker_row(row: list[str], in_done_section: bool = False) -> list[dict]:
    """[title | status | gdoc | editor+fee] → list of destinations.

    - in-flight (has a non-blank status, NOT in a done section)
        → task (content, med) + companion note #video #wip
    - done-awaiting-release (in an image-2 / done section)
        → note #video #release-queue ONLY (release lives in youtube-assistant)
    - blank status, just a title
        → note #video #idea
    Section-divider / blank rows → skip.
    """
    title = _cell(row, 0)
    status = _cell(row, 1)
    gdoc = _cell(row, 2)
    editor = _cell(row, 3)

    if not title and not status:
        return [{"type": "skip", "reason": "blank row"}]
    if title and not status and not gdoc and not editor and _SECTION_LABELS_RE.match(title):
        return [{"type": "skip", "reason": "section-divider"}]

    body = _body_lines([("Gdoc", gdoc), ("Editor/fee", editor), ("Status", status)])

    if in_done_section:
        return [{"type": "note", "title": title, "tags": ["video", "release-queue"],
                 "body": (title + "\n\n" + body).strip(), "role": "note"}]

    if status:  # in-flight → task + companion note
        return [
            {"type": "task", "title": title, "category": "content", "priority": "med",
             "due": None, "role": "task"},
            {"type": "note", "title": title, "tags": ["video", "wip"],
             "body": (title + "\n\n" + body).strip(), "role": "note"},
        ]

    # blank status, just a title → idea note
    return [{"type": "note", "title": title, "tags": ["video", "idea"],
             "body": (title + "\n\n" + body).strip() if body else title, "role": "note"}]


def map_tab(rows: list[list[str]], shape: str) -> list[dict]:
    """Map a whole tab → list of {raw, results:[...]} preserving order.

    `raw` is the joined source row (idempotency key). One tracker row may fan out
    into two results (task + note), so results is always a list.
    """
    out = []
    if shape == "unknown" or not rows:
        for row in rows:
            raw = " | ".join(row)
            out.append({"raw": raw, "results": [{"type": "skip",
                        "reason": "tab shape not importable (finance/analytics)"}]})
        return out

    data = rows[1:] if shape == "ideas" else rows  # ideas has a header row
    in_done = False
    for row in data:
        raw = " | ".join(row)
        if shape == "ideas":
            out.append({"raw": raw, "results": [map_ideas_row(row)]})
        else:  # tracker — track section state
            title = _cell(row, 0)
            if title and is_done_section_label(title) and not _cell(row, 1):
                in_done = True
            elif title and _SECTION_LABELS_RE.match(title) and not is_done_section_label(title) \
                    and not _cell(row, 1) and not _cell(row, 2):
                in_done = False
            out.append({"raw": raw, "results": map_tracker_row(row, in_done)})
    return out


# ── preview ────────────────────────────────────────────────────────────────────
# _ORDER / _HEADINGS are shared via import_common.


def _proposed_str(r: dict) -> str:
    t = r["type"]
    if t == "task":
        return f"TASK — \"{r.get('title')}\" (category={r.get('category')}, priority={r.get('priority')})"
    if t == "note":
        tags = " ".join("#" + x for x in r.get("tags", []))
        return f"NOTE — \"{r.get('title')}\" [{tags}]"
    if t == "skip":
        return f"SKIP — {r.get('reason')}"
    return f"{t.upper()} — {r.get('note') or r.get('reason')}"


def build_preview(tabs: list[dict], *, fetched: bool, note: str = "") -> str:
    counts = {k: 0 for k in _ORDER}
    total_rows = 0
    for tab in tabs:
        for entry in tab["mapped"]:
            total_rows += 1
            for r in entry["results"]:
                counts[r["type"]] = counts.get(r["type"], 0) + 1

    out = ["# Import preview — YouTube projects sheet", ""]
    out.append(f"Sheet: `{SHEET_ID}` ({'REAL DATA fetched' if fetched else 'NO DATA fetched'})")
    if note:
        out.append("")
        out.append(f"> {note}")
    out.append("")
    out.append("## Counts (destinations across all tabs)")
    out.append(f"- **{counts['task']}** tasks / **{counts['note']}** notes / "
               f"**{counts['journal']}** journal / **{counts['skip']}** skip / "
               f"**{counts['uncertain']}** uncertain  (from **{total_rows}** source rows)")
    out.append("")
    out.append("DRY-RUN preview — nothing written. Run with `--apply` to import (idempotent).")

    for tab in tabs:
        out.append("")
        out.append(f"## Tab gid={tab['gid']} — detected shape: **{tab['shape']}**")
        # group this tab's entries by destination
        pairs = []
        for entry in tab["mapped"]:
            for r in entry["results"]:
                pairs.append((entry["raw"], r))
        for kind in _ORDER:
            group = [(raw, r) for raw, r in pairs if r["type"] == kind]
            if not group:
                continue
            out.append("")
            out.append(f"### {_HEADINGS[kind]} ({len(group)})")
            for raw, r in group:
                out.append("")
                out.append(f"- `{raw}`")
                out.append(f"  → {_proposed_str(r)}")
    return "\n".join(out) + "\n"


# ── apply ──────────────────────────────────────────────────────────────────────
def apply_all(tabs: list[dict], *, verbose: bool = True) -> dict:
    from core.db import connect
    conn = connect()
    ledger = load_ledger()
    stats = {"created": 0, "already": 0, "skipped": 0}
    try:
        for tab in tabs:
            for entry in tab["mapped"]:
                for r in entry["results"]:
                    res = apply_item(conn, entry["raw"], r, ledger,
                                     role=r.get("role", ""))
                    stats[res["status"]] = stats.get(res["status"], 0) + 1
        save_ledger(ledger)
    finally:
        conn.close()
    if verbose:
        print(f"  applied: {stats}", file=sys.stderr)
    return stats


# ── driver ─────────────────────────────────────────────────────────────────────
def gather_tabs() -> tuple[list[dict], bool, str]:
    """Fetch + map every tab. Returns (tabs, fetched, note). On any network /
    permission error, returns ([], False, message) so the caller still exits 0."""
    try:
        gids = list_tab_gids()
    except Exception as e:
        return [], False, f"Could not enumerate tabs: {type(e).__name__}: {e}"

    tabs = []
    for gid in gids:
        try:
            text = fetch_tab_csv(gid)
        except Exception as e:
            return [], False, f"Fetch failed for gid={gid}: {type(e).__name__}: {e}"
        rows = parse_csv(text)
        shape = detect_shape(rows)
        tabs.append({"gid": gid, "shape": shape, "mapped": map_tab(rows, shape)})
    return tabs, True, ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import the YouTube-projects sheet (dry-run by default).")
    ap.add_argument("--apply", action="store_true",
                    help="actually write items (idempotent). Default = preview only.")
    args = ap.parse_args(argv)

    tabs, fetched, note = gather_tabs()
    os.makedirs(DATA_DIR, exist_ok=True)

    if not fetched:
        # Known-blocker path: still write a preview explaining no data was fetched.
        msg = ("NO DATA fetched — " + note + ". The row-mapping logic is built and "
               "unit-tested; re-run once the sheet is reachable.")
        print(msg, file=sys.stderr)
        preview = build_preview([], fetched=False, note=msg)
        with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
            f.write(preview)
        print(f"wrote preview → {PREVIEW_PATH}", file=sys.stderr)
        return 0

    for tab in tabs:
        print(f"tab gid={tab['gid']}: shape={tab['shape']}, "
              f"{len(tab['mapped'])} rows", file=sys.stderr)

    preview = build_preview(tabs, fetched=True)
    with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
        f.write(preview)
    print(f"wrote preview → {PREVIEW_PATH}", file=sys.stderr)

    if args.apply:
        print("--apply: writing through create_task / vault_store …", file=sys.stderr)
        apply_all(tabs)
    else:
        print("dry-run: nothing written (pass --apply to import)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
