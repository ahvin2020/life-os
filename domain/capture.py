"""Quick-capture routing — the ONE place that turns a raw line of text into a
task, note, or journal entry.

Both the web composer (POST /capture) and the future Telegram daemon call
route_capture(); factoring it here (per the build spec) guarantees the phone bot
and the web twin file things identically. create_task() is likewise the single
source of truth for inserting a task, shared by route_capture and routes_tasks.
"""

from __future__ import annotations

import html as _html
import os
import re
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, quote

import requests

from core.db import now_iso, today_iso
from domain import vault_store
from domain.vault_store import first_url  # single-source URL extractor (re-exported here)

_URL_RE = re.compile(r"https?://\S+", re.I)
_IDEA_DOMAINS = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")

# Query params that identify a share/campaign, not the content — dropped when
# normalising a URL so a re-shared link maps to the SAME note (no twin).
_STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "igsh", "igshid", "fbclid", "gclid", "si", "feature", "ref", "ref_src",
}



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
                parent_id=None, at_top=False, media=None, link=None) -> int:
    """Insert a task (or subtask when parent_id is set). Returns the new id.
    Single source of truth for task inserts. at_top=True surfaces the new task at
    the TOP of its column — used by the capture paths (bot / composer / refile),
    where "I just sent this" should be the first thing seen, not buried at the
    bottom of a long column. Board/editor creation keeps bottom placement."""
    ts = now_iso()
    if parent_id is not None:
        col = "backlog"  # subtasks ignore column/due
        due_date = planned_on = None
    if at_top and parent_id is None:
        row = conn.execute(
            "SELECT COALESCE(MIN(sort_order), 1) AS m FROM tasks "
            "WHERE col = ? AND parent_id IS NULL", (col,)).fetchone()
        sort_order = int(row["m"]) - 1
    else:
        sort_order = next_sort_order(conn, col, parent_id)
    # week_since = the "This week" staleness clock, stamped on entering the column.
    week_since = today_iso() if col == "week" else None
    cur = conn.execute(
        """INSERT INTO tasks
             (title, col, sort_order, priority, category, due_date, planned_on,
              recur_rule, goal_id, parent_id, done, completed_at, archived_at,
              week_since, media, link, created, updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,?,?,?,?,?)""",
        (title.strip(), col, sort_order, priority, category, due_date, planned_on,
         recur_rule, goal_id, parent_id, week_since, media, link, ts, ts),
    )
    return cur.lastrowid


def has_url(text: str) -> bool:
    """True when `text` CONTAINS a url somewhere. It may still be prose that merely CITES
    one — see is_bare_url."""
    return bool(_URL_RE.match(text.strip())) or any(
        d in text.lower() for d in ("instagram.com", "youtube.com", "youtu.be",
                                    "tiktok.com", "http://", "https://"))


def is_bare_url(text: str) -> bool:
    """True when `text` IS a url and nothing else — one token, no prose around it.

    The has_url/is_bare_url split is the whole point: ONE contains-test used to answer both
    questions, so every caller that meant "this message IS a shared link" silently accepted
    "this message mentions a link somewhere". That is how "add task, connect to my invoicing
    <reel>" was acked and filed as a saved link — a message can cite a url without being one.
    """
    body = (text or "").strip()
    return bool(body) and not body.split()[1:] and has_url(body)


# ── multi-item messages ─────────────────────────────────────────────────────────
# One Telegram message can hold several captures, one per line ("paste 3 links"). A
# whole message is otherwise treated as ONE note, so line 2+ used to vanish into the
# first note's body. We split ONLY when EVERY non-empty line is independently a
# capturable unit (a URL); a link-with-caption or a prose note with line breaks has
# non-unit lines, so it stays intact. (There are no text prefixes anymore — natural
# language is classified by the AI router, not by t:/n:/j: syntax.)


def _is_capture_unit(line: str) -> bool:
    """True if `line` on its own is a distinct capture (a bare URL)."""
    return is_bare_url(line)


# Leading phrases that unambiguously mean "create a plain task" — each literally names a
# task ("task"/"todo"), so they're caught deterministically (no claude, works offline)
# rather than sent to the AI router. Phrases that imply a TIME ("remind me to … at 9am")
# are deliberately NOT here: those need the router to tell a task from a timed reminder.
_TASK_VERBS = ("add a task", "add task", "new task", "create a task", "create task",
               "make a task", "make task", "todo", "to-do")

