"""Phase 2 tests — Telegram capture daemon routing, Claude triage application
(three-way: task / note / journal), the ack→classify→outcome reply sequence, the
morning-digest builder, the Change/refile endpoint, and health-dot staleness."""

import os
from datetime import datetime, timedelta, timezone

import capture_daemon as cd
import triage.run_triage as rt
import vault_store
import web_core
from capture import create_task, route_capture
from routes_tasks import today_tasks, purge_deleted
from routes_goals import goal_progress, archive_expired_goals, current_period_start
from db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


# ── a fake Telegram that records what the daemon would send ───────────────────
class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {"ok": True}


# ── voice transcription routing ───────────────────────────────────────────────
def test_voice_unprefixed_lands_unsorted_with_audio(client, tmp_path):
    conn = _db()
    oga = tmp_path / "clip.oga"
    oga.write_bytes(b"fake-audio-bytes")
    result = cd.route_voice(conn, "some rambling thought about the market", str(oga))
    conn.close()
    assert result["kind"] == "note" and "unsorted" in result["tags"]
    note = vault_store.read_note(result["slug"])
    assert note["audio"].endswith(".oga")                    # frontmatter pointer set
    assert os.path.exists(os.path.join(vault_store.audio_dir(), result["slug"] + ".oga"))


def test_voice_spoken_task_prefix_makes_task(client):
    conn = _db()
    result = cd.route_voice(conn, "task buy milk on the way home", None)
    conn.close()
    assert result["kind"] == "task"


# ── triage application (mock claude output) ───────────────────────────────────
def test_triage_converts_unsorted_note_to_task(client):
    conn = _db()
    route_capture(conn, "renew passport before September")     # → #unsorted note
    conn.close()
    unsorted = [n for n in vault_store.list_notes() if "unsorted" in n["tags"]]
    assert len(unsorted) == 1
    slug = unsorted[0]["slug"]

    fake = lambda prompt: (
        '[{"path": "vault/notes/%s.md", "action": "to_task", '
        '"title": "Renew passport", "category": "personal", "due_date": "2026-09-01"}]' % slug)
    conn = _db()
    applied = rt.run(conn, claude_fn=fake)
    conn.close()

    assert any("Tasks" in a for a in applied)
    assert vault_store.read_note(slug) is None                 # note consumed
    conn = _db()
    row = conn.execute("SELECT category, due_date FROM tasks WHERE title='Renew passport'").fetchone()
    ran = conn.execute("SELECT value FROM settings WHERE key='triage_last_ran'").fetchone()
    conn.close()
    assert row["category"] == "personal" and row["due_date"] == "2026-09-01"
    assert ran is not None                                     # heartbeat stamped


def test_triage_three_way_note_and_journal(client):
    conn = _db()
    route_capture(conn, "felt drained today, skipped the gym")   # should become journal
    route_capture(conn, "interesting thread on SG dividend stocks")  # stays a note, retag
    conn.close()
    notes = {n["title"]: n["slug"] for n in vault_store.list_notes() if "unsorted" in n["tags"]}
    jslug = [s for t, s in notes.items() if "drained" in t][0]
    nslug = [s for t, s in notes.items() if "dividend" in t][0]

    fake = lambda prompt: (
        '[{"path":"vault/notes/%s.md","action":"to_journal"},'
        '{"path":"vault/notes/%s.md","action":"retag","tags":["idea","research"]}]'
        % (jslug, nslug))
    conn = _db()
    applied = rt.run(conn, claude_fn=fake)
    conn.close()

    assert any("Journal" in a for a in applied) and any("Notes" in a for a in applied)
    # journal note consumed + appended to today's page
    assert vault_store.read_note(jslug) is None
    page = vault_store.read_journal(today_iso())
    assert page and any("drained" in e["text"] for e in page["entries"])
    # retagged note keeps existing, no longer #unsorted
    retagged = vault_store.read_note(nslug)
    assert "unsorted" not in retagged["tags"] and "idea" in retagged["tags"]


