"""Read-only query answering for the Telegram bot.

Kelvin can ASK about his data ("what are my todos", "any overdue?", "goals") and
get an instant, deterministic answer — no Claude call, no cost. Intent detection is
deliberately CONSERVATIVE: only clearly interrogative/list-shaped messages are
treated as queries; anything ambiguous falls through to capture, because a missed
answer is retryable but a missed capture is data loss.

All handlers reuse the existing routes helpers (today_tasks, day_score,
goal_progress) and vault_store — no query logic is duplicated here.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from db import today_iso
from routes_tasks import today_tasks, day_score
from routes_goals import goal_progress
import vault_store

# Free-form Q&A context budget (chars). Oldest material is trimmed first.
_CTX_CAP = 12000

# Nouns that make a message about Kelvin's data.
_QNOUNS = ("todo", "todos", "task", "tasks", "today", "overdue", "goal", "goals",
           "journal", "note", "notes", "backlog")
# Leading words that make a message a question/list request.
_QSTARTS = ("what", "show", "list", "how many", "any", "give me", "do i have",
            "whats", "what's")
# Interrogative openers — with a trailing '?' these signal an open (free-form)
# question even when no data-noun is present ("how was my week?").
_QVERBS = ("how", "what", "why", "when", "who", "where", "which", "do", "did",
           "does", "is", "are", "am", "can", "should", "could", "would",
           "whats", "what's")
_FIND_RE = re.compile(r"^(?:find|search|notes?\s+about|show\s+me\s+notes?\s+about)\s+(.+)",
                      re.I)


def is_query(text: str) -> bool:
    """True only for clearly query-shaped messages; ambiguous → False (capture).
    A lost answer is retryable, a lost capture is data loss — so when in doubt, False."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if _FIND_RE.match(t):
        return True
    has_noun = any(re.search(r"\b" + re.escape(n) + r"\b", t) for n in _QNOUNS)
    if any(t.startswith(s) for s in _QSTARTS) and has_noun:
        return True
    if t.endswith("?"):
        # A question mark plus either a data-noun or an interrogative opener.
        if has_noun:
            return True
        if any(t == v or t.startswith(v + " ") for v in _QVERBS):
            return True
    return False


# ── formatting helpers ────────────────────────────────────────────────────────
def _due_suffix(t: dict, today: str) -> str:
    d = t.get("due_date")
    if not d:
        return ""
    if d == today:
        return " · due today"
    if d < today:
        return " · overdue"
    return f" · due {d}"


def _task_line(t: dict, today: str) -> str:
    mark = "❗" if t.get("priority") == "high" else "•"
    plan = " · ☀" if t.get("planned_on") == today else ""
    return f"{mark} {t['title']}{_due_suffix(t, today)}{plan}"


def _text_bar(pct: float, n: int = 5) -> str:
    filled = max(0, min(n, round((pct or 0) / 100 * n)))
    return "▓" * filled + "░" * (n - filled)


# ── individual handlers ───────────────────────────────────────────────────────
def _answer_today(conn, today):
    tasks = today_tasks(conn)
    open_t = [t for t in tasks if not t["done"]]
    score = day_score(tasks)
    if not open_t:
        return f"📋 Nothing on today — clear board. ({score['done']}/{score['total']} done)"
    lines = ["📋 Today:"] + [_task_line(t, today) for t in open_t]
    lines.append(f"({score['done']}/{score['total']} done today)")
    return "\n".join(lines)


def _answer_overdue(conn, today):
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL AND deleted_at IS NULL "
        "AND done=0 AND due_date IS NOT NULL AND due_date < ? ORDER BY due_date", (today,)).fetchall()
    if not rows:
        return "✅ Nothing overdue."
    lines = [f"⚠️ Overdue ({len(rows)}):"]
    for r in rows:
        lines.append(f"• {r['title']} · due {r['due_date']}")
    return "\n".join(lines)


def _answer_column(conn, today, col, heading):
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL AND deleted_at IS NULL "
        "AND done=0 AND col=? ORDER BY sort_order, id", (col,)).fetchall()
    if not rows:
        return f"{heading}: nothing here."
    lines = [f"{heading} ({len(rows)}):"]
    for r in rows:
        lines.append(_task_line(dict(r), today))
    return "\n".join(lines)