# The wrapper, then a word boundary, then ANY run of punctuation as the separator. `\b` is the
# real form of the "requires a word boundary" rule the enumerated separator list only
# approximated ("todolist" / "add a taskforce" still can't match — no boundary after the verb),
# and `\W*` means the separator can be a comma, colon, dash, semicolon, ellipsis or nothing at
# all without anyone maintaining a list of characters. That list is what broke "add task,
# connect to my invoicing": it held " :-–—" and no comma, so tier 1 missed and the URL branch
# below took the message. Longest verb first so "add a task" wins over "add task".
_TASK_VERB_RE = re.compile(
    r"^(?:" + "|".join(r"\s+".join(re.escape(w) for w in v.split())
                       for v in sorted(_TASK_VERBS, key=len, reverse=True)) + r")\b\W*",
    re.I)


# Tier 2 — bare action verbs that open a plain task ("buy milk", "call the dentist"). Kept
# deliberately SHORT and low noun-collision: a MISS is cheap (it falls through to the AI
# router, which still files it correctly), but a FALSE POSITIVE files a note as a task. So
# words that are just as often nouns are excluded on purpose — "book club", "text from mum",
# "order of service", "check out this link", "update from the team", "water bill",
# "schedule for the week". The word-boundary test below also drops past tense for free
# ("called"/"finished" don't match "call"/"finish"), which keeps journal entries out.
_ACTION_VERBS = ("buy", "call", "pay", "renew", "cancel", "submit", "install", "confirm",
                 "email", "send", "write", "fix", "finish", "wash",
                 "reply to", "respond to", "follow up", "pick up", "drop off")

# The next word proving the "verb" was really a NOUN ("email from mum", "order of service")
# or a different intent ("call me Sam" sets the greeting name) — bail to the router.
_VERB_BAIL_NEXT = ("from", "about", "of", "by", "me")

# Bail-guards. Each only ever HANDS BACK to the router, so the risk is one-directional: a
# miss costs ~5s, a false positive mis-files Sam's data. Every one below is a real phrasing
# that this layer used to swallow:
#   a question        — "call anyone about the invoice?" is a question, not a capture
#   a copula/past     — prose ABOUT a thing, not an instruction to do it: "email is down
#                       again", "call with Sam went well", "todo list is long" (which the
#                       tier-1 wrapper even mangled into a task titled "list is long")
#   a priority word   — the router owns priority; deterministic would bury the word in the
#                       title instead ("pay the invoice, urgent" → priority=high)
#   a 2nd action verb — several captures in one line ("buy milk and call the dentist");
#     after and/&/,     only the router can split them into separate tasks
_QUESTION_RE = re.compile(r"\?\s*$")
_PROSE_RE = re.compile(r"\b(?:is|are|was|were|went|isn't|aren't|wasn't|weren't)\b", re.I)
_PRIORITY_RE = re.compile(r"\b(?:urgent|asap|important|priority)\b", re.I)
_MULTI_RE = re.compile(r"(?:\band\b|&|,)\s+(?:" + "|".join(_ACTION_VERBS) + r")\b", re.I)

# Any when-language means a date/time has to be PARSED, and that's the router's job: it sets
# a real due date and tells a task from a timed reminder. The deterministic path stands down
# rather than bury the date in the title — "call the dentist tomorrow" is worth ~1s to come
# back as a task DUE tomorrow instead of one titled "the dentist tomorrow".
_WHEN_RE = re.compile(
    r"\b\d{1,2}:\d{2}\b"                                        # 15:30
    r"|\b\d{1,2}\s*(?:am|pm)\b"                                 # 3pm, 9 am
    r"|\bin\s+\d+\s*(?:min|mins|minute|minutes|hour|hours|hr|hrs|day|days|week|weeks)\b"
    r"|\b(?:today|tonight|tomorrow|tmr|tmrw|yesterday|eod|eow)\b"
    r"|\b(?:next|this|last)\s+\w+"                              # next week, this friday
    r"|\b(?:mon|tues?|wed|thur?s?|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}\b"
    # a BARE month is a deadline too ("renew passport before September") — the router owns
    # turning it into a real due date. "may" is left out on purpose: as a bare word it's far
    # more often the modal verb than the month, and a wrong miss here is cheap.
    r"|\b(?:january|february|march|april|june|july|august|september|october|november"
    r"|december)\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)\b",                             # the 15th
    re.I)


