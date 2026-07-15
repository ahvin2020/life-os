"""Router v2 tests — the agentic `claude -p` entry point.

Covers JSON application for every action type (incl. multi + invalid-id → clarify),
the fallback path when claude fails, undo inverse ops + daemon callback handling,
and the raw-log safety rail. The model is always mocked (claude_fn) — no real calls.
"""

import json
import os

from ai import router
from domain import vault_store
from domain.capture import create_task
from core.db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _fn(obj):
    """A fake claude_fn that always returns the given decision as JSON text."""
    return lambda prompt: json.dumps(obj)


def _open_task(conn, title, **kw):
    with conn:
        return create_task(conn, title, col=kw.pop("col", "week"), **kw)


def _capture_fn(box, obj):
    """A fake claude_fn that records the prompt it was handed, then returns obj."""
    def fn(prompt):
        box.append(prompt)
        return json.dumps(obj)
    return fn


# ── security: the AI can never touch the system ───────────────────────────────
def test_call_claude_disables_all_tools_by_default(client, monkeypatch):
    """The single choke point runs `claude -p --tools ""` — no Bash/Write/Edit/Read —
    so no injected instruction from either AI surface can act on the machine."""
    from ai import claude_cli
    seen = {}

    class _P:
        stdout = "{}"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **kw: (seen.__setitem__("argv", argv), _P())[1])
    claude_cli.call_claude("hello")
    argv = seen["argv"]
    assert "-p" in argv and "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""      # "" == all tools disabled
    assert "--dangerously-skip-permissions" not in argv


def test_call_claude_grants_named_tool_only_when_asked(client, monkeypatch):
    from ai import claude_cli
    seen = {}

    class _P:
        stdout = "{}"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **kw: (seen.__setitem__("argv", argv), _P())[1])
    claude_cli.call_claude("hello", tools="Read")
    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == "Read"