# ── ack → classify → outcome reply sequence ───────────────────────────────────
def test_ack_then_outcome_reply_sequence(client, monkeypatch):
    conn = _db()
    tg = FakeTelegram()
    upd = {"update_id": 1, "message": {"from": {"id": 12345678},
           "chat": {"id": 12345678}, "text": "reply to the sponsor email tomorrow"}}
    due = cd._process_update(conn, tg, "12345678", upd, None)
    # first reply is the instant ack; triage is scheduled
    assert tg.sent[-1][1] == "📥 saved — filing…"
    assert due is not None

    # when triage applies something, the daemon sends the outcome as a follow-up
    monkeypatch.setattr(rt, "run", lambda c: ["'reply to the sponsor email…' → Tasks · business · due 2026-07-10"])
    cd.run_triage_now(conn, tg, 12345678)
    conn.close()
    assert tg.sent[-1][1].startswith("✓ ") and "Tasks" in tg.sent[-1][1]


# ── query mode: intent detection + handlers ───────────────────────────────────
def test_query_intent_detection():
    import queries
    # clear queries
    assert queries.is_query("what are my todos")
    assert queries.is_query("what's on today")
    assert queries.is_query("any overdue?")
    assert queries.is_query("how many tasks today")
    assert queries.is_query("find rate card")
    assert queries.is_query("show me my goals")
    # open questions (free-form tier) — interrogative + '?'
    assert queries.is_query("how was my week?")
    assert queries.is_query("what did I say about the sponsor deal?")
    assert queries.is_query("do I have too much on this week?")
    # ambiguous / captures must NOT be treated as queries (data loss > lost answer)
    assert not queries.is_query("buy milk today")            # a capture, not a query
    assert not queries.is_query("remember what tasks I have")  # imperative → capture
    assert not queries.is_query("goals")                     # bare noun → capture
    assert not queries.is_query("call the editor tomorrow")  # no data noun → capture


def test_query_handlers_output(client):
    conn = _db()
    today = today_iso()
    with conn:
        create_task(conn, "Ship the newsletter", col="week", due_date=today, priority="high")
        create_task(conn, "Old thing", col="week", due_date="2020-01-01")
        conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('2 videos','month','2026-07-01','rollup',NULL,0,?)", (now_iso(),))
    import queries
    todos = queries.answer_query(conn, "what are my todos")
    assert "Ship the newsletter" in todos and "❗" in todos      # high-priority marker
    overdue = queries.answer_query(conn, "any overdue?")
    assert "Old thing" in overdue
    goals = queries.answer_query(conn, "show me my goals")
    assert "2 videos" in goals and ("▓" in goals or "░" in goals)  # text bar
    # note search
    vault_store.create_note(title="Sponsor rate card 2026", body="numbers", tags=["business"])
    found = queries.answer_query(conn, "find rate card")
    assert "Sponsor rate card 2026" in found
    # query-shaped but unmatched by deterministic handlers → None (free-form fallback)
    assert queries.answer_query(conn, "how was my week going?") is None
    conn.close()


def test_freeform_context_builder_and_cap(client):
    import queries
    conn = _db()
    today = today_iso()
    with conn:
        create_task(conn, "Sponsor deal with Moomoo", col="week", due_date=today, category="business")
    vault_store.create_note(title="Moomoo sponsor deal terms", body="They offered X for a dedicated video.",
                            tags=["business"])
    vault_store.create_note(title="Unrelated recipe", body="chicken rice steps", tags=["personal"])
    ctx = queries.build_context(conn, "what did I say about the Moomoo sponsor deal?")
    conn.close()
    # profile + task + the matching note body are present; the unrelated note body is not
    assert "profile.md" in ctx
    assert "Sponsor deal with Moomoo" in ctx
    assert "They offered X for a dedicated video." in ctx      # matched note body included
    assert "chicken rice steps" not in ctx                     # non-matching body excluded
    assert len(ctx) <= 12000                                   # size cap honoured