def task_imperative(text: str):
    """The task title if `text` opens with a deterministic task verb — either an explicit
    wrapper ('add a task X', 'todo X') or a bare action verb ('buy milk'). None when the
    router should take it instead: any when-language (a date/time it must parse), a verb
    that reads as a noun, or no match. Requires a word boundary after the verb (so
    'todolist' / 'add a taskforce' / 'called the dentist' don't match)."""
    body = (text or "").strip()
    low = body.lower()
    m = _TASK_VERB_RE.match(body)               # tier 1: explicit "add a task X" wrapper
    # The guards judge what's left AFTER the wrapper, because the wrapper already declared the
    # kind — its own punctuation is not evidence about the title. Judged against the whole
    # body, "add task, pay the invoice" tripped _MULTI_RE (", pay" reads as the second verb in
    # "buy milk and call the dentist") and bailed a perfectly plain one-task capture to the
    # router. On the title the guards still bite exactly as before: "to-do list is long" →
    # "list is long" → prose; "add a task, buy milk and call the dentist" → "and call" → multi.
    probe = body[m.end():].strip() if m else body
    # guards that outrank BOTH tiers ("todo list is long" is prose, not a todo)
    if (_QUESTION_RE.search(probe) or _PROSE_RE.search(probe)
            or _MULTI_RE.search(probe)):
        return None
    if m:
        # a date in an explicit add still goes to the router — it owns due-date parsing
        return probe if (probe and not _WHEN_RE.search(probe)) else None
    if _WHEN_RE.search(body):
        return None
    # A priority word only needs the router on a BARE verb, where it's a modifier the router
    # can lift out of the text ("pay the invoice, urgent" → title + priority=high). After an
    # explicit "add a task" wrapper (returned above) the user already declared the kind and
    # the rest is simply the title — "add a task urgent capture" is a task called "urgent
    # capture". "!" is this app's own marker, which _as_task already turns into priority=high.
    if _PRIORITY_RE.search(body) and not body.rstrip().endswith("!"):
        return None
    for v in _ACTION_VERBS:                     # tier 2: bare "buy milk" — the verb IS the task
        if low.startswith(v) and body[len(v):len(v) + 1] in ("", " "):
            rest = body[len(v):].strip()
            if not rest or rest.split()[0].lower() in _VERB_BAIL_NEXT:
                return None
            return body.strip()
    return None


def declares_kind(text: str) -> bool:
    """True when the message NAMES its own kind in words — "add a task …", "todo …",
    "remind me …". Reuses reminders._TRIGGER_RE so there is one list of reminder openers.

    This is what outranks the url branch. Every tier above that branch may DECLINE the
    details it can't parse (a date only the router reads, an unparseable time) — the bail
    guards in task_imperative exist precisely to hand those back to the router. But a
    decline is not a rejection of the KIND, and the url branch below happily claimed the
    fall-through: "add task: review this <reel>" bailed on when-language (`_WHEN_RE` reads
    "this https" as "this friday"), and instead of reaching the router it came back a saved
    #link note. When the kind is declared and the parser declines, the LADDER declines —
    'unsorted' is not explicit, so both surfaces hand it to the router, which is what the
    guard wanted all along."""
    from domain.reminders import _TRIGGER_RE
    body = (text or "").strip()
    return bool(_TASK_VERB_RE.match(body) or _TRIGGER_RE.match(body))


def classify(text: str, *, has_media: bool = False) -> str:
    """Name the deterministic kind of `text`, in the ladder's PRECEDENCE order:
    'reminder' | 'task' | 'note' | 'link' | 'unsorted'.

    THIS FUNCTION IS THE LADDER'S ORDER, and it is the only copy of it. route_capture files
    by it, is_explicit_capture gates on it, and the daemon's instant-ack link path asks it
    for 'link' by name. Nothing may re-derive the order locally: every caller that did got
    it wrong the same way — it asked a single cheap question ("contains a url?") that a tier
    ABOVE the url branch would have answered differently. That is the whole bug behind "add
    task, connect to my invoicing <reel>" coming back as a saved #link #idea note: the
    ladder ranks the task verb above the url and always did, but the two gates in front of
    it never consulted the ladder, so the url won before the task tier was ever reached.

    Adding a tier means adding it HERE — never in one gate or one surface.
    """
    from domain.reminders import parse_reminder
    body = (text or "").strip()
    if not body:
        return "note" if has_media else "unsorted"
    # A reminder needs an explicit trigger AND a real clock time; an attachment can't be one.
    if not has_media and parse_reminder(body) is not None:
        return "reminder"
    if task_imperative(body) is not None:
        return "task"
    if has_media:
        # free text + a file: keep them together as one note rather than guessing a task
        return "note"
    # A url makes this a link ONLY if the message didn't already name itself something else
    # and get declined for its details — that fall-through belongs to the router, not here.
    if has_url(body) and not declares_kind(body):
        return "link"
    return "unsorted"


