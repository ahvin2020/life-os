"""Agentic router — the ONE `claude -p` entry point for the Telegram bot (v2).

Every unprefixed message (text, or voice after local transcription) is handed to
route(): it builds compact LIVE context (open tasks with ids, goals, today, journal
count), asks the local Claude CLI for a STRICT JSON action, validates every id
against that context, and applies the action through the existing capture / task /
goal helpers. It doesn't just FILE words any more — it ACTS ("mark the CPF video
done", "push the invoice to Friday", "how many videos this week?").

Safety rails (non-negotiable):
  1. The raw message is appended to data/capture_raw.log BEFORE the claude call, so
     input is never lost even if everything downstream fails.
  2. If claude fails/times out or returns invalid JSON after one retry, we fall back
     to the old behaviour — save as an #unsorted note (route_capture) and let the
     daemon schedule a sweep — and the reply says so.
  3. delete_task is the existing SOFT delete; the reply carries an inline Undo.
  4. Only ids present in the provided context may be referenced; an unknown id is
     downgraded to a clarify question rather than mutating the wrong row.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

import capture
import vault_store
from claude_cli import call_claude
from db import now_iso, today_iso, now_sg

_ROOT = os.path.dirname(os.path.abspath(__file__))
_RAW_LOG = os.path.join(_ROOT, "data", "capture_raw.log")
_PROFILE_PATH = os.path.join(_ROOT, "vault", "profile.md")

CLAUDE_TIMEOUT = 60

FALLBACK_REPLY = "📥 Saved to your inbox — couldn't reach my brain just now, so I'll sort it on the next sweep."

_CATEGORIES = ("content", "business", "personal")
_PRIORITIES = ("high", "med", "low")
_COLUMNS = ("backlog", "week", "done")
_PERIODS = ("week", "month")
_GOAL_KINDS = ("rollup", "number")


# ── raw capture log (safety rail #1) ──────────────────────────────────────────
def log_raw(message: str, source: str = "telegram") -> None:
    """Append the raw inbound message to data/capture_raw.log. Best-effort; never
    raises — a logging failure must not block capture."""
    try:
        os.makedirs(os.path.dirname(_RAW_LOG), exist_ok=True)
        with open(_RAW_LOG, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()}\t{source}\t{(message or '').strip()}\n")
    except OSError:
        pass


# ── live context ──────────────────────────────────────────────────────────────
def build_context(conn) -> dict:
    """Compact snapshot the model routes against: open tasks (with ids), goals (with
    ids + progress), today's date/day, today's journal count, and a little recent
    history so the `answer` action has something to answer from. Returns a dict with
    a prompt-ready `text` plus the id sets used to validate the model's output."""
    from routes_goals import goal_progress
    today = today_iso()
    now = now_sg()

    task_rows = conn.execute(
        "SELECT id, title, col, due_date, category, priority FROM tasks "
        "WHERE parent_id IS NULL AND archived_at IS NULL AND deleted_at IS NULL AND done=0 "
        "ORDER BY col, sort_order, id").fetchall()
    tasks = {}
    task_lines = []
    for r in task_rows:
        tasks[r["id"]] = {"title": r["title"], "col": r["col"], "due": r["due_date"]}
        bits = [f"col={r['col']}"]
        if r["due_date"]:
            bits.append(f"due {r['due_date']}")
        if r["category"]:
            bits.append(r["category"])
        if r["priority"]:
            bits.append(r["priority"])
        task_lines.append(f"- #{r['id']} {r['title']} [{', '.join(bits)}]")

    goal_rows = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL ORDER BY period, created").fetchall()
    goal_ids = set()
    goal_lines = []
    for g in goal_rows:
        goal_ids.add(g["id"])
        p = goal_progress(conn, g)
        if g["kind"] == "number":
            prog = f"{int(p.get('current', 0))}/{int(p.get('target', 0))}"
        else:
            prog = f"{p.get('done', 0)}/{p.get('total', 0)} tasks"
        goal_lines.append(f"- #{g['id']} {g['title']} ({g['period']}, {g['kind']}) {prog}")

    # Completed-today: shown WITH ids so "reopen X" / references resolve, and added to
    # the valid-id set so uncomplete/move/etc. can target a just-finished task.
    done_rows = conn.execute(
        "SELECT id, title FROM tasks WHERE parent_id IS NULL AND done=1 AND deleted_at IS NULL "
        "AND completed_at=? ORDER BY id", (today,)).fetchall()
    done_today = []
    for r in done_rows:
        tasks[r["id"]] = {"title": r["title"], "col": "done", "due": None}
        done_today.append(f"#{r['id']} {r['title']}")

    jpage = vault_store.read_journal(today)
    jentries = jpage["entries"] if jpage else []
    jcount = len(jentries)

    profile = ""
    if os.path.exists(_PROFILE_PATH):
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            profile = f.read()

    lines = [
        f"TODAY: {today} ({now.strftime('%A')}). Journal entries logged today: {jcount}.",
        "",
        "OPEN TASKS (reference tasks ONLY by these #ids):",
        "\n".join(task_lines) or "(none)",
        "",
        "GOALS (reference goals ONLY by these #ids):",
        "\n".join(goal_lines) or "(none)",
        "",
        "COMPLETED TODAY: " + (", ".join(done_today) or "(none)"),
    ]
    if jentries:
        lines += ["", "TODAY'S JOURNAL:"] + [f"  {e['time']} {e['text'][:160]}" for e in jentries]

    return {
        "text": "\n".join(lines),
        "profile": profile,
        "task_ids": set(tasks.keys()),
        "goal_ids": goal_ids,
        "tasks": tasks,
        "today": today,
    }


