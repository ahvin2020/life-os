#!/usr/bin/env python3
"""One-time AI clustering of the imported idea library.

The vault holds ~700 notes tagged `imported` (IG reels/links + sheet/todo imports)
that form a flat, unbrowsable dump. This adds EXACTLY ONE topic tag per imported note
so the Notes tag-chip bar becomes a set of browsable shelves (cpf-retirement,
banks-cards, …). Imports stay ordinary notes — no separate view.

Two passes, both via claude_cli.call_claude (subscription auth, never raw subprocess):
  Pass 1 (taxonomy): titles (+ a snippet where the title is generic) → 6-10 short
                     lowercase topic slugs tailored to Kelvin's actual library.
  Pass 2 (assignment): batches of ~60 notes → EXACTLY ONE slug each (or `misc`).
                     Unparseable batch → those notes are SKIPPED, never guessed.

Idempotent: assignments + taxonomy cache in data/cluster_cache.json; what actually got
written to disk is logged in data/cluster_log.json {slug: added_tag} (cluster_undo.py
reads it). Tags are applied with vault_store.add_tag, which rewrites ONLY the frontmatter
tags line — every other byte of the note is preserved, so note order is untouched.

Dry-run by default (prints taxonomy + sample assignments); --apply writes to the vault.

Usage:
  python3 scripts/cluster_notes.py            # dry-run: taxonomy + sample assignments
  python3 scripts/cluster_notes.py --apply    # write one topic tag per imported note
  python3 scripts/cluster_notes.py --retaxonomy   # force a fresh taxonomy pass
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vault_store
from claude_cli import call_claude

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(_ROOT, "data", "cluster_cache.json")
LOG_PATH = os.path.join(_ROOT, "data", "cluster_log.json")

TARGET_TAG = "imported"          # notes to cluster
MISC = "misc"                    # catch-all when nothing fits
ASSIGN_BATCH = 60
TAXONOMY_CHAR_BUDGET = 38000     # cap the one taxonomy call's listing size

TAXONOMY_PROMPT = """You are organising the saved-idea library of Kelvin, a Singapore-based
personal finance / investing YouTuber. Below are titles (and short snippets where a title is
generic) of {n} saved notes — Instagram reels, links, and imported to-dos.

Derive between 6 and 10 SHORT lowercase topic slugs in kebab-case (e.g. cpf-retirement,
banks-cards, property, market-investing, creator-craft, sponsor-business, personal-admin)
that together cover THIS library — tailored to the actual data below, not a generic list.
Each slug becomes a browsable shelf Kelvin filters by, so make them distinct and useful.

Reply with ONLY a JSON array of objects, each {{"slug": "...", "meaning": "one short line"}}.
No prose, no code fences.

Notes:
{items}"""

ASSIGN_PROMPT = """You are shelving Kelvin's saved-idea notes (Singapore finance/investing
YouTuber) into EXACTLY ONE topic each. The allowed topic slugs are:
{taxonomy}

For each numbered note below, choose the SINGLE best-fitting slug from that list. If none
genuinely fits, use "{misc}". Reply with ONLY a JSON object mapping each number (as a string)
to its chosen slug, e.g. {{"0":"cpf-retirement","1":"{misc}"}}. Every number below must appear
exactly once, and every value must be one of the allowed slugs or "{misc}". No prose, no fences.