# The kinds `text` names ITSELF — no claude needed to tell what it is. Everything else is
# natural language, which is the AI router's job.
_EXPLICIT_KINDS = ("reminder", "task", "link")


def is_explicit_capture(text: str) -> bool:
    """True when the text already declares its kind deterministically — a URL, a task-verb
    opener ('add a task …', 'todo …'), or a fully-parseable timed reminder ('remind me in 10
    minutes to call mum'). Such input is unambiguous, so both surfaces keep it on the instant
    path instead of spending a claude call on the AI router. (Colon prefixes like t:/n:/j:
    are no longer a thing — everything else is natural language and goes to the router.)"""
    return classify(text) in _EXPLICIT_KINDS


def split_capture_lines(text: str):
    """Split a multi-item message into its per-line captures, or None when it's a single
    capture (fewer than 2 lines, or any line isn't a standalone unit — e.g. a link with a
    caption, or a multi-line prose note). Only multi-URL messages split now."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2 or not all(_is_capture_unit(ln) for ln in lines):
        return None
    return lines


def route_deterministic(conn, text: str, source: str = "web",
                        enrich: str = "async") -> dict | None:
    """THE tier ladder for everything that does NOT need claude — the single source both
    surfaces route through (web POST /capture and the Telegram daemon).

    Returns a result dict, or None when nothing deterministic matched → the caller hands the
    text to the AI router. Order matters and is deliberate:
      1. explicit capture — a URL, a task-verb opener, a fully-parseable timed reminder
      2. an unambiguous list question ("what's overdue?") → the instant `queries` answer
      3. a question answerable straight from the document-facts cache

    Why this lives HERE and not per-surface: the web bar and the phone bot each used to own
    their own ladder, so they drifted — the phone made a task out of "add a task test 1"
    while the web filed a note. One ladder, one behaviour, both surfaces.

    NOTE the caller still owns its own UX-specific fast paths that run BEFORE this (the bot
    acks a bare link instantly then edits in the enriched reply; it also handles pending
    yes/no and on-demand backlog triage), and its own rendering of the result.
    """
    text = (text or "").strip()
    if not text:
        return None
    if is_explicit_capture(text):
        return route_capture(conn, text, source=source, enrich=enrich)
    from domain.queries import is_query, answer_query
    if is_query(text):
        ans = answer_query(conn, text)
        if ans is not None:
            return {"kind": "answer", "reply": ans}
    from domain import docs
    fact = docs.answer_from_facts(conn, text)
    if fact is not None:
        return {"kind": "answer", "reply": fact}
    return None


def route_capture(conn, text: str, source: str = "web", forced: str = "auto",
                  enrich: str = "async", media: str | None = None) -> dict:
    """Classify `text` and file it immediately. Returns a dict describing where it
    went: {kind, id|slug, label, title?, tags?}. `forced` pins the kind for a caller that
    already knows it (a photo caption → 'note', a refile target); 'auto' deterministically
    catches a task-verb opener + URLs and files everything else as an unsorted note.
    `enrich` controls link enrichment: 'async' (default → web/voice) schedules the
    background rewrite; 'off' skips it so a caller (the Telegram bot) can enrich inline
    and edit its own reply. `media` is an optional comma-separated list of vault/.media
    pointers to attach to whatever the capture becomes; a lone attachment (no text) files
    as a note."""
    text = (text or "").strip()
    media = (media or "").strip() or None
    if not text and not media:
        return {"kind": "none", "label": "nothing to add"}

    body = text

    # --- caller-forced kind (photo caption → note, refile targets) ---
    if forced == "task":
        return _as_task(conn, body, media=media)
    if forced == "journal":
        return _as_journal(body, media=media)
    if forced == "note":
        return _as_note(conn, body, tags=[], media=media)

    # --- attachment with no text → a note (its filename becomes the title) ---
    if not body:
        return _as_note(conn, "", tags=[], media=media)

    # --- auto: file by whatever `classify` names it. There are no t:/n:/j: prefixes anymore —
    # natural language is the AI router's job; this deterministic path only catches the
    # unambiguous shapes and files the rest as a note. The ladder's ORDER lives in classify()
    # and nowhere else, so the gates in front of this (is_explicit_capture, the daemon's link
    # ack) can't jump a tier.
    low = body.lower()
    kind = classify(body, has_media=bool(media))
    if kind == "reminder":
        from domain import reminders
        rem = reminders.parse_reminder(body)
        r = reminders.create_reminder(conn, rem["text"], rem["fire_local"])
        return {"kind": "reminder", "id": r["id"], "title": r["text"],
                "label": "Reminders · " + r["label"], "reminder": r}
    if kind == "task":
        return _as_task(conn, task_imperative(body), media=media)
    if kind == "note":
        return _as_note(conn, body, tags=[], media=media)
    if kind == "link":
        # Dedupe by normalised URL: a re-shared link touches the existing #link note
        # instead of minting a twin (strip utm/igsh/etc first).
        url = first_url(body)
        existing = find_link_note_by_url(url) if url else None
        if existing:
            vault_store.touch_note(existing["slug"])
            if enrich == "async":
                schedule_enrichment(existing["slug"])
            tag_str = " ".join("#" + t for t in existing["tags"]) if existing["tags"] else ""
            return {"kind": "note", "slug": existing["slug"],
                    "label": "Notes" + (f" · {tag_str}" if tag_str else ""),
                    "title": existing["title"], "tags": existing["tags"], "deduped": True}
        tags = ["link"]
        if any(d in low for d in _IDEA_DOMAINS):
            tags.append("idea")
        res = _as_note(conn, body, tags=tags, title=_url_title(body))
        if enrich == "async":
            schedule_enrichment(res["slug"])   # async: capture stays instant
        return res
    # ambiguous → filed now as an unsorted note; triage refiles later (Phase 2)
    return _as_note(conn, body, tags=["unsorted"])


_TRAIL_JUNK = " \t-–—:,;.·|(){}[]<>\"'"   # what's left holding the gap a lifted url vacated


def split_off_link(text: str) -> tuple:
    """('clean title', 'url' | None) — lift the url OUT of a task's title.

    A task may CITE a link ("add task, connect to my invoicing <reel>") and the url is the
    task's reference, not part of what to DO. Wedged into the title it's ~70 chars of
    tracking querystring that reads as noise on a card, buries the actual verb, and drags a
    dead url through search; dropping it instead would lose the reel. So it moves to
    tasks.link (schema v10) and the title says only the thing to do."""
    url = first_url(text or "")
    if not url:
        return (text or "").strip(), None
    title = (text or "").replace(url, " ")
    # a lifted url leaves its punctuation behind — "watch this: <url>" must not end at ":"
    title = " ".join(title.split()).strip(_TRAIL_JUNK)
    return title, url


def _as_task(conn, text, media=None) -> dict:
    priority = None
    if text.rstrip().endswith("!"):
        priority = "high"
        text = text.rstrip()[:-1].strip()
    title, link = split_off_link(text)
    # a task that is ONLY a url has no words left to be a title — keep the url as the title
    # rather than filing an "Untitled task" whose one identifying detail is hidden in a chip
    title = title or link or "Untitled task"
    with conn:
        tid = create_task(conn, title, col="week", priority=priority,
                          at_top=True, media=media, link=link)
    label = "Tasks" + (" · high" if priority else "")
    return {"kind": "task", "id": tid, "label": label, "title": title,
            "priority": priority, "link": link}


def _as_note(conn, text, tags, title=None, media=None) -> dict:
    tags = tags or []
    if title is None:
        first = text.strip().splitlines()[0] if text.strip() else ""
        # an attachment-only capture is titled by its (first) filename
        if not first and media:
            first = vault_store.media_display_name(media.split(",")[0])
        title = first[:60] or "Attachment"
    note = vault_store.create_note(title=title, body=text, tags=tags, media=media)
    tag_str = " ".join("#" + t for t in tags) if tags else ""
    label = "Notes" + (f" · {tag_str}" if tag_str else "")
    return {"kind": "note", "slug": note["slug"], "label": label,
            "title": note["title"], "tags": tags}


def _as_journal(text, media=None) -> dict:
    day = today_iso()
    vault_store.append_journal_entry(day, text, source="", media=media)
    return {"kind": "journal", "id": day, "label": "today's Journal"}


_OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', re.I)
_OG_TITLE_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:title["\']', re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _url_title(text):
    """Title for a URL capture: the page's og:title (or <title>), fetched with a 3s
    timeout. Falls back to '<domain> link' so a note is never titled a bare domain."""
    m = _URL_RE.search(text)
    url = m.group(0) if m else text
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].strip()
    fallback = f"{host} link" if host else "link"
    if not m:
        return fallback
    try:
        # reuse the single og:/<title> scraper (same 3s timeout) — no duplicate fetch/parse
        title = (_fetch_og(url, 3).get("title") or "").strip()
        if title:
            return title[:120]
    except Exception:
        pass
    return fallback


# ── link enrichment ────────────────────────────────────────────────────────────
# When a URL is captured the raw note is just the link ("instagram.com" / "Add to
# note https://…"). Enrichment fetches page metadata best-effort, then makes ONE
# claude -p call combining that metadata + the user's accompanying words + profile.md
# to produce {title, summary, tags}, and rewrites the note. It runs ASYNC after the
# instant save so capture stays instant; any network/claude failure leaves the note
# untouched (same never-block-capture contract as triage).
_ADD_TO_NOTE_RE = re.compile(r"^\s*add to note\s*", re.I)


def normalize_url(url: str) -> str:
    """Canonical key for dedupe: lowercase host, drop 'www.', strip tracking params
    (utm_*/igsh/fbclid/…), drop the fragment, and trim a trailing slash. Two shares of
    the same link (with different igsh=/utm= tails) collapse to one key."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url.strip().lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
         if k.lower() not in _STRIP_PARAMS]
    query = urlencode(sorted(q))
    path = p.path.rstrip("/")
    # scheme collapsed to https so http/https of the same resource share one key
    return urlunparse(("https", host, path, "", query, ""))


