"""Unified retrieval brain: Gmail search, vault-note down-ranking, instant-vs-deep
answer, and the router `lookup` action."""

import os

from core.db import connect
from domain import retrieve, docs, vault_store
from ai import router, google_client


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


class _GmailStub:
    def users(self): return self
    def messages(self): return self
    def list(self, userId=None, q=None, maxResults=None): self._mode = "list"; return self
    def get(self, userId=None, id=None, format=None, metadataHeaders=None): self._mode = "get"; return self
    def execute(self):
        if self._mode == "list":
            return {"messages": [{"id": "1"}]}
        return {"snippet": "Your Scoot flight TR123 departs 14 Aug 09:15",
                "payload": {"headers": [{"name": "Subject", "value": "Scoot booking"},
                                        {"name": "From", "value": "Scoot"},
                                        {"name": "Date", "value": "1 Jul 2026"}]}}


def test_gmail_search_parses(client):
    hits = google_client.gmail_search("flight august", service=_GmailStub())
    assert hits and hits[0]["subject"] == "Scoot booking"
    assert "14 Aug" in hits[0]["snippet"]


def test_docs_downranks_vault_md(client):
    d = vault_store.VAULT_DIR
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "passport_notes.md"), "w").write("x")
    open(os.path.join(d, "passport_scan.txt"), "w").write("x")
    conn = _db()
    names = [h["name"] for h in docs.search_documents(conn, "passport")]
    conn.close()
    # both match 'passport'; the .md note is down-ranked below the real .txt document
    assert names.index("passport_scan.txt") < names.index("passport_notes.md")


def test_retrieve_instant_from_facts(client, monkeypatch):
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: "Passport K1234567 (exp 2030)")
    reply = retrieve.answer(conn, "passport", "my passport number",
                            claude_fn=lambda p: (_ for _ in ()).throw(AssertionError("deep path should be skipped")))
    conn.close()
    assert reply == "Passport K1234567 (exp 2030)"


def _planner(*replies):
    """A scripted plan_fn for the agent loop: returns each JSON reply in turn (repeating the
    last), and records every prompt it was shown so a test can assert what reached the model."""
    state = {"i": 0, "prompts": []}
    def fn(prompt):
        state["prompts"].append(prompt)
        r = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return r
    fn.prompts = state["prompts"]
    return fn


def test_retrieve_answers_directly_from_gmail(client, monkeypatch):
    """Hop 1: the planner sees gmail evidence in the pre-seeded state and answers directly."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)   # force the loop
    monkeypatch.setattr(google_client, "is_configured", lambda: True)
    monkeypatch.setattr(google_client, "gmail_search",
                        lambda q, n=5, body=False: [{"from": "Scoot", "subject": "Scoot booking",
                                         "date": "1 Jul", "snippet": "flight TR123 on 14 Aug 09:15",
                                         "body": "Scoot booking TR123 departs 14 Aug 09:15"}])
    plan = _planner('{"tool":"answer","text":"TR123 — 14 Aug 09:15, from Gmail"}')
    res = retrieve.run(conn, "flight august", "what's my flight date in august", claude_fn=plan)
    conn.close()
    assert "14 Aug" in res["reply"] and res["documents"] == []
    assert "Scoot booking" in plan.prompts[0]       # gmail evidence reached the prompt
    assert "WHO SAM IS" in plan.prompts[0]        # profile injected for disambiguation


def test_retrieve_reads_one_document(client, monkeypatch):
    """read → observe → answer: the loop opens a validated candidate and answers from it."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    monkeypatch.setattr(docs, "search_documents",
                        lambda c, q, limit=10: [{"name": "lee jun kai passport.pdf",
                                                 "root_idx": 1, "rel": "p.pdf", "path": "/x/p.pdf", "score": 1.0}])
    monkeypatch.setattr(docs, "local_path_for_hit", lambda c, h: "/x/p.pdf")
    read = {}
    monkeypatch.setattr(docs, "extract_info",
                        lambda path, q, claude_fn=None: read.update(path=path) or "Passport K1234567")
    plan = _planner('{"tool":"read","ids":[1],"for":"passport number"}',
                    '{"tool":"answer","text":"Passport K1234567"}')
    res = retrieve.run(conn, "passport", "my passport number", claude_fn=plan)
    conn.close()
    assert res["reply"] == "Passport K1234567" and read["path"] == "/x/p.pdf"
    assert "Passport K1234567" in plan.prompts[1]   # the reading was fed back to the model


