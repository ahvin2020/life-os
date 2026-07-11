"""Proactive AI surfaces — reasoned intelligence behind the scheduled sends.

The app's outbound surfaces used to be dumb templates. This module replaces their
bodies with a single `claude -p` call each, reasoning over live data. Three features,
each built the same way so they stay testable and never miss a send:

  * a PURE build-context function (no claude, unit-testable on seeded data)
  * one prompt template
  * a deterministic FALLBACK used verbatim whenever claude fails/times out

  1. morning_brief      — the 07:00 AI brief (replaces the digest body)
  2. backlog_triage     — Do / Defer / Delete verdicts over the stale backlog
  3. evening_reflection — 21:30 journal-reflection prompts grounded in the day

claude is reached ONLY through claude_cli.call_claude (subscription auth, no API
key), matching the router. Every call is wrapped so any failure returns the
fallback text — a scheduled send is never dropped.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import vault_store
from claude_cli import call_claude
from db import now_sg, today_iso
from goals_core import current_period_start, goal_progress, format_goal_progress

STALE_DAYS = 30


def _stale_days(conn) -> int:
    from db import get_setting
    try:
        return int(get_setting(conn, "stale_backlog_days", STALE_DAYS))
    except (TypeError, ValueError):
        return STALE_DAYS


# ── shared helpers ────────────────────────────────────────────────────────────
def _d(s: str) -> date:
    """Parse the date portion of an ISO string ('YYYY-MM-DD' or full timestamp)."""
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _age(day: str, iso: str | None) -> int:
    """Whole days between `day` and the date in `iso` (0 if missing/unparseable)."""
    if not iso:
        return 0
    try:
        return (_d(day) - _d(iso)).days
    except ValueError:
        return 0


def _meta_bits(t: dict) -> str:
    """Compact bracketed metadata for one task line in a prompt context block."""
    bits = []
    if t.get("category"):
        bits.append(t["category"])
    if t.get("priority") == "high":
        bits.append("high")
    bits.append(f"age {t['age_days']}d")
    if "untouched_days" in t:
        bits.append(f"untouched {t['untouched_days']}d")
    if t.get("due_date"):
        overdue = " (overdue)" if t["due_date"] < t["_day"] else ""
        bits.append(f"due {t['due_date']}{overdue}")
    if t.get("reschedule_count"):
        bits.append(f"postponed {t['reschedule_count']}×")
    return ", ".join(bits)


def _period_end(timeframe: str, g, day: str):
    """End date of the CURRENT period for a goal timeframe, or None (ongoing/unknown)."""
    d = _d(day)
    if timeframe == "week":
        return (d - timedelta(days=d.weekday())) + timedelta(days=6)      # Sunday
    if timeframe == "month":
        nxt = (d.replace(year=d.year + 1, month=1, day=1) if d.month == 12
               else d.replace(month=d.month + 1, day=1))
        return nxt - timedelta(days=1)
    if timeframe == "quarter":
        end_m = ((d.month - 1) // 3) * 3 + 3
        nxt = (d.replace(year=d.year + 1, month=1, day=1) if end_m == 12
               else d.replace(month=end_m + 1, day=1))
        return nxt - timedelta(days=1)
    if timeframe == "year":
        return date(d.year, 12, 31)
    if timeframe == "by_date":
        try:
            return _d(g["end_date"]) if g["end_date"] else None
        except (ValueError, TypeError):
            return None
    return None                                                           # ongoing


# ── FEATURE 1: AI morning brief ───────────────────────────────────────────────
def build_brief_context(conn, day: str = None, now=None) -> dict:
    """Pure snapshot for the morning brief: today's tasks (with ages/postpones), goal
    progress + period-end + pace math, yesterday's journal, stale-backlog count."""
    day = day or today_iso()
    now = now or now_sg()

    from tasks_core import today_task_rows
    rows = today_task_rows(conn, day)
    tasks = []
    for r in rows:
        if r["due_date"] and r["due_date"] < day:
            marker = f"overdue by {_age(day, r['due_date'])}d"
        elif r["due_date"] == day:
            marker = "due today"
        else:
            marker = "planned today"
        tasks.append({
            "_day": day, "title": r["title"], "marker": marker,
            "priority": r["priority"], "category": r["category"],
            "due_date": r["due_date"], "age_days": _age(day, r["created"]),
            "reschedule_count": r["reschedule_count"] or 0})

    goals = []
    for g in conn.execute(
            "SELECT * FROM goals WHERE archived_at IS NULL AND deleted_at IS NULL ORDER BY period, created").fetchall():
        p = goal_progress(conn, g)
        tf = g["timeframe"] or g["period"]
        end = _period_end(tf, g, day)
        days_left = (end - _d(day)).days if end else None
        has_open_task = any(not l["done"] for l in p["linked"])
        behind = False
        need_per_day = None
        prog_str = format_goal_progress(p)
        if p["shape"] in ("measure", "both"):
            cur, tgt = p["current"] or 0, p["target"] or 0
            remaining = max(0, tgt - cur)
            if tgt and end:
                p_start = _d(current_period_start(tf, day))
                total = (end - p_start).days or 1
                elapsed = max(0, (_d(day) - p_start).days)
                expected = tgt * min(1.0, elapsed / total)
                behind = cur < expected * 0.95 and remaining > 0
                if days_left is not None and days_left > 0:
                    need_per_day = remaining / days_left
                elif remaining > 0:
                    need_per_day = remaining          # period essentially over
        goals.append({
            "title": g["title"], "timeframe": tf, "shape": p["shape"],
            "prog_str": prog_str, "period_end": end.isoformat() if end else None,
            "days_left": days_left, "behind": behind, "need_per_day": need_per_day,
            "has_open_task": has_open_task})

    yest = (_d(day) - timedelta(days=1)).isoformat()
    ypage = vault_store.read_journal(yest)
    yesterday_journal = [f"{e['time']} {e['text']}" for e in ypage["entries"]] if ypage else []

    sd = _stale_days(conn)
    stale_count = len(_stale_rows(conn, day, sd))

    # ── prompt-ready text block ──
    tlines = [f"- {t['title']} [{t['marker']}, {_meta_bits(t)}]" for t in tasks]
    glines = []
    for gd in goals:
        parts = [f"- {gd['title']} ({gd['timeframe']}): {gd['prog_str']}"]
        if gd["period_end"]:
            parts.append(f"period ends {gd['period_end']} ({gd['days_left']}d left)")
        if gd["behind"] and gd["need_per_day"] is not None:
            parts.append(f"BEHIND PACE, need ~{math.ceil(gd['need_per_day'])}/day")
            parts.append("no linked open task" if not gd["has_open_task"]
                         else "has a linked open task")
        glines.append(", ".join(parts))

    text = "\n".join([
        f"TODAY: {day} ({now.strftime('%A')})",
        "",
        "TODAY'S TASKS (due / overdue / planned for today):",
        "\n".join(tlines) or "(nothing due or planned)",
        "",
        "GOALS (progress · period end · pace):",
        "\n".join(glines) or "(no active goals)",
        "",
        f"YESTERDAY'S JOURNAL ({yest}):",
        "\n".join("  " + j for j in yesterday_journal) or "  (nothing written)",
        "",
        f"STALE BACKLOG: {stale_count} tasks untouched {sd}+ days.",
    ])
    return {"day": day, "is_sunday": now.weekday() == 6, "tasks": tasks,
            "goals": goals, "yesterday_journal": yesterday_journal,
            "stale_count": stale_count, "text": text}


