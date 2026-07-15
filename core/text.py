import re

_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens. The single source for note/query tokenizing."""
    return _WORD_RE.findall((text or "").lower())


# ── search vocabulary ─────────────────────────────────────────────────────────
# THE stop/weak sets, shared by every keyword search (Gmail, Dropbox, docs). They were two
# divergent private lists and the drift was a live bug: Gmail's dropped "have"/"this", Dropbox's
# didn't, so a Dropbox ladder peeled a real question down to searching "up" and returned
# whichever file happened to contain it as genuine evidence. One vocabulary, one behaviour.

# Question scaffolding: words that say you're ASKING, not what you're asking about. A document
# or email never contains them, so ANDing them into a keyword search guarantees zero hits.
STOP = {
    "what", "whats", "when", "where", "which", "who", "whose", "how", "why", "the", "and",
    "for", "you", "your", "yours", "mine", "with", "was", "were", "are", "any", "did", "does",
    "do", "has", "have", "had", "from", "about", "please", "number", "date", "dates", "much",
    "many", "there", "their", "that", "this", "these", "those", "its", "our", "not", "but",
    "can", "could", "would", "should", "will", "shall", "get", "got", "give", "tell", "show",
    "find", "know", "need", "want", "look", "see", "into", "out", "off", "over", "under",
    "again", "still", "just", "only", "some", "all", "more", "most", "other", "than", "then",
    "him", "her", "his", "hers", "they", "them", "she", "his", "and",
}

# Time language: real words, but a confirmation email/document rarely spells them literally
# ("later this year" appears nowhere in a booking). Dropped only AFTER the full query misses,
# because occasionally they ARE the answer ("August invoice").
WEAK = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "today", "tomorrow", "yesterday", "week", "weeks", "month", "months", "year", "years",
    "next", "last", "coming", "upcoming", "later", "soon", "recent", "recently", "ago",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}


def content_terms(query: str, cap: int = 6) -> list:
    """The words in `query` worth sending to a keyword search, in order: no question
    scaffolding, no bare digits, nothing under 3 chars. Never returns a term a search would be
    embarrassed to run on its own."""
    out = []
    for t in tokenize(query):
        if len(t) >= 3 and t not in STOP and not t.isdigit():
            out.append(t)
    return out[:cap]


def first_non_empty(calls: list):
    """Run every callable CONCURRENTLY; return the first truthy result in LIST order (which is
    preference order — most specific first), else None.

    This is for a widening search ladder — "try the whole question, then fewer terms, then
    fewer still". Every rung is derivable before the first call, so discovering emptiness one
    network round trip at a time is pure waiting: measured, a Dropbox ladder burned 5.6s over 8
    sequential calls, seven of which were always going to be empty. The rungs are independent
    and read-only, so racing them costs the SLOWEST rung instead of the sum. A rung that raises
    counts as empty — one broken probe must never sink the ladder.

    Only race rungs that are CHEAP. Where each rung is expensive or quota-metered (a full Gmail
    search is ~130 quota units against a 250/second ceiling), probe first and fetch once.
    """
    import concurrent.futures as _cf
    calls = [c for c in calls if c]
    if not calls:
        return None
    if len(calls) == 1:
        try:
            return calls[0]() or None
        except Exception:
            return None
    with _cf.ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(c) for c in calls]
        for f in futures:                    # list order, NOT completion order
            try:
                got = f.result()
            except Exception:
                got = None
            if got:
                return got
    return None
