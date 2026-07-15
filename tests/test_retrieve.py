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


class _MailStub:
    """A stub mailbox over [{id, threadId, subject, body}]. No new_batch_http_request, so
    google_client takes its serial fallback — the same path a real batch failure takes."""

    def __init__(self, msgs):
        self.msgs = msgs

    def users(self): return self
    def messages(self): return self

    def list(self, userId=None, q=None, maxResults=None):
        self._r = {"messages": [{"id": m["id"], "threadId": m["threadId"]}
                                for m in self.msgs[:maxResults]]}
        return self

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        import base64
        m = next(x for x in self.msgs if x["id"] == id)
        payload = {"headers": [{"name": "Subject", "value": m["subject"]},
                               {"name": "From", "value": "sender@example.com"},
                               {"name": "Date", "value": "1 Jul 2026"}]}
        if format == "full":
            payload["mimeType"] = "text/plain"
            payload["body"] = {"data": base64.urlsafe_b64encode(m["body"].encode()).decode()}
        self._r = {"id": m["id"], "snippet": m["subject"][:40], "payload": payload}
        return self

    def execute(self): return self._r


def test_gmail_bodies_spread_across_conversations(client):
    """THE bug: Gmail ranks MESSAGES, so one chatty conversation ate every slot. Six replies
    under one subject buried the cruise confirmation past n=5 — the model was handed five
    emails about bedding and truthfully said it found no cruise. Bodies are now dealt
    round-robin across conversations, so a lone email about a different thing is never
    starved by a long thread."""
    msgs = [{"id": str(i), "threadId": f"t{i}",
             "subject": "Re: Room allocation and bedding requests - Ref: 11112222",
             "body": "bedding chatter"} for i in range(1, 7)]
    msgs.append({"id": "9", "threadId": "t9", "body": "sailing 12 Nov",
                 "subject": "Ocean Star Cruise 12 Nov 2027 Booking No: 99998888"})
    hits = google_client.gmail_search("cruise", n=3, body=True, service=_MailStub(msgs))
    withbody = [h["subject"] for h in hits if h.get("body")]
    assert len(withbody) == 3
    assert any("Ocean Star Cruise" in s for s in withbody)  # the minority got a body slot


def test_gmail_returns_every_candidate_even_past_n(client):
    """`n` is a BODY budget, never a visibility limit: everything Gmail matched comes back, the
    overflow as headlines. A booking's date/ref usually sits in the subject, so no ranking
    accident can hide the answer from the model — which is what made a bigger `n` unnecessary."""
    msgs = [{"id": str(i), "threadId": f"t{i}", "subject": f"Subject number {i}",
             "body": f"body {i}"} for i in range(1, 6)]
    hits = google_client.gmail_search("x", n=2, body=True, service=_MailStub(msgs))
    assert len(hits) == 5                                   # every candidate visible
    assert sum(1 for h in hits if h.get("body")) == 2       # only n carry the costly body
    assert {h["subject"] for h in hits} == {f"Subject number {i}" for i in range(1, 6)}
    assert all(h["snippet"] for h in hits)                  # headlines still carry a snippet