def brief_prompt(ctx: dict, backlog_summary: str | None = None) -> str:
    weave = ""
    if ctx["is_sunday"]:
        extra = ("\n\nHere is a fresh triage of his stale backlog — weave its key "
                 "verdicts, the one pattern, and the one question into a short "
                 "'Backlog' section at the END of the brief:\n" + backlog_summary
                 if backlog_summary else
                 "\n\nIt is Sunday — end with a short line nudging a weekly review "
                 "and setting next week's goals.")
        weave = extra
    return (
        "=== vault/profile.md (who Kelvin is — voice + context) ===\n"
        f"{vault_store.read_profile()}\n\n"
        "=== TASK ===\n"
        "You are Kelvin's Life OS assistant writing his MORNING BRIEF. Read the live "
        "context and write a short, sharp brief he'll actually act on, in his own "
        "plain voice (see profile). Rules:\n"
        "- Open by naming THE single most important thing to do today, and the "
        "specific deadline or dependency that makes it #1 — cite the actual date, "
        "overdue-days, or goal number, never a vague 'important'.\n"
        "- Then 4-6 items, each ONE line, each with a concrete reason it matters today.\n"
        "- Flag any COLLISION where a deadline lands the same day as unfinished prep "
        "it depends on.\n"
        "- If a goal is behind pace AND has no linked open task pulling it forward, add "
        "ONE goal-pace alert line naming the number and the daily rate needed.\n"
        "- Plain text for Telegram: no markdown, no headers, no tables. Tight — he "
        "reads this on his phone. Use a simple '-' for list items.\n"
        f"{weave}\n\n"
        "=== LIVE CONTEXT ===\n"
        f"{ctx['text']}\n\n"
        "=== BRIEF ===\n")