# ── prompt ──────────────────────────────────────────────────────────────────--
_CONTRACT = """\
You are Kelvin's Life OS assistant. He sends you ONE message; decide what he wants
and reply with ONE JSON object (no prose, no code fences). Don't just file words —
ACT on instructions. Pick the single best action:

create_task    {"action":"create_task","title":str,"due":ISO-date|null,"category":"content|business|personal"|null,"priority":"high|med|low"|null,"subtasks":[str,...]}
create_note    {"action":"create_note","title":str,"tags":[str,...],"body":str}
append_journal {"action":"append_journal","text":str}           # past-tense reflection about his day
complete_task  {"action":"complete_task","id":int}              # "mark X done", "finished X"
uncomplete_task{"action":"uncomplete_task","id":int}
plan_today     {"action":"plan_today","id":int}                 # "do X today", "put X on today"
unplan         {"action":"unplan","id":int}
set_due        {"action":"set_due","id":int,"date":ISO-date}    # "push X to Friday", "X is due next week"
rename_task    {"action":"rename_task","id":int,"title":str}
move_task      {"action":"move_task","id":int,"col":"backlog|week|done"}
delete_task    {"action":"delete_task","id":int}                # "drop X", "remove X"
create_goal    {"action":"create_goal","title":str,"period":"week|month","kind":"rollup|number","target":number|null}
update_goal_number {"action":"update_goal_number","id":int,"value":number}   # "newsletter is at 450"
answer         {"action":"answer","text":str}                   # a QUESTION about his data — answer from the context below
clarify        {"action":"clarify","question":str}              # genuinely ambiguous — ask one short question
multi          {"action":"multi","actions":[ ...two or more of the above... ]}   # compound message

Rules:
- Reference tasks/goals ONLY by the #ids in the context. If he means a task/goal you
  can't find in the context, use clarify — NEVER guess an id.
- Dates are ISO YYYY-MM-DD in Asia/Singapore. "tomorrow"/"Friday"/"next week" → resolve
  against TODAY in the context.
- Actionable ("reply to the sponsor", "renew passport") → create_task. Past-tense
  reflection ("felt drained, skipped gym") → append_journal. Reference/idea/link to
  keep → create_note. A question ("how many videos this week?", "am I overloaded?")
  → answer, using the context; if the context doesn't say, say so plainly.
- Use multi for compound messages ("mark cpf done and remind me to invoice friday").
- Output ONLY the JSON object.
"""