def test_one_hop_runs_several_searches(client, monkeypatch):
    """A hop can carry several searches and they run concurrently — the old loop spent one hop
    AND one 3-10s planning call per search just to ask the next question."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    seen = []
    monkeypatch.setattr(docs, "search_documents",
                        lambda c, q, limit=10: seen.append(q) or [])
    plan = _planner('{"tool":"search","searches":[{"source":"docs","query":"genting dream"},'
                    '{"source":"docs","query":"star cruises"}]}',
                    '{"tool":"answer","text":"done"}')
    retrieve.run(conn, "cruise", "when is my cruise", claude_fn=plan)
    conn.close()
    assert "genting dream" in seen and "star cruises" in seen     # both ran, in ONE hop
    assert len(plan.prompts) == 2                                 # one planning call, not two


def test_a_reworded_search_does_not_burn_the_hops(client, monkeypatch):
    """'cruise booking' -> 'cruise itinerary' -> 'cruise confirmation' are the SAME search.
    Re-running rewordings of a search that already came back empty burned all five hops and
    then answered "I couldn't find it" without ever reading anything."""
    conn = _db()
    monkeypatch.setattr(docs, "answer_from_facts", lambda c, q: None)
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    monkeypatch.setattr(docs, "search_documents", lambda c, q, limit=10: [])
    plan = _planner('{"tool":"search","source":"docs","query":"cruise booking"}',
                    '{"tool":"search","source":"docs","query":"cruise itinerary"}',
                    '{"tool":"search","source":"docs","query":"cruise confirmation"}',
                    '{"tool":"search","source":"docs","query":"cruise reservation details"}')
    retrieve.run(conn, "cruise", "when is my cruise", claude_fn=plan)
    conn.close()
    # hop1, hop2 (recognised as a re-tread -> break), then the one forced final answer.
    assert len(plan.prompts) == 3, "reworded searches should stop the loop, not spin it"


def test_first_non_empty_prefers_list_order_not_completion_order(client):
    """The ladder races its rungs but must still return the MOST SPECIFIC hit — a slow
    specific rung beats a fast broad one. A rung that raises counts as empty."""
    import time
    from core.text import first_non_empty

    def slow_specific():
        time.sleep(0.05)
        return ["specific"]
    got = first_non_empty([lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                           slow_specific, lambda: ["broad"]])
    assert got == ["specific"]                     # not "broad", despite finishing first
    assert first_non_empty([lambda: [], lambda: None]) is None


def test_dropbox_ladder_never_searches_a_stopword(client):
    """The old ladder peeled EVERY term and ended up searching 'up' on a real question, then
    handed back whichever file contained it as genuine evidence — 8 sequential calls to
    manufacture noise. A rung that can't identify anything is not a search."""
    from core.text import tokenize
    q = "I have a cruise coming up later this year, what date"
    variants = docs._dropbox_variants(q, tokenize(q))
    assert variants[0] == q.strip()                # most specific first: the whole question
    for junk in ("up", "have", "this", "year", "coming", "later", "what", "date"):
        assert junk not in variants, f"ladder would search the stopword {junk!r}"
    assert "cruise" in " ".join(variants)          # the one word that identifies anything


def test_widened_gmail_search_warns_the_model(client, monkeypatch):
    """Widening is LOSSY and can only know emptiness, never relevance: Sam's typo ("fight")
    made the ladder drop "august" and widen to bare "booking", which matched 201 unrelated
    emails — and the model named a September trip as the August one, fast and sourced. The
    evidence must admit the question's words were dropped."""
    monkeypatch.setattr(google_client, "gmail_probe",
                        lambda v, service=None: 0 if "fight" in v else 3)
    monkeypatch.setattr(google_client, "gmail_search",
                        lambda v, n=5, body=False: [{"subject": "some booking", "from": "x",
                                                     "date": "1 Jul", "snippet": "s"}])
    hits = retrieve._gmail_hits("booking number for my fight in august")
    assert hits[0]["asked"] != hits[0]["query"]      # recorded that it widened
    text = retrieve._evidence_text({"gmail": hits})
    assert "NOTHING matched" in text and hits[0]["query"] in text
    # and a clean match must NOT cry wolf
    monkeypatch.setattr(google_client, "gmail_probe", lambda v, service=None: 3)
    clean = retrieve._gmail_hits("scoot booking")
    assert clean[0]["asked"] == clean[0]["query"]
    assert "NOTHING matched" not in retrieve._evidence_text({"gmail": clean})


def test_a_dead_source_reports_instead_of_looking_empty(client, monkeypatch):
    """THE production bug, and the costliest one: the bot ran in a container with no OAuth
    token, so is_configured() was False and _fetch quietly returned [] — for MONTHS the bot
    answered "nothing here mentions a cruise" about mail sitting in the inbox, while Settings
    (a DIFFERENT container, which had the token) showed "Google: Connected ✓". A source that
    did not run must never look like a source that found nothing."""
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    items, err = retrieve._fetch("gmail", "cruise")
    assert items == [] and err and "NOT connected" in err
    text = retrieve._evidence_text({"gmail": [], "errors": {"gmail": err}})
    assert "NOT evidence of absence" in text and "gmail" in text

    # a source that THROWS is a failure too, not an absence — and it must not sink the answer
    def boom(*a, **k):
        raise RuntimeError("HttpError 403 insufficient scope")
    monkeypatch.setattr(google_client, "is_configured", lambda: True)
    monkeypatch.setattr(retrieve, "_gmail_hits", boom)
    items, err = retrieve._fetch("gmail", "cruise")
    assert items == [] and "403" in err


