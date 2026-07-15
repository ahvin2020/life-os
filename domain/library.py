"""On-demand idea pull from the clustered idea library (Telegram).

Sam has ~700 imported idea notes (IG reels/links), each tagged with exactly ONE
topic cluster (the canonical record is data/cluster_log.json; the tag also lives in
each note's frontmatter). He can text the bot "give me 5 ideas about CPF" / "ideas
for my next video" / "what have I saved about bank promos" and get back 3–5 numbered
video-idea picks.

PULL ONLY — nothing here is scheduled or pushed, and it NEVER mutates tasks / notes /
goals (read-only). The router's `library_ideas` action routes here. Follow-up
mutations ("save #2 as a task to film") go back through the normal router actions,
resolved via the rolling exchange memory.

Flow:
  1. map the topic → a candidate note pool. Multi-concept topics ("editing with AI")
     straddle clusters, so the pool is the UNION of (a) every note in the closest
     matching cluster(s) and (b) OR-keyword hits across imported titles+bodies. Recall
     matters at this stage; the single Claude call below supplies the precision. Capped
     ~80, trimmed by recency.
  2. ONE `claude -p` call ranks the pool and picks the best 3–5 AS VIDEO IDEAS for a
     Singapore finance/investing YouTuber, favouring fresher saves on ties, each with a
     one-line why. It's told Sam's raw phrasing verbatim so it judges true fit.
  3. Strict JSON out, one retry, then a deterministic fallback (the N most recent notes
     in the matched cluster) so a reply is never dropped.
"""

from __future__ import annotations

import json
import os
import re

from core.db import data_dir
from core.text import tokenize
from domain import vault_store
from ai.claude_cli import call_claude

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLUSTER_LOG = os.path.join(data_dir(), "cluster_log.json")   # persistent mount, not /app/data

# The fixed taxonomy (also listed in vault/profile.md). One tag per imported note.
CLUSTERS = (
    "market-investing", "creator-craft", "sg-my-money-culture", "cpf-epf-retirement",
    "brokers-platforms", "life-misc", "options-trading", "ai-investing-tools",
    "banks-cards", "misc", "property-housing",
)
_CLUSTER_SET = set(CLUSTERS)

# Common phrasings → cluster. These sit ON TOP of the automatic match against a
# cluster's own name-tokens (e.g. "options" already matches options-trading), so only
# words that DON'T appear in a cluster slug need listing here.
_ALIASES = {
    "cpf": "cpf-epf-retirement", "epf": "cpf-epf-retirement",
    "retire": "cpf-epf-retirement", "retirement": "cpf-epf-retirement",
    "frs": "cpf-epf-retirement", "srs": "cpf-epf-retirement",
    "option": "options-trading", "options": "options-trading",
    "spread": "options-trading", "wheel": "options-trading",
    "stock": "market-investing", "stocks": "market-investing",
    "etf": "market-investing", "etfs": "market-investing", "index": "market-investing",
    "reit": "market-investing", "reits": "market-investing",
    "dividend": "market-investing", "dividends": "market-investing",
    "sp500": "market-investing", "invest": "market-investing",
    "broker": "brokers-platforms", "brokers": "brokers-platforms",
    "platform": "brokers-platforms", "tiger": "brokers-platforms",
    "moomoo": "brokers-platforms", "ibkr": "brokers-platforms",
    "tastytrade": "brokers-platforms", "syfe": "brokers-platforms",
    "bank": "banks-cards", "banks": "banks-cards", "card": "banks-cards",
    "cards": "banks-cards", "promo": "banks-cards", "promos": "banks-cards",
    "cashback": "banks-cards", "uob": "banks-cards", "ocbc": "banks-cards",
    "hdb": "property-housing", "bto": "property-housing", "condo": "property-housing",
    "property": "property-housing", "housing": "property-housing",
    "mortgage": "property-housing", "rent": "property-housing",
    "ai": "ai-investing-tools", "chatgpt": "ai-investing-tools",
    "gpt": "ai-investing-tools", "tool": "ai-investing-tools",
    "tools": "ai-investing-tools",
    "editing": "creator-craft", "edit": "creator-craft", "thumbnail": "creator-craft",
    "hook": "creator-craft", "youtube": "creator-craft", "script": "creator-craft",
    "shorts": "creator-craft", "channel": "creator-craft", "content": "creator-craft",
    "singapore": "sg-my-money-culture", "malaysia": "sg-my-money-culture",
    "spending": "sg-my-money-culture", "salary": "sg-my-money-culture",
    "sg": "sg-my-money-culture", "budget": "sg-my-money-culture",
}