def test_freeform_answer_mocked_and_timeout(client):
    import queries
    conn = _db()
    with conn:
        create_task(conn, "Edit the REITs video", col="week")
    # mocked claude reply path
    reply = queries.answer_freeform(conn, "what's left to do?",
                                    claude_fn=lambda p: "You still need to edit the REITs video.")
    assert reply and "REITs" in reply
    # timeout / failure path → None (daemon shows a retry message)
    def _boom(p):
        raise TimeoutError("claude timed out")
    assert queries.answer_freeform(conn, "how was my week?", claude_fn=_boom) is None
    conn.close()


def test_freeform_qa_reply_sequence(client, monkeypatch):
    import queries
    conn = _db()
    tg = FakeTelegram()
    monkeypatch.setattr(queries, "answer_freeform", lambda c, q: "Your week looked productive — 3 videos done.")
    upd = {"update_id": 21, "message": {"from": {"id": 12345678},
           "chat": {"id": 12345678}, "text": "how was my week?"}}
    cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()
    # ack first, then the free-form answer; nothing captured
    assert tg.sent[0][1] == "🤔 thinking…"
    assert "productive" in tg.sent[-1][1]
    assert not vault_store.list_notes()


def test_query_message_files_nothing(client):
    conn = _db()
    with conn:
        create_task(conn, "existing", col="week")
    tg = FakeTelegram()
    upd = {"update_id": 9, "message": {"from": {"id": 12345678},
           "chat": {"id": 12345678}, "text": "what are my todos"}}
    cd._process_update(conn, tg, "12345678", upd, None)
    n_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()
    assert n_tasks == 1                                        # query created no task
    assert not vault_store.list_notes()                        # and no note
    assert tg.sent and "📋" in tg.sent[-1][1]                  # it answered with a list


def test_slash_command_not_filed(client):
    conn = _db()
    tg = FakeTelegram()
    upd = {"update_id": 3, "message": {"from": {"id": 12345678},
           "chat": {"id": 12345678}, "text": "/start"}}
    cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()
    assert "send me anything" in tg.sent[-1][1].lower()         # help, not filed
    assert not vault_store.list_notes()                         # nothing captured


def test_unauthorised_sender_ignored(client):
    conn = _db()
    tg = FakeTelegram()
    upd = {"update_id": 2, "message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "hi"}}
    cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()
    assert tg.sent == []                                        # nothing filed or replied


# ── morning digest builder ────────────────────────────────────────────────────
def test_digest_lists_tasks_and_goals(client):
    conn = _db()
    today = today_iso()
    with conn:
        create_task(conn, "Due today thing", col="week", due_date=today)
        create_task(conn, "Overdue thing", col="week", due_date="2020-01-01")
        conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('Newsletter','month','2026-07-01','number',500,438,?)", (now_iso(),))
    text = cd.build_digest(conn)
    conn.close()
    assert "Due today thing" in text and "Overdue thing" in text
    assert "Newsletter" in text and "438/500" in text


def test_digest_sunday_stale_backlog(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Rotting backlog item", col="backlog")
        conn.execute("UPDATE tasks SET updated=? WHERE id=?", ("2026-05-01T00:00:00Z", tid))
    sunday = datetime(2026, 7, 12, 9, 0)                        # a Sunday
    text = cd.build_digest(conn, day="2026-07-12", now=sunday)
    conn.close()
    assert "Stale backlog" in text and "Rotting backlog item" in text
    assert "next week's goals" in text


# ── Change / refile endpoint (three-way) ──────────────────────────────────────
def test_refile_note_to_task(client):
    conn = _db()
    res = route_capture(conn, "misfiled as a note but really a task")
    conn.close()
    slug = res["slug"]
    r = client.post("/capture/refile", data={"kind": "note", "ref": slug, "to": "task"})
    assert r.status_code == 200 and r.get_json()["kind"] == "task"
    assert vault_store.read_note(slug) is None
    conn = _db()
    got = conn.execute("SELECT 1 FROM tasks WHERE title LIKE 'misfiled%'").fetchone()
    conn.close()
    assert got is not None


def test_refile_task_to_journal(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "actually a diary line", col="week")
    conn.close()
    r = client.post("/capture/refile", data={"kind": "task", "ref": str(tid), "to": "journal"})
    assert r.status_code == 200 and r.get_json()["kind"] == "journal"
    conn = _db()
    gone = conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert gone is None
    page = vault_store.read_journal(today_iso())
    assert page and any("diary line" in e["text"] for e in page["entries"])


# ── task soft-delete (undo, not confirmation) ─────────────────────────────────
def test_task_soft_delete_hides_and_restores(client):
    conn = _db()
    today = today_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('vids','month',?,'rollup',NULL,0,?)", (current_period_start("month"), now_iso()))
        gid = cur.lastrowid
        tid = create_task(conn, "Delete me", col="week", due_date=today, goal_id=gid)
        sub = create_task(conn, "a subtask", parent_id=tid)
    conn.close()

    # delete → gone from board, Today, and the goal rollup
    client.post(f"/tasks/{tid}/delete")
    conn = _db()
    board = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL "
        "AND deleted_at IS NULL").fetchone()[0]
    assert board == 0
    assert tid not in {t["id"] for t in today_tasks(conn)}
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    assert goal_progress(conn, g)["total"] == 0                 # deleted task not counted
    subrow = conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (sub,)).fetchone()
    conn.close()
    assert subrow["deleted_at"] is not None                     # subtask followed parent

    # restore → back on the board and in the rollup
    client.post(f"/tasks/{tid}/restore")
    conn = _db()
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    back = conn.execute("SELECT deleted_at FROM tasks WHERE id=?", (tid,)).fetchone()
    total = goal_progress(conn, g)["total"]
    conn.close()
    assert back["deleted_at"] is None and total == 1


