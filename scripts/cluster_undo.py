#!/usr/bin/env python3
"""Reverse scripts/cluster_notes.py — remove exactly the topic tags it recorded.

Reads data/cluster_log.json ({slug: added_tag}) and removes ONLY those tags from each
note's frontmatter (via vault_store.remove_tag, which rewrites just the tags line). Any
other tags — including `imported` and anything Kelvin added by hand — are left untouched.

Dry-run by default; --apply performs the removal and clears the log.

Usage:
  python3 scripts/cluster_undo.py            # show what would be removed
  python3 scripts/cluster_undo.py --apply    # remove the recorded tags
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vault_store

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(_ROOT, "data", "cluster_log.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the removal")
    args = ap.parse_args()

    if not os.path.exists(LOG_PATH):
        print(f"no cluster log at {LOG_PATH} — nothing to undo.")
        return
    log = json.load(open(LOG_PATH))
    print(f"cluster log records {len(log)} tagged notes.")
    if not args.apply:
        for slug, tag in list(log.items())[:20]:
            print(f"  would remove #{tag} from {slug}")
        if len(log) > 20:
            print(f"  … and {len(log) - 20} more.")
        print("\n… dry-run only. Re-run with --apply to remove these tags.")
        return

    removed = 0
    for slug, tag in log.items():
        if vault_store.remove_tag(slug, tag):
            removed += 1
    print(f"removed {removed} tags.")
    json.dump({}, open(LOG_PATH, "w"))           # clear the log — undo is done
    print("cleared cluster_log.json.")


if __name__ == "__main__":
    main()