def find_link_note_by_url(url: str):
    """The existing #link note whose first URL normalises to the same key as `url`
    (earliest-created wins), or None. Powers going-forward URL dedupe."""
    if not url:
        return None
    key = normalize_url(url)
    matches = []
    for n in vault_store.list_notes():
        if "link" not in (n["tags"] or []):
            continue
        nu = n.get("url") or first_url(n["body"])
        if nu and normalize_url(nu) == key:
            matches.append(n)
    if not matches:
        return None
    matches.sort(key=lambda n: (n["created"] or "", n["slug"]))
    return matches[0]


_OG_DESC_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']', re.I)
_OG_DESC_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']og:description["\']', re.I)


def fetch_link_metadata(url: str, timeout: int = 5) -> dict:
    """Best-effort page metadata: {title, author, description, site}. YouTube uses the
    unauthenticated oEmbed endpoint; everything else parses og:/<title>. Instagram &
    TikTok are attempted the same way but usually block — we degrade to {} silently.
    Never raises."""
    if not url:
        return {}
    low = url.lower()
    try:
        if "youtube.com" in low or "youtu.be" in low:
            return _fetch_youtube_oembed(url, timeout)
        return _fetch_og(url, timeout)
    except Exception:
        return {}


def _fetch_youtube_oembed(url: str, timeout: int) -> dict:
    api = "https://www.youtube.com/oembed?url=" + quote(url, safe="") + "&format=json"
    resp = requests.get(api, timeout=timeout,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; LifeOS/1.0)"})
    data = resp.json()
    return {
        "title": (data.get("title") or "").strip(),
        "author": (data.get("author_name") or "").strip(),
        "description": "",
        "site": "youtube",
    }