def test_data_dir_follows_the_mount_not_the_image(client, monkeypatch):
    """Prod runs the web app and the bot as two containers from ONE image, and .dockerignore
    excludes data/ — so <repo>/data is /app/data: absent from the image, unshared, and wiped by
    every redeploy. Anything that must outlive a deploy (token, secret key, raw-capture log)
    belongs beside the DB on the mounted volume."""
    from core.db import data_dir
    monkeypatch.setenv("LIFEOS_DB_PATH", "/data/app.db")
    assert data_dir() == "/data"
    monkeypatch.delenv("LIFEOS_DB_PATH")
    assert data_dir().endswith("/data")          # dev falls back to <repo>/data, unchanged


def test_profile_lives_in_the_vault_wherever_the_vault_is(tmp_path, monkeypatch):
    """profile.md is injected into EVERY claude -p surface, and read_profile() returns "" on a
    miss — so a profile pointing at the wrong place doesn't raise, it just makes the bot
    quietly dumber. PROFILE_PATH used to be pinned to <repo>/vault while LIFEOS_VAULT_DIR moved
    everything else, which in prod (vault on the /data volume) would have resolved it to
    /app/vault inside the image: absent, and wiped by every Watchtower pull. Same shape as
    test_data_dir_follows_the_mount_not_the_image — durable things follow the mount."""
    import importlib
    monkeypatch.setenv("LIFEOS_VAULT_DIR", str(tmp_path / "vault"))
    import domain.vault_store as vs
    vs = importlib.reload(vs)
    try:
        assert vs.VAULT_DIR == str(tmp_path / "vault")
        assert vs.PROFILE_PATH == str(tmp_path / "vault" / "profile.md")   # IN the vault
    finally:
        monkeypatch.undo()
        importlib.reload(vs)


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


def test_every_connector_backed_block_states_whether_it_ran(client):
    """THE tripwire for a bug class that has now cost four production failures in four files:
    truncated≡absent, widened≡relevant, connector-dead≡nothing-found, free-day≡Google-broken.
    Each was fixed at its own site, which is why a fifth appeared (the router's calendar block
    was simply OMITTED when Google was down — the bot went silently calendar-blind).

    Local SQLite reads are exempt on purpose: with no connector to fail, "(none)" cannot lie.
    Only sources that might not have RUN must declare it — and core.evidence.source_block makes
    `ran` a required argument so a call site cannot forget."""
    import inspect
    from core import evidence
    from ai import router, proactive

    # `ran` is REQUIRED — the enforcement, not the formatting
    sig = inspect.signature(evidence.source_block)
    assert sig.parameters["ran"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["ran"].default is inspect.Parameter.empty

    # a source that didn't run never renders as emptiness
    dead = evidence.source_block("CAL:", [], ran=False, unavailable="Google is not connected",
                                 empty="(nothing today)")
    assert "NOT CHECKED" in dead and "NOT evidence of absence" in dead
    assert "(nothing today)" not in dead
    # ...and it is never a blank block: an absent block is how the router went blind
    assert dead.strip()

    # a source that ran and is empty states that as a usable fact
    live = evidence.source_block("CAL:", [], ran=True, empty="(nothing today)")
    assert "(nothing today)" in live and "NOT CHECKED" not in live

    # a degraded hit still carries its caveat
    noted = evidence.source_block("CAL:", ["x"], ran=True, note="showing 5 of ~201")
    assert "showing 5 of ~201" in noted and "x" in noted

    # every Google-backed block in the prompt builders goes through the primitive
    for mod in (router, proactive):
        src = inspect.getsource(mod)
        assert "source_block(" in src, f"{mod.__name__} renders a connector source by hand"