# Cluster name-token → cluster (so "options"→options-trading, "market"→market-investing).
_NAME_TOKENS: dict = {}
for _c in CLUSTERS:
    for _tok in _c.split("-"):
        _NAME_TOKENS.setdefault(_tok, []).append(_c)

_POOL_CAP = 80
_DEFAULT_COUNT = 5
_MIN_COUNT = 3
_MAX_COUNT = 5
_BODY_SNIPPET = 100
CLAUDE_TIMEOUT = 60

# Filler that carries no topic meaning — stripped before OR-keyword matching. "video"
# is filler here because ~everything in the library is a video.
_KW_STOP = {
    "the", "a", "an", "about", "for", "my", "me", "some", "give", "get", "got",
    "show", "find", "of", "on", "in", "with", "and", "to", "idea", "ideas", "saved",
    "save", "i", "have", "what", "next", "please", "you", "can", "any", "video",
    "videos", "reel", "reels", "topic", "topics", "make", "do", "want", "need",
}


# ── shelf summary (for the router context line — cheap, from cluster_log.json) ──
_SHELVES_CACHE = None


def shelf_summary() -> str:
    """A compact one-line census of the library for the router context, e.g.
    'market-investing 195, creator-craft 118, …'. Cached; empty string if the log
    is absent. Read from cluster_log.json so it costs ONE small read, not 700."""
    global _SHELVES_CACHE
    if _SHELVES_CACHE is not None:
        return _SHELVES_CACHE
    counts: dict = {}
    try:
        with open(_CLUSTER_LOG, encoding="utf-8") as f:
            data = json.load(f)
        for cluster in data.values():
            counts[cluster] = counts.get(cluster, 0) + 1
    except (OSError, json.JSONDecodeError, AttributeError):
        _SHELVES_CACHE = ""
        return _SHELVES_CACHE
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    _SHELVES_CACHE = ", ".join(f"{c} {n}" for c, n in ordered)
    return _SHELVES_CACHE


# ── topic → cluster(s) ─────────────────────────────────────────────────────────
def match_clusters(topic: str) -> list:
    """Best-matching cluster(s) for a free-text topic, ordered by strength (aliases and
    name-token hits first). Multi-concept topics can return several ("editing with ai"
    → creator-craft + ai-investing-tools). [] when nothing maps."""
    toks = tokenize(topic)
    out: list = []
    joined = "-".join(toks)
    # Whole-topic == a cluster slug (e.g. "market investing").
    if joined in _CLUSTER_SET and joined not in out:
        out.append(joined)
    for tok in toks:
        if tok in _CLUSTER_SET and tok not in out:
            out.append(tok)
        alias = _ALIASES.get(tok)
        if alias and alias not in out:
            out.append(alias)
        for c in _NAME_TOKENS.get(tok, []):
            if c not in out:
                out.append(c)
    return out


def concept_keywords(topic: str) -> list:
    """The meaningful concept words in a topic, filler removed ('editing with ai' →
    ['editing', 'ai']). Used for OR-keyword recall across the whole library."""
    seen, out = set(), []
    for w in tokenize(topic):
        if w in _KW_STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


# ── candidate pool ─────────────────────────────────────────────────────────────
def _cluster_of(note: dict):
    """The single cluster tag a note carries, or None if it isn't a library note."""
    for t in note.get("tags", []):
        if t in _CLUSTER_SET:
            return t
    return None