def build_prompt(message: str, ctx: dict) -> str:
    return (
        "=== vault/profile.md (who Kelvin is — classification context) ===\n"
        f"{ctx['profile']}\n\n"
        "=== ACTION CONTRACT ===\n"
        f"{_CONTRACT}\n\n"
        "=== LIVE CONTEXT ===\n"
        f"{ctx['text']}\n\n"
        f"=== MESSAGE ===\n{(message or '').strip()}\n\n=== JSON ===\n")


# ── parsing ──────────────────────────────────────────────────────────────────-
def parse_obj(raw):
    """Extract the JSON action object from Claude's output (tolerates fences/prose).
    Returns a dict, or None if nothing parseable."""
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
    return obj if isinstance(obj, dict) else None


def _decide(runner, prompt):
    """Call the model, retrying once, until we get a parseable object. None on failure."""
    for _ in range(2):
        try:
            raw = runner(prompt)
        except Exception:
            raw = None
        obj = parse_obj(raw)
        if obj is not None:
            return obj
    return None


# ── helpers ──────────────────────────────────────────────────────────────────-
def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _due_label(d: str, today: str) -> str:
    if not d:
        return ""
    try:
        delta = (date.fromisoformat(d) - date.fromisoformat(today)).days
    except ValueError:
        return d
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 2 <= delta <= 6:
        return date.fromisoformat(d).strftime("%a")
    if delta < 0:
        return f"{d} (overdue)"
    return d


def _title(conn, ctx, tid):
    t = ctx["tasks"].get(tid)
    if t:
        return t["title"]
    r = conn.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
    return r["title"] if r else f"#{tid}"


def _undo_kb(callback_data: str) -> dict:
    return {"inline_keyboard": [[{"text": "↩ Undo", "callback_data": callback_data}]]}


