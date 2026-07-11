#!/usr/bin/env python3
"""Import Instagram saved posts + Claude-filtered recent likes as #ig notes.

Saved posts (deliberate saves): all imported.
Likes: only 2025+ items, filtered by `claude -p` for content-idea relevance
(Kelvin is a Singapore finance/investing YouTuber — see vault/profile.md).

Dry-run by default; --apply writes. Idempotent via the shared import ledger.
Notes carry the original like/save timestamp as `created` so they don't
flood the Recent section, and are tagged #ig #link #idea #imported.

Usage: python3 scripts/import_instagram.py --zip <export.zip> [--apply]
"""
import argparse, datetime, json, os, re, sys, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# import_common puts the repo root on sys.path, so vault_store / claude_cli resolve.
from import_common import ledger_key, load_ledger, save_ledger  # noqa: E402
from domain import vault_store  # noqa: E402
from ai.claude_cli import call_claude  # noqa: E402

CUTOFF = datetime.datetime(2025, 1, 1).timestamp()
BATCH = 60
TAGS = ["ig", "link", "idea", "imported"]

FILTER_PROMPT = """You filter Instagram reels liked by a Singapore-based finance/investing YouTuber.
KEEP items useful as content ideas or craft references: investing, personal finance, money,
Singapore economy/CPF/banks/property, creator techniques (hooks, thumbnails, editing, growth),
business/sponsorship. SKIP memes, lifestyle, food, travel, fitness, gaming, anything unrelated.
Reply with ONLY a JSON array of the ids to KEEP, e.g. [1,4,7]. No other text.

Items:
{items}"""


def load_entries(zip_path, name):
    with zipfile.ZipFile(zip_path) as z:
        try:
            return json.loads(z.read(f"your_instagram_activity/{name}"))
        except KeyError:
            return []


def fix_encoding(s):
    """IG exports store UTF-8 bytes as latin-1 codepoints ('â\x80\x99' etc.)."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def parse(entry):
    url, caption = "", ""
    for lv in entry.get("label_values", []):
        if lv.get("label") == "URL":
            url = lv.get("value") or lv.get("href") or ""
        elif lv.get("label") == "Caption":
            caption = lv.get("value") or ""
    ts = entry.get("timestamp") or 0
    return url, fix_encoding(caption).strip(), ts


def title_from(caption, url):
    first = re.split(r"(?:\n|(?<!\d)\.(?!\d)|[!?])", caption)[0].strip() if caption else ""
    if len(first) > 70:
        first = first[:67] + "…"
    return first or ("IG reel " + url.rstrip("/").rsplit("/", 1)[-1])


CACHE_PATH = "data/ig_filter_cache.json"


def claude_filter(items, urls):
    """items: list of (idx, caption); urls: idx->url. Returns set of kept idx."""
    cache = {}
    if os.path.exists(CACHE_PATH):
        cache = json.load(open(CACHE_PATH))
    kept = set(i for i, _ in items if cache.get(urls[i]) is True)
    items = [(i, c) for i, c in items if urls[i] not in cache]
    if not items:
        return kept
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        listing = "\n".join(f"{idx}: {cap[:180]}" for idx, cap in chunk)
        out = call_claude(FILTER_PROMPT.format(items=listing), timeout=300)
        m = re.search(r"\[[\d,\s]*\]", out)
        if not m:
            print(f"  batch {i//BATCH + 1}: unparseable reply, keeping all {len(chunk)} for safety")
            kept.update(idx for idx, _ in chunk)
            continue
        ids = set(json.loads(m.group(0)))
        for idx, _ in chunk:
            cache[urls[idx]] = idx in ids
            if idx in ids:
                kept.add(idx)
        print(f"  batch {i//BATCH + 1}: kept {len(ids & {x for x, _ in chunk})}/{len(chunk)}")
    json.dump(cache, open(CACHE_PATH, "w"))
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    saved = load_entries(args.zip, "saved/saved_posts.json")
    likes = load_entries(args.zip, "likes/liked_posts.json")

    candidates = []          # (raw_key, url, caption, ts, source)
    for e in saved:
        url, cap, ts = parse(e)
        if url:
            candidates.append((f"ig-saved|{url}", url, cap, ts, "saved"))

    recent = []
    for e in likes:
        url, cap, ts = parse(e)
        if url and ts >= CUTOFF:
            recent.append((f"ig-like|{url}", url, cap, ts, "like"))
    print(f"saved: {len(candidates)} · likes 2025+: {len(recent)} (of {len(likes)} total)")

    print("Claude-filtering likes…")
    idx_items = [(i, c[2] or c[1]) for i, c in enumerate(recent)]
    urls = {i: c[1] for i, c in enumerate(recent)}
    kept_idx = claude_filter(idx_items, urls)
    candidates += [recent[i] for i in sorted(kept_idx)]
    print(f"likes kept: {len(kept_idx)}/{len(recent)} · total to import: {len(candidates)}")

    ledger = load_ledger()
    new = [c for c in candidates if ledger_key(c[0]) not in ledger]
    print(f"new (not in ledger): {len(new)}")

    if not args.apply:
        for _, url, cap, ts, src in new[:10]:
            print(f"  [{src}] {title_from(cap, url)}  ({url})")
        print("… dry-run only. Re-run with --apply to import.")
        return

    for raw, url, cap, ts, src in new:
        created = datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
        title = title_from(cap, url)
        body = url + ("\n\n" + cap if cap else "")
        tags = TAGS + (["saved"] if src == "saved" else [])
        n = vault_store.create_note(title, body=body, tags=tags)
        vault_store.write_note(n["slug"], title, tags, body, False, created=created)
        ledger[ledger_key(raw)] = {"role": "ig", "slug": n["slug"]}
    save_ledger(ledger)
    print(f"imported {len(new)} notes.")


if __name__ == "__main__":
    main()