# ── deterministic morning digest (the brief's fallback body) ──────────────────
def _digest_tasks(conn, today):
    """Open tasks that matter today: due today, overdue, or ☀ planned."""
    from tasks_core import today_task_rows
    return today_task_rows(conn, today)


def _stale_backlog(conn, today, days=None):
    """Backlog tasks untouched for `days`+ (the Sunday do-or-delete nudge)."""
    from db import get_setting
    if days is None:
        try:
            days = int(get_setting(conn, "stale_backlog_days", "30"))
        except (TypeError, ValueError):
            days = 30
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).date().isoformat()
    return conn.execute(
        "SELECT title, updated FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL "
        "AND deleted_at IS NULL AND done = 0 AND substr(updated,1,10) < ? ORDER BY updated",
        (cutoff,)).fetchall()


def build_digest(conn, day=None, now=None) -> str:
    """Compose the morning-digest text: today's tasks, goal progress, journal nudge,
    and (Sundays) stale backlog + set-goals reminder. Pure — unit-tested directly."""
    day = day or today_iso()
    now = now or now_sg()

    lines = [f"☀ Good morning — {now.strftime('%A %-d %b')}"]

    tasks = _digest_tasks(conn, day)
    lines.append("")
    if tasks:
        lines.append(f"📋 Today ({len(tasks)}):")
        for t in tasks:
            mark = ""
            if t["due_date"] and t["due_date"] < day:
                mark = " · overdue"
            elif t["due_date"] == day:
                mark = " · due today"
            elif t["planned_on"] and t["planned_on"] <= day:   # sticky: rolled-over too
                mark = " · ☀ planned"
            if t["priority"] == "high":
                mark += " · high"
            lines.append(f"  • {t['title']}{mark}")
    else:
        lines.append("📋 Nothing due or planned today — a clear board.")

    goals = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL AND deleted_at IS NULL ORDER BY period, created").fetchall()
    if goals:
        lines.append("")
        lines.append("🎯 Goals:")
        for g in goals:
            prog = format_goal_progress(goal_progress(conn, g))
            lines.append(f"  • {g['title']}: {prog}")

    # Journal nudge if yesterday had no entry.
    yesterday = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
    if not vault_store.read_journal(yesterday):
        lines.append("")
        lines.append("✦ No journal entry yesterday — how did the day go?")

    # Sunday extras: stale backlog + set next week's goals.
    if now.weekday() == 6:
        stale = _stale_backlog(conn, day)
        lines.append("")
        if stale:
            lines.append(f"🧹 Stale backlog — do or delete ({len(stale)}):")
            for s in stale:
                lines.append(f"  • {s['title']}")
        lines.append("🗓 Weekly review: set next week's goals.")

    return "\n".join(lines)


def fallback_brief(conn, day: str, now, backlog_summary: str | None = None) -> str:
    """Deterministic morning brief — the existing digest template. Used when claude
    fails, so a missed send is impossible."""
    text = build_digest(conn, day, now)
    if backlog_summary and now.weekday() != 6:   # Sunday digest already lists stale items
        text += "\n\n" + backlog_summary
    return text