Notes:
{items}"""


# ── data ──────────────────────────────────────────────────────────────────────
def imported_notes():
    """Notes tagged `imported`, in list_notes() order (newest created first)."""
    return [n for n in vault_store.list_notes() if TARGET_TAG in n["tags"]]


def _generic_title(title: str) -> bool:
    t = (title or "").strip().lower()
    return len(t) < 20 or t.startswith(("ig reel", "http", "untitled"))


def _listing_line(idx: int, n: dict) -> str:
    """`<idx>: <title>` — plus a body snippet when the title carries little signal."""
    title = (n["title"] or "").strip().replace("\n", " ")[:90]
    if _generic_title(title):
        body = re.sub(r"\s+", " ", (n["body"] or "")).strip()[:120]
        if body and body.lower() != title.lower():
            return f"{idx}: {title} — {body}"
    return f"{idx}: {title}"


# ── cache / log ───────────────────────────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=2, ensure_ascii=False)


# ── pass 1: taxonomy ──────────────────────────────────────────────────────────
def derive_taxonomy(notes):
    """One claude call over the note titles → list of {slug, meaning}. Truncates the
    listing to a char budget (a representative sample) rather than blowing the prompt."""
    lines, used = [], 0
    for i, n in enumerate(notes):
        line = _listing_line(i, n)
        if used + len(line) > TAXONOMY_CHAR_BUDGET:
            break
        lines.append(line)
        used += len(line) + 1
    prompt = TAXONOMY_PROMPT.format(n=len(notes), items="\n".join(lines))
    out = call_claude(prompt, timeout=300)
    m = re.search(r"\[.*\]", out or "", re.S)
    if not m:
        raise SystemExit("Pass 1 failed: taxonomy reply was not parseable JSON. Aborting "
                         "(nothing written).")
    data = json.loads(m.group(0))
    tax = []
    seen = set()
    for item in data:
        slug = str(item.get("slug", "")).strip().lower().lstrip("#")
        slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
        if slug and slug not in seen and slug != MISC:
            seen.add(slug)
            tax.append({"slug": slug, "meaning": str(item.get("meaning", "")).strip()})
    if not tax:
        raise SystemExit("Pass 1 failed: no usable slugs in taxonomy reply. Aborting.")
    return tax


# ── pass 2: assignment ────────────────────────────────────────────────────────
def assign(notes, taxonomy, cache):
    """Assign each note EXACTLY one slug. Reuses cached assignments; only calls claude for
    notes not yet cached. Unparseable batch → SKIP those notes (they stay unassigned)."""
    allowed = {t["slug"] for t in taxonomy} | {MISC}
    tax_str = "\n".join(f"- {t['slug']}: {t['meaning']}" for t in taxonomy)
    assignments = dict(cache.get("assignments", {}))
    todo = [(i, n) for i, n in enumerate(notes) if n["slug"] not in assignments]
    skipped = 0
    for b in range(0, len(todo), ASSIGN_BATCH):
        chunk = todo[b:b + ASSIGN_BATCH]
        listing = "\n".join(_listing_line(j, n) for j, (_, n) in enumerate(chunk))
        prompt = ASSIGN_PROMPT.format(taxonomy=tax_str, misc=MISC, items=listing)
        try:
            out = call_claude(prompt, timeout=240)
        except Exception as e:
            print(f"  batch {b//ASSIGN_BATCH + 1}: claude error ({e}); SKIPPING {len(chunk)} notes")
            skipped += len(chunk)
            continue
        m = re.search(r"\{.*\}", out or "", re.S)
        if not m:
            print(f"  batch {b//ASSIGN_BATCH + 1}: unparseable reply; SKIPPING {len(chunk)} notes")
            skipped += len(chunk)
            continue
        try:
            mapping = json.loads(m.group(0))
        except json.JSONDecodeError:
            print(f"  batch {b//ASSIGN_BATCH + 1}: bad JSON; SKIPPING {len(chunk)} notes")
            skipped += len(chunk)
            continue
        n_ok = 0
        for j, (_, n) in enumerate(chunk):
            slug = str(mapping.get(str(j), "")).strip().lower().lstrip("#")
            if slug not in allowed:
                slug = MISC
            assignments[n["slug"]] = slug
            n_ok += 1
        print(f"  batch {b//ASSIGN_BATCH + 1}: assigned {n_ok}/{len(chunk)}")
    cache["assignments"] = assignments
    return assignments, skipped


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write tags to the vault")
    ap.add_argument("--retaxonomy", action="store_true", help="force a fresh taxonomy pass")
    args = ap.parse_args()

    notes = imported_notes()
    print(f"imported notes to cluster: {len(notes)}")
    if not notes:
        print("nothing to do.")
        return

    cache = load_json(CACHE_PATH, {})
    taxonomy = cache.get("taxonomy")
    if taxonomy and not args.retaxonomy:
        print("using cached taxonomy (pass --retaxonomy to regenerate).")
    else:
        print("Pass 1: deriving taxonomy…")
        taxonomy = derive_taxonomy(notes)
        cache["taxonomy"] = taxonomy
        save_json(CACHE_PATH, cache)

    print("\nTAXONOMY:")
    for t in taxonomy:
        print(f"  {t['slug']:<20} {t['meaning']}")

    print("\nPass 2: assigning one slug per note…")
    assignments, skipped = assign(notes, taxonomy, cache)
    save_json(CACHE_PATH, cache)

    # counts per slug
    counts = {}
    for n in notes:
        s = assignments.get(n["slug"])
        if s:
            counts[s] = counts.get(s, 0) + 1
    print("\nCLUSTER COUNTS:")
    for s, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {s:<20} {c}")
    assigned = sum(counts.values())
    print(f"\nassigned: {assigned}/{len(notes)}  ·  skipped (unparseable): {skipped}")

    # sample assignments for a human sanity check
    import random
    sample = random.sample(notes, min(20, len(notes)))
    print("\nSAMPLE ASSIGNMENTS (spot-check these):")
    for n in sample:
        print(f"  [{assignments.get(n['slug'], '—'):<18}] {n['title'][:70]}")

    if not args.apply:
        print("\n… dry-run only. Re-run with --apply once the taxonomy + samples look sane.")
        return

    print("\nApplying tags to the vault…")
    log = load_json(LOG_PATH, {})
    written = 0
    for n in notes:
        slug = assignments.get(n["slug"])
        if not slug or slug == n["slug"]:
            continue
        if slug in n["tags"]:                    # already carries this topic tag
            continue
        if vault_store.add_tag(n["slug"], slug):
            log[n["slug"]] = slug
            written += 1
    save_json(LOG_PATH, log)
    print(f"applied {written} tags. log: {LOG_PATH}")


if __name__ == "__main__":
    main()