def imported_notes() -> list:
    """Every library note (has a cluster tag), newest-created first (list_notes order)."""
    return [n for n in vault_store.list_notes() if _cluster_of(n)]


def _kw_hits(keywords: list, note: dict) -> bool:
    """True if ANY concept keyword hits this note's title+body. Matching is prefix-lenient
    (min 4 chars shared) so 'editing' catches a caption like 'cut your edit time in
    half' — a naive equality/AND filter would miss it."""
    if not keywords:
        return False
    words = set(tokenize(note.get("title", "") + " " + (note.get("body") or "")))
    for k in keywords:
        if k in words:
            return True
        if len(k) >= 4:
            kp = k[:4]
            for w in words:
                if len(w) >= 4 and (w.startswith(kp) or k.startswith(w[:4])):
                    return True
    return False


def build_pool(topic: str, notes: list, cap: int = _POOL_CAP) -> list:
    """Candidate pool for `topic`: the UNION of (a) every note in the closest matching
    cluster(s) and (b) OR-keyword hits across imported titles+bodies. `notes` must be
    newest-first (imported_notes order); the pool preserves that order and is trimmed to
    the newest `cap`. Recall-first by design — the Claude ranking call adds precision."""
    clusters = set(match_clusters(topic))
    keywords = concept_keywords(topic)
    pool = []
    for n in notes:                              # newest-first preserved → recency trim is free
        if _cluster_of(n) in clusters or _kw_hits(keywords, n):
            pool.append(n)
    if not pool and not clusters and not keywords:
        pool = list(notes)                       # empty topic → whole library, newest first
    return pool[:cap]


# ── candidate rendering + selection prompt ─────────────────────────────────────
def _snippet(note: dict) -> str:
    body = re.sub(r"\s+", " ", (note.get("body") or "")).strip()
    return body[:_BODY_SNIPPET]


def _candidate_block(pool: list) -> str:
    lines = []
    for n in pool:
        bits = [f"slug: {n['slug']}", f"title: {n['title']}"]
        if n.get("created"):
            bits.append(f"saved {n['created'][:10]}")
        snip = _snippet(n)
        if snip and snip.lower() != (n["title"] or "").lower():
            bits.append(f"— {snip}")
        if n.get("url"):
            bits.append(n["url"])
        lines.append("- " + " | ".join(bits))
    return "\n".join(lines)


def build_selection_prompt(raw_message: str, topic: str, count: int, pool: list) -> str:
    return (
        "=== vault/profile.md (who Sam is) ===\n"
        f"{vault_store.read_profile()}\n\n"
        "=== TASK ===\n"
        "The user is a Singapore finance/investing YouTuber. From "
        "his saved idea library below, pick the strongest ideas for his NEXT VIDEO on the "
        "topic he asked about. Judge true fit against his EXACT phrasing — a note only "
        "tangentially related does NOT count (e.g. 'editing with AI' means video-editing "
        "with AI tools, not just any AI note). On ties, prefer more recently saved notes.\n"
        f'His exact words: "{(raw_message or topic).strip()}"\n'
        f"Resolved topic: {topic}\n"
        f"Pick exactly {count} (or fewer ONLY if the candidates genuinely don't support "
        f"{count}). Give each a ONE-LINE why it fits.\n\n"
        "=== CANDIDATE SAVES (his library) ===\n"
        f"{_candidate_block(pool)}\n\n"
        "=== OUTPUT ===\n"
        'Reply with ONE JSON object, no prose, no code fences:\n'
        '{"picks":[{"slug":"<exact slug from a candidate>","why":"<one line>"}, ...]}\n'
        "Use ONLY slugs present in the candidates above.\n\n=== JSON ===\n")