def morning_brief(conn, day: str = None, now=None, claude_fn=None,
                  backlog_summary: str | None = None) -> str:
    """The 07:00 AI brief. Returns Telegram-ready text; falls back to the deterministic
    digest on any claude failure."""
    day = day or today_iso()
    now = now or now_sg()
    ctx = build_brief_context(conn, day, now)
    prompt = brief_prompt(ctx, backlog_summary)
    runner = claude_fn or (lambda p: call_claude(p, timeout=120))
    try:
        out = (runner(prompt) or "").strip()
    except Exception:
        out = ""
    if not out:
        return fallback_brief(conn, day, now, backlog_summary)
    return f"☀ Morning brief — {now.strftime('%A %-d %b')}\n\n{out}"


# ── FEATURE 2: backlog intelligence ───────────────────────────────────────────
def _stale_rows(conn, day: str, days: int = None):
    days = _stale_days(conn) if days is None else days
    cutoff = (_d(day) - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT id FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL "
        "AND deleted_at IS NULL AND done=0 AND substr(updated,1,10) < ?",
        (cutoff,)).fetchall()


# On-demand trigger phrases. A deterministic fast-path (like queries.is_query) is the
# cleaner fit than a router action here: the phrases are unambiguous, and routing them
# through the router would spend a claude call just to decide to spend another one.
_TRIAGE_TRIGGERS = (
    "triage my backlog", "triage the backlog", "triage backlog", "backlog triage",
    "clean up my tasks", "clean up my backlog", "clean up the backlog", "clean up backlog",
    "clean my backlog", "review my backlog", "sort my backlog", "tidy my backlog",
    "declutter my tasks", "declutter my backlog")


def is_backlog_triage_request(text: str) -> bool:
    """True for an explicit on-demand backlog-triage request ('triage my backlog',
    'clean up my tasks'). Conservative substring match so polite wrappers still hit."""
    t = (text or "").strip().lower().rstrip("?.! ")
    return any(p in t for p in _TRIAGE_TRIGGERS)


def build_backlog_context(conn, day: str = None) -> dict:
    """Pure snapshot for backlog triage: ALL open tasks with metadata (age, untouched
    days, postponed count, due, goal-link status) sorted stalest-first, active goals
    (id/title/progress), plus 14-day completion stats per category (so the model sees
    what he actually finishes vs. what rots, and which open tasks could feed a goal)."""
    day = day or today_iso()
    rows = conn.execute(
        "SELECT id, title, category, priority, created, updated, due_date, "
        "reschedule_count, goal_id FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL "
        "AND deleted_at IS NULL AND done=0 ORDER BY updated").fetchall()
    tasks = []
    for r in rows:
        tasks.append({
            "_day": day, "id": r["id"], "title": r["title"], "category": r["category"],
            "priority": r["priority"], "due_date": r["due_date"],
            "age_days": _age(day, r["created"]),
            "untouched_days": _age(day, r["updated"]),
            "reschedule_count": r["reschedule_count"] or 0,
            "goal_id": r["goal_id"]})
    tasks.sort(key=lambda t: t["untouched_days"], reverse=True)

    goals = []
    for g in conn.execute(
            "SELECT * FROM goals WHERE archived_at IS NULL AND deleted_at IS NULL ORDER BY period, created").fetchall():
        prog = format_goal_progress(goal_progress(conn, g))
        goals.append({"id": g["id"], "title": g["title"],
                      "timeframe": g["timeframe"] or g["period"], "prog": prog})

    cutoff = (_d(day) - timedelta(days=14)).isoformat()
    stats = {}
    for r in conn.execute(
            "SELECT category, COUNT(*) c FROM tasks WHERE parent_id IS NULL AND done=1 "
            "AND deleted_at IS NULL AND completed_at >= ? GROUP BY category",
            (cutoff,)).fetchall():
        stats[r["category"] or "uncategorised"] = r["c"]

    def _tline(t):
        link = f", → goal #{t['goal_id']}" if t["goal_id"] else ", unlinked"
        return f"- #{t['id']} {t['title']} [{_meta_bits(t)}{link}]"
    tlines = [_tline(t) for t in tasks]
    glines = [f"- #{gd['id']} {gd['title']} ({gd['timeframe']}): {gd['prog']}" for gd in goals]
    stat_str = ", ".join(f"{k} {v}" for k, v in sorted(stats.items())) or "(none completed)"
    text = "\n".join([
        f"OPEN BACKLOG — {len(tasks)} tasks, stalest first (goal-link status shown):",
        "\n".join(tlines) or "(empty)",
        "",
        "ACTIVE GOALS (reference ONLY by these #ids):",
        "\n".join(glines) or "(none)",
        "",
        f"COMPLETED LAST 14 DAYS BY CATEGORY: {stat_str}",
    ])
    return {"day": day, "tasks": tasks, "goals": goals, "stats": stats, "text": text}