def test_task_purge_after_30_days(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Long gone", col="backlog")
        conn.execute("UPDATE tasks SET deleted_at=? WHERE id=?", ("2026-01-01T00:00:00Z", tid))
    purge_deleted(conn)
    row = conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone()
    # a recently-deleted task is NOT purged (still within the 30-day undo window)
    with conn:
        keep = create_task(conn, "Recent delete", col="backlog")
        conn.execute("UPDATE tasks SET deleted_at=? WHERE id=?", (now_iso(), keep))
    purge_deleted(conn)
    kept = conn.execute("SELECT 1 FROM tasks WHERE id=?", (keep,)).fetchone()
    conn.close()
    assert row is None and kept is not None


# ── goal period rollover ──────────────────────────────────────────────────────
def test_expired_week_goal_auto_archives(client):
    conn = _db()
    with conn:
        # a week goal whose Monday start was 8 days ago → period ended
        old_start = "2026-06-29"                                # a Monday well in the past
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('last week','week',?,'rollup',NULL,0,?)", (old_start, now_iso()))
        stale = cur.lastrowid
        # a current-week goal must stay active
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('this week','week',?,'rollup',NULL,0,?)", (current_period_start("week"), now_iso()))
        fresh = cur.lastrowid
    archive_expired_goals(conn)
    a = conn.execute("SELECT archived_at FROM goals WHERE id=?", (stale,)).fetchone()
    b = conn.execute("SELECT archived_at FROM goals WHERE id=?", (fresh,)).fetchone()
    conn.close()
    assert a["archived_at"] is not None and b["archived_at"] is None


# ── health-dot staleness logic ────────────────────────────────────────────────
def test_health_status_ok_stale_off(client):
    conn = _db()
    now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('capture_last_ran',?)", (fresh,))
        conn.execute("INSERT INTO settings(key,value) VALUES('triage_last_ran',?)", (old,))
        # backup_last_ran deliberately absent → 'off'
    status = web_core.health_status(conn, now=now)
    conn.close()
    assert status["capture"] == "ok"       # 2 min < 10 min budget
    assert status["triage"] == "ok"        # 2 h < 26 h budget
    assert status["backup"] == "off"       # never ran


def test_health_status_capture_goes_stale(client):
    conn = _db()
    now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('capture_last_ran',?)", (old,))
    status = web_core.health_status(conn, now=now)
    conn.close()
    assert status["capture"] == "stale"    # 20 min > 10 min budget