def _answer_goals(conn):
    rows = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL ORDER BY period, created").fetchall()
    if not rows:
        return "🎯 No goals set yet."
    lines = ["🎯 Goals:"]
    for g in rows:
        p = goal_progress(conn, g)
        if g["kind"] == "number":
            cur, tgt = int(p.get("current", 0)), int(p.get("target", 0))
            lines.append(f"• {g['title']}: {_text_bar(p.get('pct', 0))} {cur}/{tgt}")
        else:
            lines.append(f"• {g['title']}: {_text_bar(p.get('pct', 0))} {p.get('done', 0)}/{p.get('total', 0)}")
    return "\n".join(lines)


def _answer_journal(today):
    page = vault_store.read_journal(today)
    if not page or not page["entries"]:
        return "✦ No journal entries today yet."
    lines = ["✦ Today's journal:"]
    for e in page["entries"]:
        lines.append(f"{e['time']} — {e['text'][:120]}")
    return "\n".join(lines)


def _answer_find(term):
    term = term.strip().lower()
    hits = []
    for n in vault_store.list_notes():
        hay = (n["title"] + " " + (n["body"] or "")).lower()
        if term in hay:
            hits.append(n["title"])
        if len(hits) >= 5:
            break
    if not hits:
        return f"🔍 No notes matching '{term}'."
    return f"🔍 Notes matching '{term}':\n" + "\n".join("• " + h for h in hits)


_SUPPORTED = (
    "I can answer things like:\n"
    "• what are my todos / what's on today\n"
    "• any overdue?\n"
    "• tasks this week / backlog\n"
    "• goals\n"
    "• journal — what did I write today\n"
    "• find <term>\n"
    "…or ask me anything about your tasks, notes, journal and goals.")


def answer_query(conn, text: str):
    """Route a query-shaped message to a DETERMINISTIC handler (instant, free).
    Returns the answer string, or None when no deterministic handler matches — the
    caller then falls back to the free-form Claude path (answer_freeform)."""
    t = (text or "").strip().lower()
    today = today_iso()

    m = _FIND_RE.match(t)
    if m:
        return _answer_find(m.group(1))
    if "overdue" in t:
        return _answer_overdue(conn, today)
    if "goal" in t:
        return _answer_goals(conn)
    if "journal" in t or "what did i write" in t:
        return _answer_journal(today)
    if "backlog" in t:
        return _answer_column(conn, today, "backlog", "🗂 Backlog")
    if "this week" in t:                         # NOT bare "week" — "how was my week?" → free-form
        return _answer_column(conn, today, "week", "🗓 This week")
    if any(w in t for w in ("todo", "task", "today")):
        return _answer_today(conn, today)
    return None                                  # → free-form Claude fallback


# ── free-form Claude Q&A (read-only fallback tier) ────────────────────────────
_STOPWORDS = {"what", "when", "where", "which", "about", "did", "have", "this",
              "that", "with", "the", "and", "for", "was", "how", "any", "are",
              "my", "i", "do", "too", "much", "on", "of", "a", "is", "me", "say",
              "said", "week", "today", "tell", "give"}