# ── applying a single action ─────────────────────────────────────────────────-
def apply_action(conn, act, ctx) -> tuple:
    """Apply one action dict. Returns (reply_text, undo_callback_data|None).
    Unknown/invalid ids become a clarify reply — never a wrong-row mutation."""
    if not isinstance(act, dict):
        return ("🤔 I didn't understand that — could you rephrase?", None)
    kind = act.get("action")
    today = ctx["today"]

    # --- actions that need a valid TASK id ---
    if kind in ("complete_task", "uncomplete_task", "plan_today", "unplan",
                "set_due", "rename_task", "move_task", "delete_task"):
        tid = _as_int(act.get("id"))
        if tid is None or tid not in ctx["task_ids"]:
            return ("❓ I couldn't find that task — which one did you mean?", None)
        title = _title(conn, ctx, tid)
        if kind == "complete_task":
            from routes_tasks import complete_task
            with conn:
                complete_task(conn, tid, True)
            return (f"✓ Done: {title}", f"u|comp|{tid}")
        if kind == "uncomplete_task":
            from routes_tasks import complete_task
            with conn:
                complete_task(conn, tid, False)
            return (f"↩ Reopened: {title}", None)
        if kind == "plan_today":
            with conn:
                conn.execute("UPDATE tasks SET planned_on=?, updated=? WHERE id=?",
                             (today, now_iso(), tid))
            return (f"☀ Planned for today: {title}", f"u|plan|{tid}")
        if kind == "unplan":
            with conn:
                conn.execute("UPDATE tasks SET planned_on=NULL, updated=? WHERE id=?",
                             (now_iso(), tid))
            return (f"Removed from today: {title}", None)
        if kind == "set_due":
            d = (act.get("date") or "").strip() or None
            with conn:
                conn.execute("UPDATE tasks SET due_date=?, updated=? WHERE id=?",
                             (d, now_iso(), tid))
            lbl = _due_label(d, today) if d else "no date"
            return (f"⏰ {title} — due {lbl}", None)
        if kind == "rename_task":
            new = (act.get("title") or "").strip()
            if not new:
                return ("❓ Rename to what?", None)
            with conn:
                conn.execute("UPDATE tasks SET title=?, updated=? WHERE id=?",
                             (new, now_iso(), tid))
            return (f"✏️ Renamed: {new}", None)
        if kind == "move_task":
            col = act.get("col")
            if col not in _COLUMNS:
                return ("❓ Move to backlog, week, or done?", None)
            prev = ctx["tasks"][tid]["col"]
            with conn:
                conn.execute("UPDATE tasks SET col=?, updated=? WHERE id=?",
                             (col, now_iso(), tid))
            return (f"→ Moved to {col}: {title}", f"u|move|{tid}|{prev}")
        if kind == "delete_task":
            ts = now_iso()
            with conn:                       # SOFT delete (undo, not confirm)
                conn.execute("UPDATE tasks SET deleted_at=?, updated=? WHERE id=? OR parent_id=?",
                             (ts, ts, tid, tid))
            return (f"🗑 Deleted: {title}", f"u|del|{tid}")

    # --- goal number update needs a valid GOAL id ---
    if kind == "update_goal_number":
        gid = _as_int(act.get("id"))
        if gid is None or gid not in ctx["goal_ids"]:
            return ("❓ I couldn't find that goal — which one?", None)
        val = act.get("value")
        try:
            val = float(val)
        except (TypeError, ValueError):
            return ("❓ Update the goal to what number?", None)
        with conn:
            conn.execute("UPDATE goals SET current_num=? WHERE id=?", (val, gid))
        row = conn.execute("SELECT title, target_num FROM goals WHERE id=?", (gid,)).fetchone()
        tgt = f"/{int(row['target_num'])}" if row["target_num"] else ""
        return (f"🎯 {row['title']}: {int(val)}{tgt}", None)

    # --- creates (no id validation needed) ---
    if kind == "create_task":
        title = (act.get("title") or "").strip()
        if not title:
            return ("❓ What's the task?", None)
        cat = act.get("category") if act.get("category") in _CATEGORIES else None
        pri = act.get("priority") if act.get("priority") in _PRIORITIES else None
        due = (act.get("due") or "").strip() or None
        subs = [s for s in (act.get("subtasks") or []) if isinstance(s, str) and s.strip()]
        with conn:
            tid = capture.create_task(conn, title, col="week", priority=pri,
                                      category=cat, due_date=due)
            for s in subs:
                capture.create_task(conn, s.strip(), parent_id=tid)
        bits = []
        if due:
            bits.append("due " + _due_label(due, today))
        if cat:
            bits.append(cat)
        if pri == "high":
            bits.append("high")
        if subs:
            bits.append(f"+{len(subs)} subtask" + ("s" if len(subs) > 1 else ""))
        tail = (" — " + " · ".join(bits)) if bits else ""
        return (f"⏰ Task: {title}{tail}", None)

    if kind == "create_note":
        title = (act.get("title") or "").strip()
        body = act.get("body") or title
        tags = [str(t).lstrip("#") for t in (act.get("tags") or []) if str(t).strip()]
        if not title:
            title = (body.strip().splitlines()[0] if body.strip() else "Note")[:60]
        note = vault_store.create_note(title=title, body=body, tags=tags)
        tag_str = " ".join("#" + t for t in tags)
        return (f"📝 Note: {note['title']}" + (f" · {tag_str}" if tag_str else ""), None)

    if kind == "append_journal":
        text = (act.get("text") or "").strip()
        if not text:
            return ("❓ What should I write in the journal?", None)
        vault_store.append_journal_entry(today, text, source="")
        return ("📝 → today's Journal", None)

    if kind == "create_goal":
        from routes_goals import current_period_start
        title = (act.get("title") or "").strip()
        if not title:
            return ("❓ What's the goal?", None)
        period = act.get("period") if act.get("period") in _PERIODS else "week"
        gkind = act.get("kind") if act.get("kind") in _GOAL_KINDS else "rollup"
        target = None
        if gkind == "number":
            try:
                target = float(act.get("target"))
            except (TypeError, ValueError):
                target = None
        with conn:
            conn.execute(
                "INSERT INTO goals (title, period, period_start, kind, target_num, "
                "current_num, created) VALUES (?,?,?,?,?,0,?)",
                (title, period, current_period_start(period), gkind, target, now_iso()))
        return (f"🎯 Goal ({period}): {title}", None)

    if kind == "answer":
        return ((act.get("text") or "").strip() or "🤔 I'm not sure.", None)

    if kind == "clarify":
        return ("❓ " + ((act.get("question") or "").strip() or "Could you clarify that?"), None)

    return ("🤔 I didn't understand that — could you rephrase?", None)