def _fetch_og(url: str, timeout: int) -> dict:
    resp = requests.get(url, timeout=timeout, allow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; LifeOS/1.0)"})
    page = resp.text or ""
    mt = _OG_TITLE_RE.search(page) or _OG_TITLE_RE2.search(page) or _TITLE_RE.search(page)
    md = _OG_DESC_RE.search(page) or _OG_DESC_RE2.search(page)
    title = _html.unescape(re.sub(r"\s+", " ", mt.group(1)).strip()) if mt else ""
    desc = _html.unescape(re.sub(r"\s+", " ", md.group(1)).strip()) if md else ""
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return {"title": title[:200], "author": "", "description": desc[:500], "site": host}


def _user_words(body: str, url: str) -> str:
    """The user's own accompanying text (their 'reason'): the note body minus the URL
    and the 'Add to note' capture boilerplate."""
    text = (body or "")
    if url:
        text = text.replace(url, " ")
    text = _URL_RE.sub(" ", text)          # drop any other URLs too
    text = _ADD_TO_NOTE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _enrich_prompt(url: str, meta: dict, words: str, profile: str) -> str:
    meta_lines = "\n".join(f"{k}: {v}" for k, v in meta.items() if v) or "(none available)"
    return (
        "You enrich a saved link for a personal notes app. Using the fetched page "
        "metadata, the user's own words (their REASON for saving it), and their "
        "profile, produce STRICT JSON: "
        '{"title": "...", "summary": "one line, why this matters to THEM", '
        '"tags": ["lowercase", "no-hash"]}. '
        "Title: concise, human (never a bare domain). Summary: <=140 chars, specific. "
        "Tags: 2-4, reuse the profile's vocabulary. Respond with ONLY the JSON.\n\n"
        f"=== URL ===\n{url}\n\n"
        f"=== FETCHED METADATA ===\n{meta_lines}\n\n"
        f"=== USER'S WORDS (their reason) ===\n{words or '(none)'}\n\n"
        f"=== vault/profile.md ===\n{profile}\n"
    )