def test_retrieve_reads_MULTIPLE_documents_for_a_family_question(client, monkeypatch):
    """A question spanning several people opens EVERY relevant candidate in one read call and
    the aggregate is fed back — never 'only one passport'."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    fam = [{"name": "lee jun kai passport.pdf", "root_idx": 1, "rel": "a.pdf", "path": "/x/a.pdf", "score": 1.0},
           {"name": "lee xin yi passport.pdf", "root_idx": 1, "rel": "b.pdf", "path": "/x/b.pdf", "score": 1.0},
           {"name": "tan zhi hao passport.pdf", "root_idx": 1, "rel": "c.pdf", "path": "/x/c.pdf", "score": 1.0}]
    monkeypatch.setattr(docs, "search_documents", lambda c, q, limit=10: fam)
    monkeypatch.setattr(docs, "local_path_for_hit", lambda c, h: "/x/" + h["rel"])
    seen = {}
    monkeypatch.setattr(docs, "extract_info_multi",
                        lambda paths, q, claude_fn=None: seen.update(paths=list(paths))
                        or "Jun Kai: K123 (2032)\nXin Yi: K456 (2030)\nZhi Hao: K789 (2029)")
    monkeypatch.setattr(docs, "extract_info",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("multi-read expected")))
    plan = _planner('{"tool":"read","ids":[1,2,3],"for":"passport number and expiry"}',
                    '{"tool":"answer","text":"Jun Kai: K123; Xin Yi: K456; Zhi Hao: K789"}')
    res = retrieve.run(conn, "family passport", "my family's passports all together", claude_fn=plan)
    conn.close()
    assert seen["paths"] == ["/x/a.pdf", "/x/b.pdf", "/x/c.pdf"]     # all three opened at once
    assert "Xin Yi" in plan.prompts[1] and "Zhi Hao" in plan.prompts[1]   # aggregate fed back


def test_lookup_multihop_fetch_delivers_multiple_files(client, monkeypatch):
    """THE general win: 'fetch everyone on the Scoot booking's passport' — read the booking to
    get the names, search each passport, deliver them ALL. One loop, no special case, multi-file
    delivery. This is the shape the old single-read pipeline could never do."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    booking = [{"name": "scoot-booking.pdf", "root_idx": 1, "rel": "bk.pdf", "path": "/x/bk.pdf", "score": 1.0}]
    passports = [{"name": "lee jun kai passport.pdf", "root_idx": 1, "rel": "kv.pdf", "path": "/x/kv.pdf", "score": 1.0},
                 {"name": "ong mei fang passport.pdf", "root_idx": 1, "rel": "cl.pdf", "path": "/x/cl.pdf", "score": 1.0}]
    monkeypatch.setattr(docs, "search_documents",
                        lambda c, q, limit=10: passports if "passport" in q.lower() else booking)
    monkeypatch.setattr(docs, "local_path_for_hit", lambda c, h: h["path"])
    monkeypatch.setattr(docs, "extract_info",
                        lambda path, q, claude_fn=None: "Passengers: Lee Jun Kai, Ong Mei Fang")
    plan = _planner('{"tool":"read","ids":[1],"for":"the passenger names on this booking"}',
                    '{"tool":"search","source":"docs","query":"passport"}',
                    '{"tool":"deliver","ids":[2,3]}')
    res = retrieve.run(conn, "scoot booking", "fetch everyone on the scoot flight's passport",
                       want="file", claude_fn=plan)
    conn.close()
    assert sorted(res["documents"]) == ["/x/cl.pdf", "/x/kv.pdf"]    # BOTH passports delivered
    assert "Sending" in res["reply"]