def backlog_prompt(ctx: dict) -> str:
    return (
        "=== vault/profile.md (who Kelvin is — voice + context) ===\n"
        f"{vault_store.read_profile()}\n\n"
        "=== TASK ===\n"
        "You are Kelvin's Life OS assistant running a BACKLOG TRIAGE. Below is his full "
        "open backlog (stalest first) and what he's actually completed lately. In his "
        "own plain voice, do exactly three things:\n"
        "1. Take the ~10 STALEST tasks. For each: a one-word verdict — Do, Defer, or "
        "Delete — then a one-line reason grounded in the data (untouched days, age, "
        "postponed count, a category he clearly isn't touching).\n"
        "2. ONE behavioral-pattern observation about how his backlog behaves (a "
        "category that never gets done, chronic postponing, etc.).\n"
        "3. ONE clarifying question about the single VAGUEST stale task, so he can "
        "sharpen or kill it.\n"
        "4. SUGGESTED LINKS (optional): scan ALL open tasks marked 'unlinked' — not just "
        "the stale ones — for any that plausibly advance one of the ACTIVE GOALS above. "
        "If you find genuinely plausible matches, add a section headed exactly "
        "'Suggested links:' and list AT MOST 3, each on its own line in this exact shape:\n"
        "   task #62 \"set up GIRO\" → goal #2 \"retire by 50\" — reply \"link task 62 to goal 2\"\n"
        "Use the real #ids and titles from the context. Only suggest links you're "
        "confident about — zero is fine; if nothing is a clear fit, OMIT the section "
        "entirely. NEVER invent an id, and NEVER claim you've linked anything — you are "
        "only proposing; Kelvin confirms by replying.\n"
        "Plain text for Telegram: no markdown tables, no headers beyond 'Suggested "
        "links:'. Keep every line short. Write ONLY the triage.\n\n"
        "=== LIVE CONTEXT ===\n"
        f"{ctx['text']}\n\n"
        "=== TRIAGE ===\n")


def fallback_backlog(ctx: dict) -> str:
    """Deterministic stalest-first list — used when claude fails."""
    lines = ["🧹 Backlog triage — stalest items (do or delete):"]
    for t in ctx["tasks"][:10]:
        pp = f" · postponed {t['reschedule_count']}×" if t["reschedule_count"] else ""
        lines.append(f"- {t['title']} · untouched {t['untouched_days']}d{pp}")
    return "\n".join(lines)


def backlog_triage(conn, day: str = None, claude_fn=None) -> str:
    """Run the backlog triage. Returns Telegram-ready text; falls back to a stalest-first
    list on any claude failure."""
    day = day or today_iso()
    ctx = build_backlog_context(conn, day)
    if not ctx["tasks"]:
        return "🧹 Backlog's clear — nothing stale to triage."
    prompt = backlog_prompt(ctx)
    runner = claude_fn or (lambda p: call_claude(p, timeout=120))
    try:
        out = (runner(prompt) or "").strip()
    except Exception:
        out = ""
    if not out:
        return fallback_backlog(ctx)
    return "🧹 Backlog triage\n\n" + out