def _parse_enrichment(raw: str) -> dict | None:
    from ai.claude_cli import extract_json
    data = extract_json(raw, "object")
    if data is None:
        return None
    title = (data.get("title") or "").strip()
    summary = (data.get("summary") or "").strip()
    tags = [str(t).lstrip("#").strip().lower() for t in (data.get("tags") or []) if str(t).strip()]
    if not title and not summary:
        return None
    return {"title": title, "summary": summary, "tags": tags}


def _default_claude(prompt: str) -> str:
    from ai.claude_cli import call_claude
    return call_claude(prompt, timeout=45)


def _enrich(slug: str, *, fetch_fn=None, claude_fn=None) -> tuple | None:
    """Shared enrichment work: fetch page metadata, make ONE claude call, and rewrite the
    note title/tags/body = URL + summary + user's words + fetched description. Returns
    (saved_note, data) where data={title, summary, tags}, or None if there's no URL /
    claude fails (note left untouched — the never-block-capture contract)."""
    note = vault_store.read_note(slug)
    if not note:
        return None
    url = first_url(note["body"])
    if not url:
        return None
    meta = (fetch_fn or fetch_link_metadata)(url) or {}
    words = _user_words(note["body"], url)
    prompt = _enrich_prompt(url, meta, words, vault_store.read_profile())
    try:
        raw = (claude_fn or _default_claude)(prompt)
    except Exception:
        raw = ""
    data = _parse_enrichment(raw)
    if not data:
        return None   # never-block-capture: leave the raw note exactly as it was
    title = data["title"] or note["title"]
    tags = list(dict.fromkeys((note["tags"] or []) + data["tags"]))
    parts = [url]
    if data["summary"]:
        parts.append(data["summary"])
    if words:
        parts.append(words)
    if meta.get("description") and meta["description"] not in (words, data["summary"]):
        parts.append(meta["description"])
    body = "\n\n".join(parts)
    saved = vault_store.write_note(slug, title, tags, body, note["pinned"], note["created"])
    return saved, data


def enrich_note(slug: str, *, fetch_fn=None, claude_fn=None) -> dict | None:
    """Enrich ONE captured link note in place. Returns the saved note, or None if there's
    no URL / claude fails (note untouched). Used by the async web/voice path."""
    res = _enrich(slug, fetch_fn=fetch_fn, claude_fn=claude_fn)
    return res[0] if res else None


def enrich_link(slug: str, *, fetch_fn=None, claude_fn=None) -> tuple:
    """Telegram sibling of enrich_note: returns (saved_note, summary) so the bot reply can
    surface the one-line 'why it matters'. (None, "") when enrichment didn't run."""
    res = _enrich(slug, fetch_fn=fetch_fn, claude_fn=claude_fn)
    if not res:
        return None, ""
    saved, data = res
    return saved, (data.get("summary") or "")