def test_progress_narrates_each_slow_step(client, monkeypatch):
    """The loop calls progress() before each search and each read so a multi-hop fetch shows
    activity instead of a hung 'typing…'. A quick single-call answer stays quiet."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    booking = [{"name": "scoot-booking.pdf", "root_idx": 1, "rel": "bk.pdf", "path": "/x/bk.pdf", "score": 1.0}]
    passports = [{"name": "jun kai passport.pdf", "root_idx": 1, "rel": "kv.pdf", "path": "/x/kv.pdf", "score": 1.0}]
    monkeypatch.setattr(docs, "search_documents",
                        lambda c, q, limit=10: passports if "passport" in q.lower() else booking)
    monkeypatch.setattr(docs, "local_path_for_hit", lambda c, h: h["path"])
    monkeypatch.setattr(docs, "extract_info", lambda path, q, claude_fn=None: "Passenger: Lee Jun Kai")
    updates = []
    plan = _planner('{"tool":"read","ids":[1],"for":"passenger names"}',
                    '{"tool":"search","source":"docs","query":"passport"}',
                    '{"tool":"deliver","ids":[2]}')
    retrieve.run(conn, "scoot booking", "fetch everyone on the scoot flight's passport",
                 want="file", claude_fn=plan, progress=updates.append)
    conn.close()
    assert any("Reading" in u and "scoot-booking.pdf" in u for u in updates)   # narrated the read
    assert any("Searching" in u and "passport" in u for u in updates)          # narrated the search
    assert len(updates) == 2                                                    # read + search, not the deliver


def test_search_source_tasks_feeds_tasks_into_state(client, monkeypatch):
    """The loop can search TASKS (a new source) — matching open tasks appear in the next
    prompt so the model can reason over them."""
    from domain.capture import create_task
    conn = _db()
    create_task(conn, "call the dentist about the crown")
    create_task(conn, "buy printer ink")
    conn.commit()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    plan = _planner('{"tool":"search","source":"tasks","query":"dentist"}',
                    '{"tool":"answer","text":"Yes — your dentist task is open."}')
    retrieve.run(conn, "dentist", "do I have a task about the dentist", claude_fn=plan)
    conn.close()
    assert "call the dentist about the crown" in plan.prompts[1]   # matched task surfaced
    assert "buy printer ink" not in plan.prompts[1]                # non-match excluded


def test_calendar_event_location_answered_from_calendar(client, monkeypatch):
    """'where is the event tomorrow' resolves from the CALENDAR source (pre-seeded), with the
    event's LOCATION — not a fruitless Gmail/tasks hunt."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: True)
    monkeypatch.setattr(google_client, "gmail_search", lambda *a, **k: [])
    monkeypatch.setattr(google_client, "calendar_range",
                        lambda lo, hi, service=None: [
                            {"summary": "Parent-teacher meeting", "start": "2026-07-15T14:00:00",
                             "location": "Sunshine Preschool, Blk 123", "all_day": False}])
    plan = _planner('{"tool":"answer","text":"Parent-teacher meeting tomorrow 2pm '
                    '@ Sunshine Preschool, Blk 123"}')
    res = retrieve.run(conn, "event 2026-07-15 tomorrow", "where is the event tomorrow",
                       claude_fn=plan)
    conn.close()
    assert "Sunshine Preschool" in res["reply"]
    # the calendar event + its location reached the planner in hop 1 (no extra hops needed)
    assert "Sunshine Preschool" in plan.prompts[0] and "Parent-teacher" in plan.prompts[0]


def test_chained_search_then_create_task(client, monkeypatch):
    """THE read+write win: 'find the hotel in my June journal and add a task to rebook it' —
    search vault → create_task → confirm. The write actually lands in the DB."""
    from domain import vault_store
    from core.db import today_iso
    conn = _db()
    vault_store.append_journal_entry(today_iso(), "Loved Hotel Amara in June — must rebook", source="")
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    updates = []
    plan = _planner('{"tool":"search","source":"vault","query":"hotel june"}',
                    '{"tool":"create_task","title":"Rebook Hotel Amara","category":"personal"}',
                    '{"tool":"answer","text":"Found Hotel Amara in your June journal — added a task to rebook it."}')
    res = retrieve.run(conn, "hotel june", "find the hotel I liked in june and add a task to rebook it",
                       claude_fn=plan, progress=updates.append)
    row = conn.execute("SELECT title, category FROM tasks "
                       "WHERE title LIKE 'Rebook%' AND deleted_at IS NULL").fetchone()
    conn.close()
    assert row and row["title"] == "Rebook Hotel Amara" and row["category"] == "personal"
    assert "rebook" in res["reply"].lower()
    assert any("Added task" in u for u in updates)                 # the write was narrated


def test_chained_append_journal_from_a_lookup(client, monkeypatch):
    """A lookup can also append to the journal (write tool), and it really lands."""
    from domain import vault_store
    from core.db import today_iso
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    plan = _planner('{"tool":"append_journal","text":"Shipped the retrieval agent today."}',
                    '{"tool":"answer","text":"Noted in today\'s journal."}')
    retrieve.run(conn, "", "journal that I shipped the retrieval agent today", claude_fn=plan)
    page = vault_store.read_journal(today_iso())
    conn.close()
    assert page and any("retrieval agent" in e["text"] for e in page["entries"])