def apply_result(conn, obj, ctx) -> dict:
    """Apply the model's decision (single or multi) and build the daemon reply."""
    if obj.get("action") == "multi":
        actions = obj.get("actions") or []
        replies, applied = [], []
        for a in actions:
            reply, _undo = apply_action(conn, a, ctx)
            replies.append(reply)
            applied.append(a.get("action") if isinstance(a, dict) else "?")
        return {"reply": "\n".join(replies) or "🤔 Nothing to do.",
                "keyboard": None, "fell_back": False, "applied": applied}
    reply, undo = apply_action(conn, obj, ctx)
    return {"reply": reply, "keyboard": _undo_kb(undo) if undo else None,
            "fell_back": False, "applied": [obj.get("action")]}


# ── the one entry point ──────────────────────────────────────────────────────-
def route(conn, message, source: str = "telegram", claude_fn=None) -> dict:
    """Route ONE message through Claude and act on it. Returns
    {reply, keyboard, fell_back, applied}. On claude failure/invalid JSON (after one
    retry) falls back to an #unsorted note and flags fell_back=True."""
    log_raw(message, source)                                  # safety rail #1
    ctx = build_context(conn)
    prompt = build_prompt(message, ctx)
    runner = claude_fn or (lambda p: call_claude(p, CLAUDE_TIMEOUT))
    obj = _decide(runner, prompt)
    if obj is None:                                           # safety rail #2
        capture.route_capture(conn, message, source=source)   # save as #unsorted note
        return {"reply": FALLBACK_REPLY, "keyboard": None, "fell_back": True,
                "applied": ["fallback_note"]}
    return apply_result(conn, obj, ctx)


# ── inline-keyboard Undo (inverse operations) ────────────────────────────────-
def handle_callback(conn, data: str) -> str:
    """Apply the inverse of a prior action from an inline-keyboard Undo tap.
    callback_data formats: u|comp|<id>, u|del|<id>, u|plan|<id>, u|move|<id>|<prevcol>."""
    parts = (data or "").split("|")
    if len(parts) < 3 or parts[0] != "u":
        return "Nothing to undo."
    op, tid = parts[1], _as_int(parts[2])
    if tid is None:
        return "Nothing to undo."
    if op == "comp":
        from routes_tasks import complete_task
        with conn:
            complete_task(conn, tid, False)
        return "↩ Undone — task reopened."
    if op == "del":
        with conn:
            conn.execute("UPDATE tasks SET deleted_at=NULL, updated=? WHERE id=? OR parent_id=?",
                         (now_iso(), tid, tid))
        return "↩ Undone — task restored."
    if op == "plan":
        with conn:
            conn.execute("UPDATE tasks SET planned_on=NULL, updated=? WHERE id=?",
                         (now_iso(), tid))
        return "↩ Undone — removed from today."
    if op == "move" and len(parts) >= 4 and parts[3] in _COLUMNS:
        with conn:
            conn.execute("UPDATE tasks SET col=?, updated=? WHERE id=?",
                         (parts[3], now_iso(), tid))
        return f"↩ Undone — moved back to {parts[3]}."
    return "Nothing to undo."