def _salient_terms(question: str) -> list:
    words = re.findall(r"[a-z0-9]{3,}", (question or "").lower())
    seen, out = set(), []
    for w in words:
        if w not in _STOPWORDS and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def build_context(conn, question: str, cap: int = _CTX_CAP) -> str:
    """Assemble a READ-ONLY snapshot for the model to answer `question` from.
    Trims oldest material first to stay under `cap` chars. Includes profile.md."""
    today = today_iso()
    week_ago = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=7)).date().isoformat()

    # profile.md (always kept — it's the smallest, most valuable context)
    profile = ""
    ppath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault", "profile.md")
    if os.path.exists(ppath):
        with open(ppath, encoding="utf-8") as f:
            profile = f.read()

    # open tasks + done-this-week
    open_rows = conn.execute(
        "SELECT title, col, due_date, category, priority FROM tasks WHERE parent_id IS NULL "
        "AND archived_at IS NULL AND deleted_at IS NULL AND done=0 ORDER BY col, sort_order").fetchall()
    open_lines = [f"- {r['title']} [{r['col']}"
                  + (f", due {r['due_date']}" if r["due_date"] else "")
                  + (f", {r['category']}" if r["category"] else "")
                  + (f", {r['priority']}" if r["priority"] else "") + "]"
                  for r in open_rows]
    done_rows = conn.execute(
        "SELECT title, completed_at FROM tasks WHERE parent_id IS NULL AND done=1 "
        "AND deleted_at IS NULL AND completed_at >= ? ORDER BY completed_at", (week_ago,)).fetchall()
    done_lines = [f"- {r['title']} (done {r['completed_at']})" for r in done_rows]

    # goals with progress
    goal_lines = []
    for g in conn.execute("SELECT * FROM goals WHERE archived_at IS NULL ORDER BY period, created").fetchall():
        p = goal_progress(conn, g)
        if g["kind"] == "number":
            goal_lines.append(f"- {g['title']} ({g['period']}): {int(p.get('current', 0))}/{int(p.get('target', 0))}")
        else:
            goal_lines.append(f"- {g['title']} ({g['period']}): {p.get('done', 0)}/{p.get('total', 0)}")

    # journal, last 7 days (oldest-first so trimming drops oldest)
    journal_blocks = []
    for d in sorted(vault_store.list_journal_days(), key=lambda x: x["day"]):
        if d["day"] < week_ago:
            continue
        page = vault_store.read_journal(d["day"])
        if page and page["entries"]:
            body = "\n".join(f"  {e['time']} {e['text']}" for e in page["entries"])
            journal_blocks.append(f"{d['day']}:\n{body}")

    # note titles + tags, plus full bodies of up to 3 that match the question
    notes = vault_store.list_notes()
    note_title_lines = [f"- {n['title']} [{', '.join('#' + t for t in n['tags'])}]" for n in notes]
    terms = _salient_terms(question)
    matched = []
    for n in notes:
        hay = (n["title"] + " " + (n["body"] or "")).lower()
        if any(term in hay for term in terms):
            matched.append(n)
        if len(matched) >= 3:
            break
    note_body_blocks = [f"### {n['title']}\n{(n['body'] or '').strip()[:2000]}" for n in matched]

    # Sections in trim priority: journal (oldest) and note titles trimmed first.
    def _assemble():
        parts = [
            "=== profile.md ===\n" + profile,
            "=== OPEN TASKS ===\n" + ("\n".join(open_lines) or "(none)"),
            "=== DONE THIS WEEK ===\n" + ("\n".join(done_lines) or "(none)"),
            "=== GOALS ===\n" + ("\n".join(goal_lines) or "(none)"),
            "=== JOURNAL (last 7 days) ===\n" + ("\n\n".join(journal_blocks) or "(none)"),
            "=== NOTE TITLES ===\n" + ("\n".join(note_title_lines) or "(none)"),
            "=== RELEVANT NOTE BODIES ===\n" + ("\n\n".join(note_body_blocks) or "(none)"),
        ]
        return "\n\n".join(parts)

    ctx = _assemble()
    # Trim oldest-first to respect the cap: drop oldest journal days, then oldest titles.
    while len(ctx) > cap and journal_blocks:
        journal_blocks.pop(0)
        ctx = _assemble()
    while len(ctx) > cap and note_title_lines:
        note_title_lines.pop(0)
        ctx = _assemble()
    if len(ctx) > cap:
        ctx = ctx[:cap]
    return ctx


def build_qa_prompt(conn, question: str) -> str:
    ctx = build_context(conn, question)
    return (
        "You are Kelvin's personal Life OS assistant answering a question about HIS "
        "own data (below). Answer briefly in plain text suitable for Telegram — no "
        "markdown tables. Reference specific items by name. If the data below does "
        "not contain the answer, say so plainly; NEVER invent facts.\n\n"
        f"{ctx}\n\n=== QUESTION ===\n{question}\n\n=== ANSWER ===\n")


def answer_freeform(conn, question: str, claude_fn=None):
    """Read-only free-form Q&A via `claude -p`. Returns the answer text, or None on
    failure/timeout (caller shows a retry message). NEVER mutates any data."""
    prompt = build_qa_prompt(conn, question)
    if claude_fn is None:
        from triage.run_triage import call_claude
        claude_fn = lambda p: call_claude(p, timeout=60)
    try:
        out = (claude_fn(prompt) or "").strip()
    except Exception:
        return None
    return out or None
