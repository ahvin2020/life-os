"""Read-only query answering for the Telegram bot.

Sam can ASK about his data ("what are my todos", "any overdue?", "goals") and
get an instant, deterministic answer — no Claude call, no cost. Intent detection is
deliberately CONSERVATIVE: only clearly interrogative/list-shaped messages are
treated as queries; anything ambiguous falls through to capture, because a missed
answer is retryable but a missed capture is data loss.

All handlers reuse the existing routes helpers (today_tasks, day_score,
goal_progress) and vault_store — no query logic is duplicated here.
"""

from __future__ import annotations

import re

from core.db import today_iso
from domain.tasks_core import today_tasks, day_score
from domain.goals_core import goal_progress
from domain import vault_store

# Nouns that make a message about Sam's data.
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
    # Idea-pulls from the clustered library ("give me 5 ideas about CPF", "find me some
    # ideas for my next video") always need Claude — never a deterministic answer. Let
    # them fall through to the router so `library_ideas` can fire (else the find/​list
    # handlers below would swallow them into a fruitless note search).
    if re.search(r"\bideas?\b", t):
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
    plan = " · ☀" if (t.get("planned_on") and t["planned_on"] <= today) else ""
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
        "SELECT * FROM goals WHERE archived_at IS NULL AND deleted_at IS NULL ORDER BY period, created").fetchall()
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


def answer_query(conn, text: str):
    """Route a query-shaped message to a DETERMINISTIC handler (instant, free).
    Returns the answer string, or None when no deterministic handler matches — the
    caller then routes the message to the agentic router (router.route)."""
    t = (text or "").strip().lower()
    today = today_iso()

    m = _FIND_RE.match(t)
    if m:
        return _answer_find(m.group(1))
    if "overdue" in t:
        return _answer_overdue(conn, today)
    if "goal" in t:
        return _answer_goals(conn)
    # Bare "journal" / "what did I write [today]" → today's page. But "what did I write
    # ABOUT the reno" is a recall question about the PAST — let it fall through to the
    # router's vault_recall, don't answer with today's journal.
    if "journal" in t or re.fullmatch(r"what did i write( today)?\??", t):
        return _answer_journal(today)
    if "backlog" in t:
        return _answer_column(conn, today, "backlog", "🗂 Backlog")
    if "this week" in t:                         # NOT bare "week" — "how was my week?" → router
        return _answer_column(conn, today, "week", "🗓 This week")
    if any(w in t for w in ("todo", "task", "today")):
        return _answer_today(conn, today)
    return None                                  # → agentic router fallback