def test_no_call_site_ever_grants_web_tools():
    """The perimeter invariant CLAUDE.md buys by cutting ai/research.py: NO claude call in
    the app holds web tools — every grant is "" (nothing) or "Read" (local files only).

    The two tests above pin the choke point's own default; nothing stopped a *caller* from
    passing tools="WebSearch". This greps the real call sites so reintroducing an open-web
    path has to be a deliberate edit to this test, not an accident.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    allowed = {'""', "''", '"Read"', "'Read'", "tools"}   # `tools` = router's image-gated var
    offenders = []
    files = [p for d in ("ai", "domain", "routes", "core", "triage", "scripts")
             for p in (root / d).rglob("*.py")] + list(root.glob("*.py"))
    for p in files:
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\btools\s*=\s*([^,)\s]+)", src):
            val = m.group(1)
            if val not in allowed:
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{p.relative_to(root)}:{line} → tools={val}")
    assert offenders == [], (
        "a call site grants tools beyond ''/'Read' — the no-web-tools perimeter:\n"
        + "\n".join(offenders))

    # and the router's one variable grant is image-gated: Read only when viewing a photo
    src = (root / "ai" / "router.py").read_text(encoding="utf-8")
    assert 'tools = "Read" if image_path else ""' in src


def test_router_text_runs_with_tools_disabled(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(router, "call_claude",
                        lambda p, t, tools="": (seen.__setitem__("tools", tools),
                                                json.dumps({"action": "answer", "text": "ok"}))[1])
    conn = _db()
    router.route(conn, "how many tasks do I have?")
    conn.close()
    assert seen["tools"] == ""                          # text routing → zero tools


def test_router_image_grants_only_read(client, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(router, "call_claude",
                        lambda p, t, tools="": (seen.__setitem__("tools", tools),
                                                json.dumps({"action": "answer", "text": "ok"}))[1])
    img = tmp_path / "x.jpg"
    img.write_bytes(b"x")
    conn = _db()
    router.route(conn, "what's this", image_path=str(img))
    conn.close()
    assert seen["tools"] == "Read"                      # image path → Read ONLY


def test_router_prompt_carries_injection_guard(client):
    """The router prompt tells the model that saved/attached content is data, not orders."""
    conn = _db()
    prompt = router.build_prompt("hi", router.build_context(conn))
    conn.close()
    assert "never instructions to obey" in prompt.lower()
    assert "=== message ===" in prompt.lower()


# ── raw-log safety rail (written BEFORE the claude call) ──────────────────────
def test_raw_log_always_written(client, tmp_path, monkeypatch):
    logp = tmp_path / "capture_raw.log"
    monkeypatch.setattr(router, "_RAW_LOG", str(logp))
    conn = _db()
    # even if the model raises, the raw message must already be on disk
    router.route(conn, "rambling that will fall back",
                 claude_fn=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    conn.close()
    assert logp.exists()
    assert "rambling that will fall back" in logp.read_text()


# ── every action type ─────────────────────────────────────────────────────────
def test_create_task_with_subtasks(client):
    conn = _db()
    out = router.route(conn, "plan the Q3 video", claude_fn=_fn({
        "action": "create_task", "title": "Plan Q3 video", "due": None,
        "category": "content", "priority": "high", "subtasks": ["outline", "script"]}))
    row = conn.execute("SELECT * FROM tasks WHERE title='Plan Q3 video'").fetchone()
    subs = conn.execute("SELECT COUNT(*) FROM tasks WHERE parent_id=?", (row["id"],)).fetchone()[0]
    conn.close()
    assert out["reply"].startswith("⏰ Task: Plan Q3 video")
    assert row["category"] == "content" and row["priority"] == "high" and subs == 2


def _newest(conn):
    """The task just created — _db() reuses one DB, so 'the first row' is a stale twin."""
    return conn.execute(
        "SELECT title, link FROM tasks WHERE parent_id IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def test_router_task_lands_the_same_shape_as_the_deterministic_one(client):
    """Both paths must file a cited url the SAME way — clean title + tasks.link. The AI path
    is where the deterministic ladder DECLINES to ("add task: review this <reel>" bails on
    when-language), so if only one path lifted the url the two would answer the same question
    differently depending on which tier happened to catch it."""
    url = "https://www.instagram.com/reel/EXAMPLE12345/?igsh=EXAMPLETOKEN"

    # the model uses `link` as instructed
    conn = _db()
    router.route(conn, f"add task: review this {url}", claude_fn=_fn({
        "action": "create_task", "title": "Review the invoicing reel", "link": url}))
    row = _newest(conn)
    conn.close()
    assert (row["title"], row["link"]) == ("Review the invoicing reel", url)

    # ...and when it ignores the instruction and buries the url in the title, we still lift it
    conn = _db()
    router.route(conn, f"add task: review this {url}", claude_fn=_fn({
        "action": "create_task", "title": f"Review this {url}"}))
    row = _newest(conn)
    conn.close()
    assert (row["title"], row["link"]) == ("Review this", url)

    # a multi keeps the url with the action it belongs to: the NOTE's reel is not the task's
    conn = _db()
    router.route(conn, f"save {url} and add a task to call mum", claude_fn=_fn({
        "action": "multi", "actions": [
            {"action": "create_note", "title": "Finance reel", "tags": ["link"], "body": url},
            {"action": "create_task", "title": "Call mum", "link": None}]}))
    row = _newest(conn)
    conn.close()
    assert (row["title"], row["link"]) == ("Call mum", None)


def test_create_note(client):
    conn = _db()
    out = router.route(conn, "interesting REIT thread", claude_fn=_fn({
        "action": "create_note", "title": "REIT thread", "tags": ["idea", "research"],
        "body": "worth revisiting"}))
    conn.close()
    assert out["reply"].startswith("📝 Note: REIT thread")
    note = [n for n in vault_store.list_notes() if n["title"] == "REIT thread"][0]
    assert "idea" in note["tags"] and "unsorted" not in note["tags"]


def test_voice_note_carries_audio_pointer(client):
    """A voice note Claude files as a note keeps its original recording so the web
    editor can play it back."""
    conn = _db()
    out = router.route(conn, "remember to renew the passport soon", audio_path="vault/.audio/voice-20260710-171645.oga",
                       claude_fn=_fn({"action": "create_note", "title": "Renew passport",
                                      "tags": ["personal"], "body": "renew the passport soon"}))
    conn.close()
    assert out["reply"].startswith("📝 Note: Renew passport")
    note = [n for n in vault_store.list_notes() if n["title"] == "Renew passport"][0]
    assert note["audio"] == "vault/.audio/voice-20260710-171645.oga"


def test_append_journal(client):
    conn = _db()
    out = router.route(conn, "felt great after the gym", claude_fn=_fn({
        "action": "append_journal", "text": "Felt great after the gym today."}))
    conn.close()
    assert out["reply"] == "✦ Added to today's journal"
    page = vault_store.read_journal(today_iso())
    assert page and any("gym" in e["text"] for e in page["entries"])


def test_complete_task_carries_undo(client):
    conn = _db()
    tid = _open_task(conn, "Publish CPF Life video")
    out = router.route(conn, "mark the cpf video done",
                       claude_fn=_fn({"action": "complete_task", "id": tid}))
    done = conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"]
    conn.close()
    assert out["reply"] == "✓ Done: Publish CPF Life video" and done == 1
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|comp|{tid}"


def test_uncomplete_task(client):
    conn = _db()
    tid = _open_task(conn, "Weekly review")
    from domain.tasks_core import complete_task
    with conn:
        complete_task(conn, tid, True)                          # now done + completed today
    out = router.route(conn, "actually reopen the weekly review",
                       claude_fn=_fn({"action": "uncomplete_task", "id": tid}))
    done = conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"]
    conn.close()
    assert done == 0 and out["reply"].startswith("↩ Reopened")


def test_plan_today_and_undo_keyboard(client):
    conn = _db()
    tid = _open_task(conn, "Edit the intro", col="backlog")
    out = router.route(conn, "do the intro edit today",
                       claude_fn=_fn({"action": "plan_today", "id": tid}))
    planned = conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()["planned_on"]
    conn.close()
    assert planned == today_iso()
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|plan|{tid}"


def test_unplan(client):
    conn = _db()
    tid = _open_task(conn, "Later thing")
    with conn:
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?", (today_iso(), tid))
    out = router.route(conn, "take the later thing off today",
                       claude_fn=_fn({"action": "unplan", "id": tid}))
    planned = conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()["planned_on"]
    conn.close()
    assert planned is None and "Removed from today" in out["reply"]


def test_set_due(client):
    conn = _db()
    tid = _open_task(conn, "Send the invoice")
    out = router.route(conn, "push the invoice to friday",
                       claude_fn=_fn({"action": "set_due", "id": tid, "date": "2026-07-10"}))
    due = conn.execute("SELECT due_date FROM tasks WHERE id=?", (tid,)).fetchone()["due_date"]
    conn.close()
    assert due == "2026-07-10" and out["reply"].startswith("⏰ Send the invoice — due")


def test_rename_task(client):
    conn = _db()
    tid = _open_task(conn, "vague title")
    out = router.route(conn, "rename it to Draft the sponsor reply",
                       claude_fn=_fn({"action": "rename_task", "id": tid, "title": "Draft the sponsor reply"}))
    title = conn.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()["title"]
    conn.close()
    assert title == "Draft the sponsor reply" and "Renamed" in out["reply"]


def test_move_task_undo_has_prev_col(client):
    conn = _db()
    tid = _open_task(conn, "Backlog item", col="backlog")
    out = router.route(conn, "move backlog item to this week",
                       claude_fn=_fn({"action": "move_task", "id": tid, "col": "week"}))
    col = conn.execute("SELECT col FROM tasks WHERE id=?", (tid,)).fetchone()["col"]
    conn.close()
    assert col == "week"
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|move|{tid}|backlog"


def test_delete_task_is_soft_with_undo(client):
    conn = _db()
    tid = _open_task(conn, "Drop me")
    out = router.route(conn, "drop the drop me task",
                       claude_fn=_fn({"action": "delete_task", "id": tid}))
    row = conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["deleted_at"] is not None                        # SOFT delete
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|del|{tid}"


def test_create_goal(client):
    conn = _db()
    out = router.route(conn, "goal: publish 8 videos this month", claude_fn=_fn({
        "action": "create_goal", "title": "8 videos", "period": "month",
        "kind": "number", "target": 8}))
    row = conn.execute("SELECT * FROM goals WHERE title='8 videos'").fetchone()
    conn.close()
    assert row and row["period"] == "month" and row["target_num"] == 8
    assert out["reply"].startswith("🎯 Goal")


def test_update_goal_number(client):
    conn = _db()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('Newsletter','month','2026-07-01','number',500,400,?)", (now_iso(),))
        gid = cur.lastrowid
    out = router.route(conn, "newsletter is at 460",
                       claude_fn=_fn({"action": "update_goal_number", "id": gid, "value": 460}))
    cur_num = conn.execute("SELECT current_num FROM goals WHERE id=?", (gid,)).fetchone()["current_num"]
    conn.close()
    assert cur_num == 460 and "460/500" in out["reply"]


def test_answer_action(client):
    conn = _db()
    out = router.route(conn, "how many videos this week?",
                       claude_fn=_fn({"action": "answer", "text": "You've shipped 2 videos this week."}))
    conn.close()
    assert out["reply"] == "You've shipped 2 videos this week."
    assert not vault_store.list_notes()                         # a question files nothing


def test_clarify_action(client):
    conn = _db()
    out = router.route(conn, "do the thing",
                       claude_fn=_fn({"action": "clarify", "question": "Which thing do you mean?"}))
    conn.close()
    assert out["reply"] == "❓ Which thing do you mean?"


def test_multi_compound_message(client):
    conn = _db()
    tid = _open_task(conn, "Publish CPF video")
    out = router.route(conn, "mark cpf done and remind me to invoice friday", claude_fn=_fn({
        "action": "multi", "actions": [
            {"action": "complete_task", "id": tid},
            {"action": "create_task", "title": "Send invoice", "due": "2026-07-10",
             "category": "business"}]}))
    done = conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"]
    inv = conn.execute("SELECT 1 FROM tasks WHERE title='Send invoice'").fetchone()
    conn.close()
    assert done == 1 and inv is not None
    assert "✓ Done" in out["reply"] and "Send invoice" in out["reply"]
    assert out["applied"] == ["complete_task", "create_task"]


# ── link_goal (goal-link SUGGESTIONS confirmed by Sam) ─────────────────────
def _new_goal(conn, title="Retire by 50"):
    with conn:
        return conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, timeframe, created) "
            "VALUES (?, 'month','2026-07-01','rollup','year',?)", (title, now_iso())).lastrowid


def test_link_goal_applies_and_carries_undo(client):
    conn = _db()
    tid = _open_task(conn, "Set up GIRO")
    gid = _new_goal(conn)
    out = router.route(conn, "link task to goal", claude_fn=_fn(
        {"action": "link_goal", "task_id": tid, "goal_id": gid}))
    linked = conn.execute("SELECT goal_id FROM tasks WHERE id=?", (tid,)).fetchone()["goal_id"]
    conn.close()
    assert linked == gid and out["reply"].startswith("🔗 Linked")
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|link|{tid}|"


def test_link_goal_null_unlinks(client):
    conn = _db()
    gid = _new_goal(conn)
    tid = _open_task(conn, "Wrongly linked")
    with conn:
        conn.execute("UPDATE tasks SET goal_id=? WHERE id=?", (gid, tid))
    out = router.route(conn, "unlink that", claude_fn=_fn(
        {"action": "link_goal", "task_id": tid, "goal_id": None}))
    linked = conn.execute("SELECT goal_id FROM tasks WHERE id=?", (tid,)).fetchone()["goal_id"]
    conn.close()
    assert linked is None and "Unlinked" in out["reply"]
    # undo restores the PRIOR goal_id
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|link|{tid}|{gid}"


def test_link_goal_validates_ids(client):
    conn = _db()
    tid = _open_task(conn, "Real task")
    gid = _new_goal(conn)
    # unknown goal id → clarify, no mutation
    out = router.route(conn, "link it", claude_fn=_fn(
        {"action": "link_goal", "task_id": tid, "goal_id": 999}))
    assert out["reply"].startswith("❓")
    assert conn.execute("SELECT goal_id FROM tasks WHERE id=?", (tid,)).fetchone()["goal_id"] is None
    # unknown task id → clarify
    out2 = router.route(conn, "link it", claude_fn=_fn(
        {"action": "link_goal", "task_id": 888, "goal_id": gid}))
    conn.close()
    assert out2["reply"].startswith("❓")


def test_link_goal_undo_inverse_restores_prior(client):
    conn = _db()
    g1 = _new_goal(conn, "Goal one")
    g2 = _new_goal(conn, "Goal two")
    tid = _open_task(conn, "Movable")
    with conn:
        conn.execute("UPDATE tasks SET goal_id=? WHERE id=?", (g1, tid))
    # relink g1 → g2, capturing g1 as the undo target
    out = router.route(conn, "relink", claude_fn=_fn(
        {"action": "link_goal", "task_id": tid, "goal_id": g2}))
    assert out["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"u|link|{tid}|{g1}"
    router.handle_callback(conn, f"u|link|{tid}|{g1}")
    restored = conn.execute("SELECT goal_id FROM tasks WHERE id=?", (tid,)).fetchone()["goal_id"]
    conn.close()
    assert restored == g1                                        # prior value restored
    # and an empty prev restores to unlinked (NULL)
    conn2 = _db()
    tid2 = _open_task(conn2, "Other")
    with conn2:
        conn2.execute("UPDATE tasks SET goal_id=? WHERE id=?", (g1, tid2))
    router.handle_callback(conn2, f"u|link|{tid2}|")
    v = conn2.execute("SELECT goal_id FROM tasks WHERE id=?", (tid2,)).fetchone()["goal_id"]
    conn2.close()
    assert v is None


# ── photo / image support ─────────────────────────────────────────────────────
def test_router_receives_image_path_and_caption(client):
    """A photo turn puts the absolute image path + a Read-tool instruction + the
    caption into the ONE prompt handed to Claude."""
    conn = _db()
    box = []
    img = "/some/vault/.media/20260709-120000-uABC.jpg"
    out = router.route(conn, "split this between me, WL and Jim — I paid",
                       image_path=img,
                       claude_fn=_capture_fn(box, {"action": "answer", "text": "$51.16 each"}))
    conn.close()
    prompt = box[0]
    assert img in prompt                                        # absolute path passed through
    assert "Read tool" in prompt                                # told to view it first
    assert "split this between me, WL and Jim" in prompt        # caption is the instruction
    assert out["reply"] == "$51.16 each"


def test_photo_no_caption_gets_extract_instruction(client):
    conn = _db()
    box = []
    router.route(conn, "", image_path="/v/.media/x.jpg",
                 claude_fn=_capture_fn(box, {"action": "answer", "text": "ok"}))
    conn.close()
    assert "extract whatever is useful from this image" in box[0]


def test_media_pointer_on_created_note(client):
    """A note created from a photo carries a media: frontmatter pointer to vault/.media."""
    conn = _db()
    img = "/anything/vault/.media/20260709-120000-uZ.jpg"
    router.route(conn, "", image_path=img, claude_fn=_fn({
        "action": "create_note", "title": "Harbour Grill receipt",
        "tags": ["receipt"], "body": "Total $153.47"}))
    conn.close()
    note = [n for n in vault_store.list_notes() if n["title"] == "Harbour Grill receipt"][0]
    assert note["media"] == "vault/.media/20260709-120000-uZ.jpg"
    assert vault_store.read_note(note["slug"])["media"] == note["media"]   # round-trips


def test_note_without_image_has_no_media_pointer(client):
    conn = _db()
    router.route(conn, "plain note", claude_fn=_fn({
        "action": "create_note", "title": "Plain", "tags": [], "body": "text"}))
    conn.close()
    note = [n for n in vault_store.list_notes() if n["title"] == "Plain"][0]
    assert note["media"] == ""


# ── rolling exchange memory (follow-ups: "yes" / "the second one") ────────────-
def test_followup_exchange_memory_yes_resolves(client):
    """The prior (Sam, bot) turn is replayed into the next prompt so a bare 'yes'
    after an offer has the offer to resolve against."""
    conn = _db()
    # Turn 1: the bot answers the split and offers to create collect-money tasks.
    router.route(conn, "split the bill 3 ways", claude_fn=_fn({
        "action": "answer",
        "text": "$51.16 each. Want me to create two collect-money tasks?"}))
    # Turn 2: "yes" — the prior offer must be in the context handed to Claude.
    box = []
    out = router.route(conn, "yes", claude_fn=_capture_fn(box, {
        "action": "multi", "actions": [
            {"action": "create_task", "title": "Collect $51.16 from WL"},
            {"action": "create_task", "title": "Collect $51.16 from Jim"}]}))
    conn.close()
    prompt = box[0]
    assert "RECENT CONVERSATION" in prompt
    assert "split the bill 3 ways" in prompt                    # prior user message
    assert "create two collect-money tasks" in prompt           # prior bot reply
    assert out["applied"] == ["create_task", "create_task"]     # the "yes" acted


def test_exchange_memory_cap_and_persistence(client):
    """Memory keeps only the last _MEM_MAX_PAIRS turns and survives a fresh connection
    (persisted in the settings table)."""
    conn = _db()
    for i in range(13):
        router.route(conn, f"message number {i}",
                     claude_fn=_fn({"action": "answer", "text": f"reply {i}"}))
    conn.close()

    conn2 = _db()                                               # FRESH connection
    row = conn2.execute("SELECT value FROM settings WHERE key=?",
                        (router._MEM_KEY,)).fetchone()
    pairs = router.load_exchanges(conn2)
    conn2.close()
    assert row is not None                                      # persisted, not in-memory
    assert len(pairs) == router._MEM_MAX_PAIRS == 10            # capped to last 10
    assert pairs[-1]["u"] == "message number 12"               # newest kept
    assert pairs[0]["u"] == "message number 3"                 # oldest dropped


def test_exchange_memory_entry_capped(client):
    conn = _db()
    router.route(conn, "x" * 5000, claude_fn=_fn({"action": "answer", "text": "y" * 5000}))
    pairs = router.load_exchanges(conn)
    conn.close()
    assert len(pairs[-1]["u"]) <= router._MEM_ENTRY_CAP
    assert len(pairs[-1]["b"]) <= router._MEM_ENTRY_CAP


def test_long_memo_prompt_asks_for_note_plus_tasks(client):
    conn = _db()
    ctx = router.build_context(conn)
    conn.close()
    p = router.build_prompt("a long rambling memo about the week", ctx, long_memo=True)
    assert "LONG MEMO" in p and "one create_task per concrete action" in p
    # a normal message has no such block
    p2 = router.build_prompt("buy milk", ctx, long_memo=False)
    assert "LONG MEMO" not in p2


def test_conditional_reminder_rule_in_contract():
    assert "Conditional follow-ups" in router._CONTRACT


def test_record_exchange_reply_cap_override(client):
    """The user side is always capped at _MEM_ENTRY_CAP; the bot side honours reply_cap
    so a long list answer from the deterministic tier survives instead of truncating."""
    conn = _db()
    router.record_exchange(conn, "u" * 5000, "b" * 5000, reply_cap=1200)
    pair = router.load_exchanges(conn)[-1]
    conn.close()
    assert len(pair["u"]) == router._MEM_ENTRY_CAP          # user side unchanged
    assert 400 < len(pair["b"]) <= 1200                     # bot side kept up to the larger cap


# ── invalid id → clarify (never mutate the wrong row) ─────────────────────────
def test_invalid_task_id_becomes_clarify(client):
    conn = _db()
    _open_task(conn, "A real task")                             # id 1 exists; ask for 999
    out = router.route(conn, "mark task 999 done",
                       claude_fn=_fn({"action": "complete_task", "id": 999}))
    # nothing was completed
    done_any = conn.execute("SELECT COUNT(*) FROM tasks WHERE done=1").fetchone()[0]
    conn.close()
    assert done_any == 0 and out["reply"].startswith("❓")


def test_invalid_goal_id_becomes_clarify(client):
    conn = _db()
    out = router.route(conn, "set the goal to 5",
                       claude_fn=_fn({"action": "update_goal_number", "id": 42, "value": 5}))
    conn.close()
    assert out["reply"].startswith("❓")


# ── fallback path on claude failure ───────────────────────────────────────────
def test_fallback_on_exception(client):
    conn = _db()
    out = router.route(conn, "a lost thought",
                       claude_fn=lambda p: (_ for _ in ()).throw(TimeoutError("timeout")))
    conn.close()
    assert out["fell_back"] and out["reply"] == router.FALLBACK_REPLY
    unsorted = [n for n in vault_store.list_notes() if "unsorted" in n["tags"]]
    assert any("lost thought" in n["body"] for n in unsorted)   # input preserved as #unsorted


def test_fallback_on_invalid_json_after_retry(client):
    conn = _db()
    calls = []

    def _garbage(p):
        calls.append(1)
        return "sorry, I can't do that as JSON"

    out = router.route(conn, "another lost thought", claude_fn=_garbage)
    conn.close()
    assert len(calls) == 2                                      # one retry before giving up
    assert out["fell_back"]


# ── undo inverse operations ───────────────────────────────────────────────────
def test_handle_callback_inverses(client):
    conn = _db()
    from domain.tasks_core import complete_task
    tid = _open_task(conn, "Undo target")

    # complete → undo reopens
    with conn:
        complete_task(conn, tid, True)
    assert "reopened" in router.handle_callback(conn, f"u|comp|{tid}").lower()
    assert conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"] == 0

    # soft delete → undo restores
    with conn:
        conn.execute("UPDATE tasks SET deleted_at=? WHERE id=?", (now_iso(), tid))
    assert "restored" in router.handle_callback(conn, f"u|del|{tid}").lower()
    assert conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (tid,)).fetchone()["deleted_at"] is None

    # plan → undo unplans
    with conn:
        conn.execute("UPDATE tasks SET planned_on=? WHERE id=?", (today_iso(), tid))
    router.handle_callback(conn, f"u|plan|{tid}")
    assert conn.execute("SELECT planned_on FROM tasks WHERE id=?", (tid,)).fetchone()["planned_on"] is None

    # move → undo restores previous column
    with conn:
        conn.execute("UPDATE tasks SET col='week' WHERE id=?", (tid,))
    router.handle_callback(conn, f"u|move|{tid}|backlog")
    assert conn.execute("SELECT col FROM tasks WHERE id=?", (tid,)).fetchone()["col"] == "backlog"
    conn.close()


def test_handle_callback_garbage_is_safe(client):
    conn = _db()
    assert router.handle_callback(conn, "not-a-callback") == "Nothing to undo."
    assert router.handle_callback(conn, "") == "Nothing to undo."
    conn.close()


def test_recurring_complete_undo_removes_respawn(client):
    """Completing a recurring task spawns its next occurrence; the undo payload must
    carry that respawn id and the Undo tap must soft-delete it (no orphaned copy)."""
    conn = _db()
    tid = _open_task(conn, "Daily standup", recur_rule="daily", due_date=today_iso())
    out = router.route(conn, "done with standup",
                       claude_fn=_fn({"action": "complete_task", "id": tid}))
    cb = out["keyboard"]["inline_keyboard"][0][0]["callback_data"]
    # payload is u|comp|<tid>|<respawn>
    parts = cb.split("|")
    assert parts[:3] == ["u", "comp", str(tid)] and len(parts) == 4 and parts[3].isdigit()
    respawn = int(parts[3])
    assert conn.execute("SELECT deleted_at FROM tasks WHERE id=?",
                        (respawn,)).fetchone()["deleted_at"] is None   # live before undo
    router.handle_callback(conn, cb)
    assert conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"] == 0
    assert conn.execute("SELECT deleted_at FROM tasks WHERE id=?",
                        (respawn,)).fetchone()["deleted_at"] is not None  # respawn removed
    conn.close()


# ── daemon callback wiring ────────────────────────────────────────────────────
def test_daemon_process_callback(client):
    import capture_daemon as cd
    from tests.test_phase2 import FakeTelegram
    from domain.tasks_core import complete_task
    conn = _db()
    tid = _open_task(conn, "Callback task")
    with conn:
        complete_task(conn, tid, True)
    tg = FakeTelegram()
    upd = {"update_id": 5, "callback_query": {
        "id": "cbq1", "from": {"id": 12345678}, "data": f"u|comp|{tid}",
        "message": {"message_id": 88, "chat": {"id": 12345678}}}}
    cd._process_callback(conn, tg, "12345678", upd)
    reopened = conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()["done"]
    conn.close()
    assert reopened == 0                                        # inverse applied
    assert tg.answered and tg.answered[-1][0] == "cbq1"         # tap acknowledged
    assert tg.edited and tg.edited[-1] == (12345678, 88)       # spent button removed


def test_daemon_callback_rejects_unauthorised(client):
    import capture_daemon as cd
    from tests.test_phase2 import FakeTelegram
    conn = _db()
    tid = _open_task(conn, "Protected task")
    tg = FakeTelegram()
    upd = {"update_id": 6, "callback_query": {
        "id": "cbq2", "from": {"id": 999}, "data": f"u|del|{tid}",
        "message": {"message_id": 9, "chat": {"id": 999}}}}
    cd._process_callback(conn, tg, "12345678", upd)
    conn.close()
    assert tg.answered == []                                    # ignored, no action
