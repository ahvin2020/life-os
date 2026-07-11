#!/usr/bin/env python3
"""One-time sanctioned cleanup: merge notes that point at the SAME URL.

Imports historically deduped by RAW TEXT, so the same link shared with slightly
different wording (or different igsh/utm tails) landed as several separate notes.
This walks vault/notes, groups notes by their first URL *normalised*
(capture.normalize_url — strips utm_*/igsh/fbclid, fragment, trailing slash), and
for each group of duplicates:

  - keeps the EARLIEST-created note as the survivor,
  - unions the tag sets,
  - concatenates any body text the duplicates add that the survivor lacks,
  - soft-deletes the rest to vault/.trash/ (restorable 30 days).

Default is a dry-run PREVIEW; pass --apply to actually merge. Writes go only through
vault_store (soft-delete + write_note), never by hand.

Usage:
    python3 scripts/dedupe_notes.py           # preview
    python3 scripts/dedupe_notes.py --apply    # merge
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from domain import vault_store  # noqa: E402
from domain.capture import first_url, normalize_url  # noqa: E402


def _dup_groups() -> list:
    """Lists of notes (len >= 2) that share a normalised first URL, each sorted
    earliest-created first."""
    by_url = defaultdict(list)
    for n in vault_store.list_notes():
        url = n.get("url") or first_url(n["body"])
        if url:
            by_url[normalize_url(url)].append(n)
    groups = []
    for notes in by_url.values():
        if len(notes) > 1:
            notes.sort(key=lambda n: (n["created"] or "", n["slug"]))
            groups.append(notes)
    return groups


def merge_duplicate_url_notes(dry_run: bool = True) -> dict:
    """Merge same-URL duplicate notes. Returns
    {groups, removed, details:[{url, keep, merged:[slug,...]}]}."""
    report = {"groups": 0, "removed": 0, "details": []}
    for notes in _dup_groups():
        survivor, dups = notes[0], notes[1:]
        report["groups"] += 1
        detail = {"url": survivor.get("url") or first_url(survivor["body"]),
                  "keep": survivor["slug"], "merged": [d["slug"] for d in dups]}
        report["details"].append(detail)
        report["removed"] += len(dups)
        if dry_run:
            continue
        tags = list(survivor["tags"] or [])
        body = survivor["body"] or ""
        pinned = survivor["pinned"]
        for d in dups:
            for t in (d["tags"] or []):
                if t not in tags:
                    tags.append(t)
            pinned = pinned or d["pinned"]
            extra = (d["body"] or "").strip()
            if extra and extra not in body:
                body = (body.rstrip() + "\n\n" + extra).strip()
        vault_store.write_note(survivor["slug"], survivor["title"], tags, body,
                               pinned, survivor["created"])
        for d in dups:
            vault_store.delete_note(d["slug"])
    return report


def main() -> int:
    apply = "--apply" in sys.argv
    rep = merge_duplicate_url_notes(dry_run=not apply)
    verb = "MERGED" if apply else "WOULD MERGE"
    for d in rep["details"]:
        print(f"{verb}: {d['url']}\n  keep {d['keep']}  drop {', '.join(d['merged'])}")
    print(f"\n{verb}: {rep['groups']} duplicate URL group(s), "
          f"{rep['removed']} note(s) soft-deleted to .trash"
          + ("" if apply else "  (dry-run — pass --apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