# ── FEATURE 3: evening journal reflection ─────────────────────────────────────
def build_reflection_context(conn, day: str = None, now=None) -> dict:
    """Pure snapshot for the evening reflection: today's completed tasks + captures,
    today's journal so far, and the last 7 days of journal (for the one pattern probe)."""
    day = day or today_iso()
    now = now or now_sg()

    done_rows = conn.execute(
        "SELECT title, category FROM tasks WHERE parent_id IS NULL AND done=1 "
        "AND deleted_at IS NULL AND completed_at=? ORDER BY id", (day,)).fetchall()
    done_today = [{"title": r["title"], "category": r["category"]} for r in done_rows]

    # Cap captured-today titles: list_notes is newest-created first, so this keeps the
    # most recent handful (an import day can create hundreds — don't flood the prompt).
    notes_all = [n["title"] for n in vault_store.list_notes()
                 if (n["created"] or "")[:10] == day]
    notes_today = notes_all[:15]
    notes_overflow = len(notes_all) - len(notes_today)

    tpage = vault_store.read_journal(day)
    today_entries = [f"{e['time']} {e['text']}" for e in tpage["entries"]] if tpage else []
    journaled_today = bool(today_entries)

    week_ago = (_d(day) - timedelta(days=7)).isoformat()
    recent = []
    for dd in sorted(vault_store.list_journal_days(), key=lambda x: x["day"]):
        if week_ago <= dd["day"] < day:
            page = vault_store.read_journal(dd["day"])
            if page and page["entries"]:
                body = "\n".join(f"    {e['time']} {e['text']}" for e in page["entries"])
                recent.append(f"  {dd['day']}:\n{body}")

    text = "\n".join([
        f"TODAY: {day} ({now.strftime('%A')})",
        "",
        "TASKS COMPLETED TODAY:",
        "\n".join(f"- {d['title']}"
                  + (f" [{d['category']}]" if d["category"] else "") for d in done_today)
        or "(none logged)",
        "",
        f"CAPTURED TODAY (notes{f', {len(notes_all)} total — showing 15 newest' if notes_overflow else ''}):",
        "\n".join(f"- {t}" for t in notes_today) or "(none)",
        "",
        "TODAY'S JOURNAL SO FAR:",
        "\n".join("  " + e for e in today_entries) or "  (nothing written yet)",
        "",
        "LAST 7 DAYS OF JOURNAL:",
        "\n".join(recent) or "  (nothing in the last week)",
    ])
    return {"day": day, "done_today": done_today, "notes_today": notes_today,
            "today_entries": today_entries, "journaled_today": journaled_today,
            "recent_journal": recent, "text": text}


def reflection_prompt(ctx: dict) -> str:
    already = ("He has ALREADY journaled today — build on what he wrote above rather "
               "than asking generic questions.\n" if ctx["journaled_today"] else "")
    return (
        "=== vault/profile.md (who Kelvin is — voice + context) ===\n"
        f"{vault_store.read_profile()}\n\n"
        "=== TASK ===\n"
        "You are Kelvin's Life OS assistant writing his EVENING REFLECTION. Below is "
        "what actually happened today plus the last week's journal. Write 2-3 short "
        "reflection prompts (questions) that NAME concrete things from today's data — a "
        "task he finished, a note he captured, a line he wrote. At most ONE prompt may "
        "probe a pattern across the last 7 days. "
        f"{already}"
        "Warm and human, not clinical, not a therapy worksheet. Plain text for "
        "Telegram: no markdown, no headers. Write ONLY the prompts.\n\n"
        "=== LIVE CONTEXT ===\n"
        f"{ctx['text']}\n\n"
        "=== REFLECTION ===\n")


def fallback_reflection(conn=None, day: str = None) -> str:
    """Deterministic warm nudge — used when claude fails."""
    return ("✦ Evening check-in — how did today go? What went well, what drained you, "
            "and what's the one thing you want to carry into tomorrow?")


def evening_reflection(conn, day: str = None, now=None, claude_fn=None) -> str:
    """The 21:30 reflection. Returns Telegram-ready text; falls back to the warm static
    nudge on any claude failure."""
    day = day or today_iso()
    now = now or now_sg()
    ctx = build_reflection_context(conn, day, now)
    prompt = reflection_prompt(ctx)
    runner = claude_fn or (lambda p: call_claude(p, timeout=120))
    try:
        out = (runner(prompt) or "").strip()
    except Exception:
        out = ""
    if not out:
        return fallback_reflection(conn, day)
    return "✦ Evening reflection\n\n" + out