def test_write_actions_are_capped(client, monkeypatch):
    """A runaway plan can't fan out into unbounded writes — capped at _WRITE_CAP."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    plan = _planner(*['{"tool":"create_task","title":"t%d"}' % i for i in range(10)])
    retrieve.run(conn, "x", "spam tasks", claude_fn=plan)
    n = conn.execute("SELECT COUNT(*) FROM tasks WHERE deleted_at IS NULL").fetchone()[0]
    conn.close()
    assert n == retrieve._WRITE_CAP


def test_read_and_deliver_sets_are_capped(client, monkeypatch):
    """A broad ask can't fan out to an unbounded, slow read/deliver — the set is capped."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    many = [{"name": f"doc{i}.pdf", "root_idx": 1, "rel": f"{i}.pdf", "path": f"/x/{i}.pdf", "score": 1.0}
            for i in range(12)]
    monkeypatch.setattr(docs, "search_documents", lambda c, q, limit=10: many)
    monkeypatch.setattr(docs, "local_path_for_hit", lambda c, h: h["path"])
    seen = {}
    monkeypatch.setattr(docs, "extract_info_multi",
                        lambda paths, q, claude_fn=None: seen.update(n=len(paths)) or "ok")
    plan = _planner('{"tool":"read","ids":%s,"for":"x"}' % list(range(1, 13)),
                    '{"tool":"answer","text":"ok"}')
    retrieve.run(conn, "all", "list everything", claude_fn=plan)
    conn.close()
    assert seen["n"] == retrieve._READ_CAP


