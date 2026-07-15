"""Phase 2 tests — Telegram capture daemon routing, Claude triage application
(three-way: task / note / journal), the ack→classify→outcome reply sequence, the
morning-digest builder, the Change/refile endpoint, and health-dot staleness."""

import os
from datetime import datetime, timedelta, timezone

import capture_daemon as cd
from ai import proactive
import triage.run_triage as rt
from domain import vault_store
from core import web_core
from domain.capture import create_task, route_capture
from domain.tasks_core import today_tasks, purge_deleted
from domain.goals_core import goal_progress, archive_expired_goals, current_period_start
from core.db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


# ── a fake Telegram that records what the daemon would send ───────────────────
class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.actions = []
        self.answered = []
        self.edited = []
        self.file_requests = []
        self.downloaded = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))
        return {"ok": True}

    def answer_callback_query(self, callback_id, text=None):
        self.answered.append((callback_id, text))
        return {"ok": True}

    def edit_reply_markup(self, chat_id, message_id):
        self.edited.append((chat_id, message_id))
        return {"ok": True}

    # ── file download (photos) — records the file_id asked for, writes fake bytes ──
    def get_file_path(self, file_id):
        self.file_requests.append(file_id)
        return f"photos/{file_id}.jpg"

    def download_file(self, file_path, dest):
        with open(dest, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        self.downloaded.append((file_path, dest))
        return dest

    def send_document(self, chat_id, path):
        self.uploaded = getattr(self, "uploaded", [])
        self.uploaded.append((chat_id, path))
        return {"ok": True}


# Voice routing note: the live path is `_handle_voice` → `router.route` (audio preserved
# via `_preserve_audio`), covered by tests/test_router.py + tests/test_bughunt.py. The
# old spoken-keyword `route_voice` was removed, so its two tests went with it.


def test_transcribe_passes_language_and_condition(monkeypatch):
    """Regression: auto-detect on short accented clips transcribed English as Malay and
    drifted into repetition loops. transcribe_wav must pin language + disable
    condition_on_previous_text, and default to the medium model."""
    import sys
    import types
    calls = {}

    def _fake_transcribe(wav, path_or_hf_repo=None, language=None,
                         condition_on_previous_text=None):
        calls.update(wav=wav, model=path_or_hf_repo, language=language,
                     cond=condition_on_previous_text)
        return {"text": "  What should I do today?  "}

    monkeypatch.setitem(sys.modules, "mlx_whisper",
                        types.SimpleNamespace(transcribe=_fake_transcribe))
    out = cd.transcribe_wav("/tmp/x.wav")
    assert out == "What should I do today?"                     # stripped
    assert calls["language"] == "en"                            # explicit, not auto-detect
    assert calls["cond"] is False                               # repetition-loop guard
    assert "large-v3" in calls["model"]                         # upgraded for accented clips


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


# ── multi-item messages (one message, several lines → several captures) ───────
def test_split_capture_lines_heuristic():
    """Split ONLY when every non-empty line is a standalone capturable unit — a URL, now
    that there are no text prefixes; a link with a caption or a prose note stays one
    capture (its non-URL lines aren't units)."""
    from domain.capture import split_capture_lines
    # 2+ links → split
    assert split_capture_lines(
        "https://insta.com/reel/A/\nhttps://insta.com/p/B/"
    ) == ["https://insta.com/reel/A/", "https://insta.com/p/B/"]
    # a non-URL line among links → NOT split (only URLs are capture units now)
    assert split_capture_lines("https://insta.com/reel/A/\nkeep this thought") is None
    # link with a caption → NOT split (caption line isn't a unit)
    assert split_capture_lines("great video\nhttps://youtu.be/x") is None
    # prose note with line breaks → NOT split
    assert split_capture_lines("Shopping list:\nmilk\neggs") is None
    # a single line is never split
    assert split_capture_lines("https://insta.com/reel/A/") is None


def test_daemon_multiline_links_route_each(client, monkeypatch):
    """Several links in one message each go through the link handler (own ack+enrichment),
    instead of collapsing into a single link note."""
    seen = []
    monkeypatch.setattr(cd, "_handle_link", lambda conn, tg, cid, text: seen.append(text))
    conn = _db()
    tg = FakeTelegram()
    cd._handle_text(conn, tg, 12345678,
                    "https://www.instagram.com/reel/AAA/\nhttps://www.instagram.com/p/BBB/")
    conn.close()
    assert seen == ["https://www.instagram.com/reel/AAA/", "https://www.instagram.com/p/BBB/"]


# ── v2: the daemon routes text through the agentic router ─────────────────────
def test_daemon_routes_text_through_router(client, monkeypatch):
    """An instruction goes through router.route (ONE claude call); the daemon shows
    a typing indicator and relays the router's reply + inline keyboard verbatim."""
    from ai import router
    conn = _db()
    tg = FakeTelegram()
    monkeypatch.setattr(router, "route", lambda c, t, source="telegram", **kw: {
        "reply": "✓ Done: Publish CPF Life video",
        "keyboard": {"inline_keyboard": [[{"text": "↩ Undo", "callback_data": "u|comp|7"}]]},
        "fell_back": False, "applied": ["complete_task"]})
    upd = {"update_id": 1, "message": {"from": {"id": 12345678},
           "chat": {"id": 12345678}, "text": "mark the cpf video done"}}
    due = cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()
    assert ("typing" in [a for _, a in tg.actions])            # typing shown, no ack text
    assert tg.sent[-1][1] == "✓ Done: Publish CPF Life video"  # router reply relayed
    assert tg.sent[-1][2]["inline_keyboard"][0][0]["text"] == "↩ Undo"  # keyboard passed
    assert due is None                                          # no fallback → no sweep scheduled


def test_daemon_fallback_schedules_sweep(client, monkeypatch):
    """When the router falls back (claude down), the daemon schedules a sweep."""
    from ai import router
    conn = _db()
    tg = FakeTelegram()
    monkeypatch.setattr(router, "route", lambda c, t, source="telegram", **kw: {
        "reply": router.FALLBACK_REPLY, "keyboard": None, "fell_back": True,
        "applied": ["fallback_note"]})
    upd = {"update_id": 2, "message": {"from": {"id": 12345678},
           "chat": {"id": 12345678}, "text": "some ambiguous rambling"}}
    due = cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()
    assert due is not None                                      # sweep scheduled


# ── photo capture (download highest-res → vault/.media → same router) ──────────
def test_photo_download_saves_highres_and_routes(client, monkeypatch):
    """A photo message: download the LARGEST size to vault/.media/, show typing, and
    route the caption + image path through the SAME router (one claude call)."""
    import glob
    from ai import router
    conn = _db()
    tg = FakeTelegram()
    seen = {}

    def fake_route(c, message, source="telegram", image_path=None):
        seen["message"], seen["image_path"], seen["source"] = message, image_path, source
        return {"reply": "🧾 $51.16 each — want two collect tasks?", "keyboard": None,
                "fell_back": False, "applied": ["answer"]}

    monkeypatch.setattr(router, "route", fake_route)
    upd = {"update_id": 20, "message": {
        "from": {"id": 12345678}, "chat": {"id": 12345678},
        "caption": "split this between me, WL and Jim — I paid",
        "photo": [
            {"file_id": "thumb", "file_unique_id": "uABC", "width": 90, "height": 120},
            {"file_id": "biggest", "file_unique_id": "uABC", "width": 1280, "height": 1707}]}}
    due = cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()

    from domain import vault_store
    saved = glob.glob(os.path.join(vault_store.media_dir(), "*-uABC.jpg"))
    assert tg.file_requests == ["biggest"]                      # highest-res size requested
    assert saved and os.path.exists(saved[0])                   # written into vault/.media/
    assert "typing" in [a for _, a in tg.actions]               # typing shown
    assert seen["image_path"] == saved[0]                       # router got the saved path
    assert "split this between me" in seen["message"]           # caption is the instruction
    assert tg.sent[-1][1].startswith("🧾")                      # router reply relayed
    assert due is None                                          # no fallback → no sweep


def test_image_document_also_routes_as_photo(client, monkeypatch):
    """An image sent 'as file' (document, image/* mime) is treated like a photo."""
    from ai import router
    conn = _db()
    tg = FakeTelegram()
    seen = {}
    monkeypatch.setattr(router, "route", lambda c, m, source="telegram", image_path=None:
                        seen.update(image_path=image_path) or {
                            "reply": "ok", "keyboard": None, "fell_back": False, "applied": []})
    upd = {"update_id": 21, "message": {
        "from": {"id": 12345678}, "chat": {"id": 12345678},
        "document": {"file_id": "docimg", "file_unique_id": "uDOC",
                     "mime_type": "image/png", "file_name": "receipt.png"}}}
    cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()
    assert tg.file_requests == ["docimg"]
    assert seen["image_path"] and seen["image_path"].endswith("-uDOC.jpg")


def test_maybe_send_document_uploads_multiple_and_single(client):
    """The lookup loop's multi-file fetch delivers via out['documents'] (a list); the legacy
    find_document delivers via out['document'] (single). _maybe_send_document sends ALL, always
    to the allowlisted chat — never a recipient the model chose."""
    tg = FakeTelegram()
    cd._maybe_send_document(tg, "999", {"documents": ["/x/a.pdf", "/x/b.pdf"], "document": "/x/c.pdf"})
    assert getattr(tg, "uploaded", []) == [("999", "/x/a.pdf"), ("999", "/x/b.pdf"), ("999", "/x/c.pdf")]
    # nothing to send → no upload, no crash
    tg2 = FakeTelegram()
    cd._maybe_send_document(tg2, "999", {"reply": "just text"})
    assert getattr(tg2, "uploaded", []) == []


def test_non_image_document_saved_as_note(client):
    """A non-image document (PDF/Word) can't be viewed by Claude, so it's downloaded and
    filed directly as a note with the file attached — no vision call, real filename kept."""
    from domain import vault_store
    conn = _db()
    tg = FakeTelegram()
    upd = {"update_id": 22, "message": {
        "from": {"id": 12345678}, "chat": {"id": 12345678},
        "caption": "the jan invoice",
        "document": {"file_id": "pdf1", "file_unique_id": "uPDF",
                     "mime_type": "application/pdf", "file_name": "invoice.pdf"}}}
    cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()
    assert tg.file_requests == ["pdf1"]                        # downloaded
    assert "📎 Saved" in tg.sent[-1][1]
    # a note now exists carrying the media pointer to the saved PDF
    notes = [n for n in vault_store.list_notes() if n.get("media")]
    assert notes and notes[0]["media"].endswith("__invoice.pdf")
    assert vault_store.media_items(notes[0]["media"])[0] == {
        "name": "invoice.pdf",
        "url": "/media/" + notes[0]["media"].split("/")[-1],
        "is_image": False}


# ── query mode: intent detection + handlers ───────────────────────────────────
def test_query_intent_detection():
    from domain import queries
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
    from domain import queries
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


def test_queries_journal_topic_falls_through(client):
    """'what did I write about X' is a PAST recall question — it must NOT be answered
    with today's journal; it falls through (None) to the router's vault_recall."""
    from domain import queries
    conn = _db()
    assert queries.answer_query(conn, "what did I write about the reno?") is None
    conn.close()


def test_query_answer_recorded_with_large_cap(client):
    """A long task-list answer is recorded (so 'complete the second one' resolves) and
    is NOT truncated at the default 400-char cap."""
    from ai import router
    conn = _db()
    with conn:
        for i in range(15):
            create_task(conn, f"Task number {i} with a fairly long descriptive title",
                        col="week", due_date=today_iso())
    tg = FakeTelegram()
    cd._handle_text_single(conn, tg, "12345678", "what are my tasks today")
    pair = router.load_exchanges(conn)[-1]
    conn.close()
    assert len(pair["b"]) > 400                             # not truncated at the small cap
    assert "Task number 14" in pair["b"]                    # the last item survived


def test_triage_fastpath_records_exchange(client, monkeypatch):
    from ai import router, proactive as pro
    conn = _db()
    monkeypatch.setattr(pro, "backlog_triage", lambda c: "🧹 Triage: nothing stale.")
    tg = FakeTelegram()
    cd._handle_text_single(conn, tg, "12345678", "triage my backlog")
    pair = router.load_exchanges(conn)[-1]
    conn.close()
    assert pair["u"] == "triage my backlog"
    assert "Triage" in pair["b"]


def test_deterministic_capture_records_exchange(client):
    """The CAPTURE branch of the daemon's deterministic ladder must record the exchange too.
    (The old prefix-based cover for this died with the `t:` prefixes; the surviving tests
    only pin the *answer* and *triage* branches.) Without it, "rename it" / "complete the
    second one" after a plain capture silently stop resolving — a failure with no exception,
    so only a test catches it."""
    from ai import router
    conn = _db()
    tg = FakeTelegram()
    cd._handle_text_single(conn, tg, "12345678", "buy milk")
    pair = router.load_exchanges(conn)[-1]
    conn.close()
    assert pair["u"] == "buy milk"
    assert "milk" in pair["b"]                          # the reply names the task it made


def test_daemon_reminder_capture_is_instant(client, monkeypatch):
    """A phone-side timed reminder is parsed deterministically — same ladder as the web —
    and its reply mirrors the router's set_reminder wording, so a parsed reminder is
    indistinguishable from one Claude resolved. It must never reach claude."""
    from ai import router
    conn = _db()

    def _boom(*a, **k):
        raise AssertionError("a parseable reminder must not reach the router")
    monkeypatch.setattr(router, "route", _boom)

    tg = FakeTelegram()
    cd._handle_text_single(conn, tg, "12345678", "remind me to call the bank at 3pm")
    conn.close()
    reply = tg.sent[-1][1]
    assert reply.startswith("⏰ Reminder set — ") and "call the bank" in reply


def test_followup_sees_query_list(client):
    """After the query fast path records the list, the router's next-turn context carries
    it — the material an ordinal follow-up ('the second one') resolves against."""
    from ai import router
    conn = _db()
    with conn:
        create_task(conn, "Alpha task", col="week", due_date=today_iso())
        create_task(conn, "Bravo task", col="week", due_date=today_iso())
    tg = FakeTelegram()
    cd._handle_text_single(conn, tg, "12345678", "what are my tasks today")
    ctx = router.build_context(conn)
    conn.close()
    # The recorded list is replayed into the router's context under RECENT CONVERSATION.
    assert "RECENT CONVERSATION" in ctx["text"]
    assert "Alpha task" in ctx["text"]


def test_open_question_goes_to_router_answer(client, monkeypatch):
    """An open question with no deterministic handler goes to the router, whose
    `answer` action replies inline — the SINGLE claude entry point (no separate Q&A)."""
    from ai import router
    conn = _db()
    tg = FakeTelegram()
    monkeypatch.setattr(router, "route", lambda c, t, source="telegram", **kw: {
        "reply": "Your week looked productive — 3 videos done.",
        "keyboard": None, "fell_back": False, "applied": ["answer"]})
    upd = {"update_id": 21, "message": {"from": {"id": 12345678},
           "chat": {"id": 12345678}, "text": "how was my week?"}}
    cd._process_update(conn, tg, "12345678", upd, None)
    conn.close()
    assert "productive" in tg.sent[-1][1]
    assert not vault_store.list_notes()                         # a question files nothing


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
    text = proactive.build_digest(conn)
    conn.close()
    assert "Due today thing" in text and "Overdue thing" in text
    assert "Newsletter" in text and "438/500" in text


def test_digest_sunday_stale_backlog(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Rotting backlog item", col="backlog")
        conn.execute("UPDATE tasks SET updated=? WHERE id=?", ("2026-05-01T00:00:00Z", tid))
    sunday = datetime(2026, 7, 12, 9, 0)                        # a Sunday
    text = proactive.build_digest(conn, day="2026-07-12", now=sunday)
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