def _parse_picks(raw: str):
    """Extract {'picks':[{slug,why}]} from Claude's output. None if unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    picks = obj.get("picks")
    return picks if isinstance(picks, list) else None


# ── reply + memory formatting ──────────────────────────────────────────────────
def _topic_label(topic: str) -> str:
    return (topic or "").strip() or "your library"


def _format_reply(topic: str, picks: list) -> str:
    """picks: list of (note, why). Telegram plain text — numbered, URL on its own line
    so link-previews render."""
    header = f"💡 {len(picks)} idea" + ("s" if len(picks) != 1 else "") + f" about {_topic_label(topic)}:"
    blocks = [header]
    for i, (n, why) in enumerate(picks, 1):
        lines = [f"{i}. {n['title']}"]
        if why:
            lines.append(f"   {why.strip()}")
        if n.get("url"):
            lines.append(f"   {n['url']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _format_memory(topic: str, picks: list) -> str:
    """A COMPACT numbered title list stored in the router's exchange memory (bounded to
    _MEM_ENTRY_CAP chars). The full sent reply is far longer, so store the numbering +
    titles only — that's what a follow-up like 'save #2 as a task to film' resolves
    against."""
    parts = [f"Ideas about {_topic_label(topic)}:"]
    for i, (n, _why) in enumerate(picks, 1):
        parts.append(f"{i}. {n['title'][:60]}")
    return " ".join(parts)


# ── the one entry point ────────────────────────────────────────────────────────
def pull_ideas(conn, topic: str, count=None, claude_fn=None) -> tuple:
    """Pull 3–5 video ideas from the library for `topic`. Returns (telegram_reply,
    compact_memory_line). READ-ONLY — never mutates. On any Claude failure/invalid JSON
    (after one retry) falls back to the most-recent notes in the matched cluster, so a
    reply is never dropped."""
    topic = (topic or "").strip()
    notes = imported_notes()
    pool = build_pool(topic, notes)

    try:
        want = int(count)
    except (TypeError, ValueError):
        want = _DEFAULT_COUNT
    want = max(_MIN_COUNT, min(_MAX_COUNT, want)) if count is None else max(1, want)
    n = min(want, len(pool))

    if not pool:
        clusters = match_clusters(topic)
        shelves = shelf_summary()
        hint = (" I've got: " + shelves) if shelves else ""
        _ = clusters
        return (f"🔍 Nothing saved about {_topic_label(topic)} yet.{hint}", "")

    picks = _select(topic, topic, n, pool, claude_fn)
    if not picks:                                # fallback: newest in the matched cluster
        picks = _fallback_picks(topic, pool, notes, n)
    return (_format_reply(topic, picks), _format_memory(topic, picks))


def _select(raw_message: str, topic: str, n: int, pool: list, claude_fn) -> list:
    """The ONE Claude ranking call (one retry). Returns [(note, why), …] or [] on
    failure so the caller can fall back."""
    by_slug = {p["slug"]: p for p in pool}
    prompt = build_selection_prompt(raw_message, topic, n, pool)
    runner = claude_fn or (lambda p: call_claude(p, CLAUDE_TIMEOUT))
    for _ in range(2):
        try:
            raw = runner(prompt)
        except Exception:
            raw = None
        picks = _parse_picks(raw)
        if picks is None:
            continue
        out = []
        seen = set()
        for item in picks:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            if slug in by_slug and slug not in seen:
                seen.add(slug)
                out.append((by_slug[slug], str(item.get("why") or "").strip()))
        if out:
            return out[:n]
    return []


def _fallback_picks(topic: str, pool: list, notes: list, n: int) -> list:
    """Deterministic fallback = the N most-recent notes in the closest matched cluster
    (or the pool if the topic didn't map to a cluster). Newest-first ordering is already
    baked into `notes`/`pool`."""
    clusters = match_clusters(topic)
    if clusters:
        primary = clusters[0]
        recent = [n_ for n_ in notes if _cluster_of(n_) == primary]
        if recent:
            return [(x, "") for x in recent[:n]]
    return [(x, "") for x in pool[:n]]