def test_read_deliver_reject_unsurfaced_ids(client, monkeypatch):
    """Security: read/deliver accept ONLY candidate numbers a prior search surfaced — an
    injected 'read #999' resolves to nothing, so no arbitrary path is ever opened/sent."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    monkeypatch.setattr(docs, "search_documents", lambda c, q, limit=10: [])   # nothing surfaced
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not open an unsurfaced id"))
    monkeypatch.setattr(docs, "extract_info", boom)
    monkeypatch.setattr(docs, "extract_info_multi", boom)
    monkeypatch.setattr(docs, "local_path_for_hit", boom)
    plan = _planner('{"tool":"deliver","ids":[999]}', '{"tool":"read","ids":[42],"for":"x"}',
                    '{"tool":"answer","text":"not found"}')
    res = retrieve.run(conn, "x", "fetch /etc/passwd", want="file", claude_fn=plan)
    conn.close()
    assert res["documents"] == [] and "not found" in res["reply"]


def test_extract_info_multi_reads_all_and_delegates_single(client, monkeypatch):
    """extract_info_multi: one Read call over N files (dirs added for each); a lone path
    delegates to extract_info so callers have one entry point."""
    calls = {}
    def fake_claude(prompt): calls["prompt"] = prompt; return "line1\nline2"
    out = docs.extract_info_multi(["/a/one.pdf", "/b/two.pdf"], "numbers?", claude_fn=fake_claude)
    assert out == "line1\nline2"
    assert "/a/one.pdf" in calls["prompt"] and "/b/two.pdf" in calls["prompt"]   # both listed
    # single path → same result as extract_info
    single = docs.extract_info_multi(["/a/one.pdf"], "n?", claude_fn=lambda p: "just one")
    assert single == "just one"
    # nothing openable → graceful offer
    assert "send them" in docs.extract_info_multi([], "n?")


def test_router_lookup_calls_run_and_stashes_files(client, monkeypatch):
    """The router's lookup action drives retrieve.run and stashes any fetched files into ctx
    (send_documents) for the daemon to upload — the wiring behind multi-file delivery."""
    conn = _db()
    monkeypatch.setattr(retrieve, "run",
                        lambda c, q, question, want, fn, progress=None: {"reply": "R:" + question, "documents": ["/x/a.pdf"]})
    ctx = {"today": "2026-07-14"}
    reply, _ = router.apply_action(
        conn, {"action": "lookup", "query": "f", "question": "passports?", "want": "file"}, ctx)
    conn.close()
    assert reply == "R:passports?" and ctx["send_documents"] == ["/x/a.pdf"]


def test_router_lookup_multi_file_sends_all_no_confirm(client, monkeypatch):
    """He asked for the files and they only go to his own chat → just send them (undo-not-
    confirm). The reply LISTS the clean doc_names (not temp paths) so he sees what came."""
    conn = _db()
    monkeypatch.setattr(retrieve, "run", lambda c, q, question, want, fn, progress=None: {
        "reply": "sending", "documents": ["/tmp/x1-a.pdf", "/tmp/x2-b.png"],
        "doc_names": ["jun kai passport.pdf", "zhi hao passport.png"]})
    ctx = {"today": "2026-07-14"}
    reply, _ = router.apply_action(
        conn, {"action": "lookup", "query": "f", "question": "everyone's passport", "want": "file"}, ctx)
    conn.close()
    assert ctx["send_documents"] == ["/tmp/x1-a.pdf", "/tmp/x2-b.png"]   # sent, no gate
    assert "zhi hao passport.png" in reply and "yes" not in reply.lower()


def test_merge_attachments_makes_email_files_candidates(client):
    """Email attachments become numbered candidates the loop can read/deliver (deduped by
    message+attachment) — the fix that let it see the passenger manifest in the itinerary."""
    candidates = []
    hits = [{"id": "m1", "subject": "Scoot booking",
             "attachments": [{"filename": "Itinerary.pdf", "attachment_id": "a1"},
                             {"filename": "Receipt.pdf", "attachment_id": "a2"}]}]
    retrieve._merge_attachments(candidates, hits)
    retrieve._merge_attachments(candidates, hits)            # idempotent — no dupes
    assert [c["name"] for c in candidates] == ["Itinerary.pdf", "Receipt.pdf"]
    assert all(c["source"] == "gmail_attachment" for c in candidates)


def test_gmail_widens_when_the_and_search_misses(monkeypatch):
    """Gmail ANDs every term, so a question-shaped query ('...booking number in august')
    zeroes out. _gmail_hits must drop the question words + the month and still find the
    booking email — the exact bug Sam hit."""
    calls = []

    def fake_search(q, n=5, body=False):
        calls.append(q)
        return [{"subject": "Scoot booking XY34ZW", "body": "ref XY34ZW"}] \
            if q == "scoot booking" else []      # only the trimmed query matches

    monkeypatch.setattr(google_client, "gmail_search", fake_search)
    hits = retrieve._gmail_hits("what is my scoot booking number in august")
    assert hits and "XY34ZW" in hits[0]["subject"]
    assert "scoot booking" in calls              # it widened down to the terms that hit


def test_prefer_owner_sends_owners_document(client, monkeypatch):
    """'fetch my passport' must rank SAM'S file above a family member's when they tie on
    filename score (the real bug: the daughter's newer file won)."""
    monkeypatch.setattr(vault_store, "identity_names",
                        lambda: ({"lee", "jun", "kai"}, {"lee", "xin", "yi", "ong", "mei", "fang"}))
    hits = [{"name": "lee xin yi passport.pdf", "score": 1.0},
            {"name": "lee jun kai passport.pdf", "score": 1.0}]
    ranked = docs.prefer_owner(None, list(hits), "fetch my passport")
    assert ranked[0]["name"] == "lee jun kai passport.pdf"
    # naming the relative explicitly is respected (no override)
    ranked2 = docs.prefer_owner(None, list(hits), "xin yi passport")
    assert ranked2[0]["name"] == "lee xin yi passport.pdf"


def test_search_goals_excludes_archived_and_deleted(client):
    """Goals auto-archive the moment their period ends (goals_core.archive_expired_goals),
    so an unfiltered read hands the model LAST quarter's goal as live evidence — with a
    valid id it may then act on. `_search_tasks` filters archived_at; this must too."""
    conn = _db()
    # period/kind are the deprecated legacy columns — still NOT NULL, and `period` still
    # CHECKs IN ('week','month'), which is exactly why `timeframe` superseded it (v3).
    ins = ("INSERT INTO goals(title, period, period_start, kind, timeframe, created, "
           "archived_at, deleted_at) "
           "VALUES(?, 'month', '2026-07-01', 'number', 'quarter', '2026-07-01T00:00:00Z', ?, ?)")
    with conn:
        live = conn.execute(ins, ("ship the kayak video", None, None)).lastrowid
        conn.execute(ins, ("ship the kayak reel", "2026-01-01T00:00:00Z", None))    # archived
        conn.execute(ins, ("ship the kayak short", None, "2026-01-01T00:00:00Z"))   # soft-deleted
    found = retrieve._search_goals(conn, "ship the kayak")
    conn.close()
    assert [g["id"] for g in found] == [live], "an archived or deleted goal surfaced as live evidence"