def _enrich_enabled() -> bool:
    """Async enrichment is on unless LIFEOS_ENRICH_LINKS=0 (the test suite sets 0 so
    captures never spawn a background claude call)."""
    return os.environ.get("LIFEOS_ENRICH_LINKS", "1") != "0"


def schedule_enrichment(slug: str) -> None:
    """Kick enrich_note in a daemon thread so the capture returns instantly. No-op when
    disabled. All errors are swallowed — enrichment must never break a capture."""
    if not slug or not _enrich_enabled():
        return

    def _run():
        try:
            enrich_note(slug)
        except Exception:
            pass

    threading.Thread(target=_run, name=f"enrich-{slug}", daemon=True).start()


# ── refiling helpers (shared by the Change button + Claude triage) ─────────────
# These are the ONE place that moves an item note↔task or retags it, so the web
# refile endpoint and the triage runner never duplicate the mutation logic.
def list_unsorted_notes() -> list:
    """Notes still tagged #unsorted (what triage looks at)."""
    return [n for n in vault_store.list_notes() if "unsorted" in (n["tags"] or [])]


def retag_note(slug: str, tags: list) -> dict | None:
    """Replace a note's tag set (drops #unsorted when real tags are supplied)."""
    note = vault_store.read_note(slug)
    if not note:
        return None
    tags = [t.lstrip("#") for t in (tags or [])]
    saved = vault_store.write_note(slug, note["title"], tags, note["body"],
                                   note["pinned"], note["created"])
    tag_str = " ".join("#" + t for t in tags) if tags else ""
    return {"kind": "note", "slug": slug,
            "label": "Notes" + (f" · {tag_str}" if tag_str else ""), "tags": tags}


def convert_note_to_task(conn, slug: str, *, title=None, category=None,
                         priority=None, due_date=None) -> dict | None:
    """Turn a captured note into a task (triage 'to_task' + the Change button).
    Creates the task via create_task(), then soft-deletes the source note."""
    note = vault_store.read_note(slug)
    if not note:
        return None
    with conn:
        tid = create_task(conn, title or note["title"] or "Untitled task",
                          col="week", priority=priority, category=category,
                          due_date=due_date, at_top=True)
    vault_store.delete_note(slug)
    bits = [b for b in ("high" if priority == "high" else None, category,
                        ("due " + due_date) if due_date else None) if b]
    label = "Tasks" + (" · " + " · ".join(bits) if bits else "")
    return {"kind": "task", "id": tid, "label": label, "from_slug": slug}


def convert_note_to_journal(slug: str) -> dict | None:
    """Move a captured note into today's journal page (triage 'to_journal' + Change)."""
    note = vault_store.read_note(slug)
    if not note:
        return None
    vault_store.append_journal_entry(today_iso(), note["body"] or note["title"], source="")
    vault_store.delete_note(slug)
    return {"kind": "journal", "id": today_iso(), "label": "today's Journal",
            "from_slug": slug}


def convert_task_to_journal(conn, task_id: int) -> dict | None:
    """Move a task into today's journal page, then delete the task row."""
    r = conn.execute("SELECT * FROM tasks WHERE id=? AND parent_id IS NULL",
                     (task_id,)).fetchone()
    if not r:
        return None
    vault_store.append_journal_entry(today_iso(), r["title"], source="")
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"kind": "journal", "id": today_iso(), "label": "today's Journal",
            "from_task": task_id}


def convert_task_to_note(conn, task_id: int, tags=None) -> dict | None:
    """Turn a task back into a note (the Change button, misfiled task→note).
    Reads the task, writes a note, then deletes the task row."""
    r = conn.execute("SELECT * FROM tasks WHERE id=? AND parent_id IS NULL",
                     (task_id,)).fetchone()
    if not r:
        return None
    tags = [t.lstrip("#") for t in (tags or ["unsorted"])]
    note = vault_store.create_note(title=r["title"], body=r["title"], tags=tags)
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    tag_str = " ".join("#" + t for t in tags) if tags else ""
    return {"kind": "note", "slug": note["slug"],
            "label": "Notes" + (f" · {tag_str}" if tag_str else ""),
            "tags": tags, "from_task": task_id}
