"""Life OS test suite — exercises the product logic through the real routes and the
real markdown vault (verification checklist item 1)."""

import json
import os

from core import db_init  # noqa: F401  (ensures path set up by conftest import order)
from core import web_core
from domain import vault_store
from domain.capture import create_task, route_capture
from domain.tasks_core import (next_due_date, complete_task, today_tasks, week_tasks,
                          archive_old_done)
from core.db import connect, today_iso, now_iso


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


# ── pages render ──────────────────────────────────────────────────────────────
def test_pages_200(client):
    for path in ("/", "/tasks", "/notes", "/journal", "/goals"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_mockup_structure_present(client):
    """Spot-check the approved mockup's classes/structure made it into the render."""
    home = client.get("/").data.decode()
    assert 'class="qcap"' in home and 'id="qin"' in home        # composer
    # the composer is a clean +｜input｜📎｜Add bar — no type chips; the AI classifies
    assert 'id="qtypes"' not in home
    tasks = client.get("/tasks").data.decode()
    assert 'class="board"' in tasks and 'data-cat="content"' in tasks  # kanban + chips


# ── tasks ─────────────────────────────────────────────────────────────────────
def test_create_and_complete_sets_completed_at(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Pay invoice", col="week")
    conn.close()
    r = client.post(f"/tasks/{tid}/complete", data={"done": "1"})
    assert r.status_code == 200
    conn = _db()
    row = conn.execute("SELECT done, completed_at, col FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["done"] == 1 and row["completed_at"] == today_iso() and row["col"] == "done"


def test_subtask_auto_completes_parent(client):
    conn = _db()
    with conn:
        pid = create_task(conn, "Publish video", col="week")
        s1 = create_task(conn, "Record", parent_id=pid)
        s2 = create_task(conn, "Edit", parent_id=pid)
    conn.close()
    client.post(f"/tasks/{s1}/complete", data={"done": "1"})
    r = client.post(f"/tasks/{s2}/complete", data={"done": "1"})
    assert r.get_json().get("parent_completed") is True
    conn = _db()
    p = conn.execute("SELECT done FROM tasks WHERE id=?", (pid,)).fetchone()
    conn.close()
    assert p["done"] == 1
    # unchecking a subtask un-completes the parent
    client.post(f"/tasks/{s2}/complete", data={"done": "0"})
    conn = _db()
    p = conn.execute("SELECT done FROM tasks WHERE id=?", (pid,)).fetchone()
    conn.close()
    assert p["done"] == 0


def test_reorder_persists(client):
    conn = _db()
    with conn:
        a = create_task(conn, "A", col="week")
        b = create_task(conn, "B", col="week")
        c = create_task(conn, "C", col="week")
    conn.close()
    r = client.post("/tasks/reorder", json={"col": "week", "ids": [c, a, b]})
    assert r.status_code == 200
    conn = _db()
    rows = conn.execute(
        "SELECT id FROM tasks WHERE col='week' AND parent_id IS NULL ORDER BY sort_order").fetchall()
    conn.close()
    assert [x["id"] for x in rows] == [c, a, b]


def test_recurring_respawns_with_next_due(client):
    conn = _db()
    with conn:
        tid = create_task(conn, "Weekly review", col="week",
                          due_date=today_iso(), recur_rule="daily")
    conn.close()
    client.post(f"/tasks/{tid}/complete", data={"done": "1"})
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE title='Weekly review' AND parent_id IS NULL").fetchall()
    conn.close()
    assert len(rows) == 2                       # original + respawn
    dones = [r for r in rows if r["done"]]
    opens = [r for r in rows if not r["done"]]
    assert len(dones) == 1 and len(opens) == 1
    assert opens[0]["due_date"] == next_due_date("daily", today_iso())


def test_next_due_date_forms():
    assert next_due_date("daily", "2026-07-09") == "2026-07-10"
    # 2026-07-09 is a Thursday; next Sunday is the 12th
    assert next_due_date("weekly:sun", "2026-07-09") == "2026-07-12"
    assert next_due_date("monthly:1", "2026-07-09") == "2026-08-01"


def test_archive_after_7_days(client):
    conn = _db()
    old = "2026-06-01"
    with conn:
        tid = create_task(conn, "Ancient", col="done")
        conn.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?", (old, tid))
    archive_old_done(conn)
    row = conn.execute("SELECT archived_at FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["archived_at"] is not None


def test_today_membership(client):
    conn = _db()
    today = today_iso()
    with conn:
        due = create_task(conn, "Due today", col="week", due_date=today)
        over = create_task(conn, "Overdue", col="week", due_date="2020-01-01")
        planned = create_task(conn, "Planned", col="backlog", planned_on=today)
        create_task(conn, "Someday", col="backlog")            # must NOT appear
    ids = {t["id"] for t in today_tasks(conn)}
    conn.close()
    assert due in ids and over in ids and planned in ids
    assert len(ids) == 3


def test_week_pool_excludes_today_overlap(client):
    """The Today 'This week' pool = open col='week' tasks NOT already on Today:
    future-due or undated week tasks appear; anything due today / overdue / planned
    today / done, plus backlog-column tasks, are excluded (no duplicate sections)."""
    conn = _db()
    today = today_iso()
    future = "2099-01-01"
    with conn:
        undated = create_task(conn, "Undated week", col="week")
        futured = create_task(conn, "Future week", col="week", due_date=future)
        parent = create_task(conn, "Parent week", col="week")
        create_task(conn, "sub", parent_id=parent)
        # overlaps with Today — must be excluded from the pool
        due = create_task(conn, "Due today", col="week", due_date=today)
        over = create_task(conn, "Overdue", col="week", due_date="2020-01-01")
        planned = create_task(conn, "Planned", col="week", planned_on=today)
        done = create_task(conn, "Done", col="week")
        conn.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?", (today, done))
        backlog = create_task(conn, "Backlog", col="backlog")  # wrong column
    ids = {t["id"] for t in week_tasks(conn)}
    conn.close()
    assert {undated, futured, parent} <= ids            # the week pool
    assert not ({due, over, planned, done, backlog} & ids)  # no Today/backlog overlap


def test_week_pool_hidden_when_empty(client):
    """Section is view-only and hidden when there's nothing to show: no `week` rows
    render, and today.html omits the 'This week' card entirely."""
    conn = _db()
    today = today_iso()
    with conn:
        # a single week task that IS on Today (planned) — pool should be empty
        create_task(conn, "On today", col="week", planned_on=today)
    assert week_tasks(conn) == []
    conn.close()
    assert 'class="card weekpool"' not in client.get("/").data.decode()


# ── capture routing ───────────────────────────────────────────────────────────
def test_capture_routing(client):
    conn = _db()
    # No more t:/n:/j: prefixes — plain text (even a leftover prefix) files as an unsorted
    # note; the AI router classifies natural language now. route_capture stays deterministic
    # only for a task-verb opener and URLs.
    note = route_capture(conn, "t: buy milk")
    assert note["kind"] == "note" and "unsorted" in note["tags"]
    # task-verb opener → task; trailing ! → high priority
    hi = route_capture(conn, "add a task urgent thing!")
    assert hi["kind"] == "task"
    url = route_capture(conn, "https://instagram.com/reel/abc")
    assert url["kind"] == "note" and "link" in url["tags"] and "idea" in url["tags"]
    conn.close()
    row = connect(os.environ["LIFEOS_DB_PATH"]).execute(
        "SELECT priority FROM tasks WHERE id=?", (hi["id"],)).fetchone()
    assert row["priority"] == "high"


def test_task_verb_layer_instant_vs_router():
    """The deterministic verb layer decides what skips the AI router. The router is MEASURED
    at 3.4-10.2s (median 5.4s), so a miss is expensive — but it's still only slow, whereas a
    FALSE POSITIVE mis-files Sam's data. The None cases below therefore matter more than the
    hits, and every one of them is a real phrasing this layer once swallowed."""
    from domain.capture import task_imperative as ti

    # instant: an explicit wrapper, or a bare action verb with nothing to parse
    for t in ["add a task test 1", "todo water plants", "buy milk", "call the dentist",
              "pay the rent", "fix bicycle", "renew passport", "follow up with the sponsor"]:
        assert ti(t) is not None, t
    assert ti("add a task test 1") == "test 1"      # wrapper stripped
    assert ti("buy milk") == "buy milk"             # bare verb IS the task

    # → router: any when-language, so it comes back with a real due date / as a reminder
    # rather than a task with the date buried in its title
    for t in ["call the dentist tomorrow", "buy milk at 3pm", "pay rent on the 15th",
              "call mum in 10 minutes", "finish the report next week",
              "renew passport before September", "email bob friday"]:
        assert ti(t) is None, t

    # → router: the "verb" is really a noun, a different intent, or past tense (journal)
    for t in ["email from mum", "order of service", "book club notes", "text from dad",
              "call me Sam", "update from the team", "water bill is due",
              "check out this link", "write about my day", "called the dentist",
              "finished the report", "the weather is nice", "todolist",
              "add a taskforce meeting"]:
        assert ti(t) is None, t

    # → router: prose ABOUT a thing reads as an instruction to a naive verb match. These are
    # real phrasings that silently became tasks before the bail-guards existed — note
    # "to-do list is long" was even MANGLED into a task titled "list is long".
    for t in ["call with Sam went well", "call was great, they want a follow up",
              "email is down again", "finish line was brutal", "cancel culture is out of hand",
              "buy low sell high is the whole game", "call for submissions is open",
              "install base is growing fast", "write up on the CPF video was great",
              "to-do list is long", "todo list is getting out of hand"]:
        assert ti(t) is None, t

    # → router: it owns priority, so a priority word must not get buried in the title
    for t in ["pay the invoice, urgent", "call the bank asap"]:
        assert ti(t) is None, t

    # → router: only it can SPLIT one line into several captures
    assert ti("buy milk and call the dentist") is None
    assert ti("buy milk & call the dentist") is None
    # ...but a plain conjoined object is still one task
    assert ti("buy milk and bread") == "buy milk and bread"

    # → router: a question is not a capture
    assert ti("call anyone about the invoice?") is None

    # ANY punctuation is just a separator after an explicit wrapper — the wrapper already
    # named the kind, so the rest is simply the title. This was an enumerated character list
    # (" :-–—") that happened to omit the comma, so "add task, connect to my invoicing <reel>"
    # missed tier 1 and fell to the URL branch as a #link note. Don't re-enumerate: the rule
    # is a word boundary, and these are only witnesses to it.
    for sep in (",", ":", " -", ";", "...", " —", ""):
        assert ti(f"add task{sep} water the plants") == "water the plants", repr(sep)
    assert ti("todo, water the plants") == "water the plants"
    # ...and the word boundary still holds, which is what the character list was really for
    assert ti("todolist") is None
    assert ti("add a taskforce meeting") is None
    # ...and every guard still outranks the wrapper, whatever the punctuation
    assert ti("add a task, buy milk and call the dentist") is None
    assert ti("add task: call the dentist tomorrow") is None

    # The guards judge the TITLE, not the wrapper's punctuation. "add task, pay the invoice"
    # is ONE plain task; judged against the whole body its comma tripped the multi-capture
    # guard (", pay" reads like the "and call" in "buy milk and call the dentist") and bailed
    # it to the router. The same guards must still bite on the title itself.
    assert ti("add task, pay the invoice") == "pay the invoice"
    assert ti("todo, call the dentist") == "call the dentist"
    assert ti("add a task, buy milk and call the dentist") is None   # multi, on the title
    assert ti("to-do list is long") is None                          # prose, on the title


def test_a_task_that_cites_a_url_keeps_a_clean_title():
    """The url is the task's REFERENCE, not the thing to do, so it lifts out of the title
    into tasks.link (schema v10) — a card reads "Connect to my invoicing", not 70 chars of
    tracking querystring. It must be LIFTED, never dropped: the reel is why the task exists."""
    from domain.capture import route_capture, split_off_link
    conn = _db()
    url = "https://www.instagram.com/reel/EXAMPLE12345/?igsh=EXAMPLETOKEN"

    res = route_capture(conn, f"Add task, connect to my invoicing {url}", enrich="off")
    assert res["kind"] == "task"
    assert res["title"] == "connect to my invoicing"     # clean
    assert res["link"] == url                            # and the reel survives
    row = conn.execute("SELECT title, link FROM tasks WHERE id=?", (res["id"],)).fetchone()
    assert (row["title"], row["link"]) == ("connect to my invoicing", url)

    # the punctuation a lifted url leaves behind goes with it — no title ending in ":" or "-"
    assert split_off_link(f"watch this: {url}") == ("watch this", url)
    assert split_off_link(f"read {url} later") == ("read later", url)
    assert split_off_link("no url here") == ("no url here", None)

    # a task that is ONLY a url has no words to be a title — keep the url visible rather than
    # filing an "Untitled task" whose one identifying detail is hidden in a chip
    r2 = route_capture(conn, f"add a task {url}", enrich="off")
    assert r2["kind"] == "task" and r2["title"] == url

    # priority still lifts, and the url doesn't block it
    r3 = route_capture(conn, f"add task, pay the invoice {url}!", enrich="off")
    assert r3["priority"] == "high" and r3["title"] == "pay the invoice" and r3["link"] == url


def test_a_message_may_cite_a_url_without_being_one():
    """The ladder ranks a timed reminder and a task verb ABOVE the url branch, and always did.
    But its two gates (is_explicit_capture, the daemon's instant-ack link path) each asked a
    LOCAL "contains a url?" question instead of asking the ladder — so a url won before the
    tiers above it were reached, and "add task, connect to my invoicing <reel>" was filed and
    acked as a saved #link #idea note. classify() is the single copy of the order; no gate may
    re-derive it."""
    from domain.capture import classify, is_bare_url, has_url, is_explicit_capture
    url = "https://www.instagram.com/reel/EXAMPLE12345/?igsh=EXAMPLETOKEN"

    # the reported bug, both phrasings — a tier above the url claims them
    assert classify(f"Add task, connect to my invoicing {url}") == "task"
    assert classify(f"add a task connect to invoicing {url}") == "task"
    assert classify(f"remind me at 3pm to watch {url}") == "reminder"

    # a real link capture is untouched — bare, and with a caption (the caption feeds enrichment)
    assert classify(url) == "link"
    assert classify(f"great reel on finance agents {url}") == "link"
    # an attachment keeps its text as one note rather than guessing a task
    assert classify("thoughts on this", has_media=True) == "note"
    assert classify("mumbled prose") == "unsorted"

    # A DECLARED kind outranks the url even when the parser declines the details. Each of
    # these bails a tier above the url branch ("this https" reads as when-language; a date
    # with no clock time isn't a reminder) — a decline hands the message to the ROUTER, and
    # the url branch must not intercept it on the way there. 'unsorted' is how the ladder
    # says "not mine": is_explicit_capture is False, so both surfaces route it to the router.
    for t in (f"add task: review this {url}",              # _WHEN_RE bails on "this <word>"
              f"todo buy milk and call the dentist {url}",   # only the router splits these
              f"add a task, call with Sam went well {url}",  # prose, not an instruction
              f"remind me on friday to watch {url}"):        # a date, no clock → router
        assert classify(t) == "unsorted", t
        assert not is_explicit_capture(t), t

    # has_url CONTAINS, is_bare_url IS — one test used to answer both, which is the bug above
    assert has_url(f"connect to my invoicing {url}") and not is_bare_url(f"connect to {url}")
    assert is_bare_url(url) and is_bare_url(f"  {url}  ")


def test_capture_endpoint(client, monkeypatch):
    # Auto natural-language text now runs the AI router (the web twin of the Telegram bot).
    import ai.claude_cli
    from ai import router
    # pin the gate: /capture only takes the AI path if has_claude(), which resolves the real
    # binary — unpinned, this test asserts the AI path on Sam's Mac and the note path on the NAS.
    monkeypatch.setattr(ai.claude_cli, "has_claude", lambda: True)
    monkeypatch.setattr(router, "call_claude",
                        lambda p, *a, **k: json.dumps({"action": "create_task", "title": "from web"}))
    r = client.post("/capture", data={"text": "remember to file from web", "type": "auto"})
    j = r.get_json()
    assert r.status_code == 200 and j["ai"] and "from web" in j["reply"]


def test_capture_without_claude_files_unsorted_note_instantly(client, monkeypatch):
    """The NAS container has no `claude` binary — that fall-through IS the production
    capture path, so it gets a test. Ambiguous prose must land as an #unsorted note
    without ever reaching the router (a claude call there would eat a timeout per capture)."""
    import ai.claude_cli
    from ai import router
    monkeypatch.setattr(ai.claude_cli, "has_claude", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("no-claude host must never reach the router")
    monkeypatch.setattr(router, "route", _boom)

    j = client.post("/capture", data={"text": "some vague musing to file", "type": "auto"}).get_json()
    assert j["kind"] == "note" and "unsorted" in j["tags"]
    assert not j.get("ai")


# ── notes round-trip to disk ──────────────────────────────────────────────────
def test_note_file_roundtrip_edit_delete(client):
    r = client.post("/notes/new", data={"title": "Rate card", "body": "hello", "tags": "business, idea"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    slug = r.get_json()["slug"]
    path = os.path.join(vault_store.notes_dir(), slug + ".md")
    assert os.path.exists(path)                             # file created on disk
    raw = open(path).read()
    assert raw.startswith("---") and "title: Rate card" in raw and "business" in raw
    # edit
    client.post(f"/notes/{slug}/save", data={"title": "Rate card 2026", "body": "world", "tags": "business"})
    n = vault_store.read_note(slug)
    assert n["title"] == "Rate card 2026" and "world" in n["body"]
    # soft-delete moves file to .trash, restore brings it back
    client.post(f"/notes/{slug}/delete")
    assert not os.path.exists(path)
    client.post(f"/notes/{slug}/restore")
    assert os.path.exists(path)


# ── journal append ────────────────────────────────────────────────────────────
def test_journal_append_lands_in_file(client):
    today = today_iso()
    r = client.post("/journal/entry", data={"text": "ate chicken rice", "day": today},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    path = vault_store.journal_path(today)
    assert os.path.exists(path)
    raw = open(path).read()
    assert "ate chicken rice" in raw and raw.count("##") >= 1     # timestamped header
    page = vault_store.read_journal(today)
    assert page["entries"] and page["entries"][-1]["text"] == "ate chicken rice"


def test_capture_journal_via_router(client):
    conn = _db()
    route_capture(conn, "second entry via capture", forced="journal")
    conn.close()
    page = vault_store.read_journal(today_iso())
    assert any("second entry" in e["text"] for e in page["entries"])


# ── goals rollup math ─────────────────────────────────────────────────────────
def test_goal_rollup_and_number(client):
    from domain.goals_core import goal_progress, current_period_start
    conn = _db()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('2 videos','month',?,'rollup',NULL,0,?)",
            (current_period_start("month"), now_iso()))
        gid = cur.lastrowid
        t1 = create_task(conn, "Video A", col="week", goal_id=gid)
        create_task(conn, "Video B", col="week", goal_id=gid)
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (t1,))
    g = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    prog = goal_progress(conn, g)
    assert prog["done"] == 1 and prog["total"] == 2 and prog["pct"] == 50
    conn.close()
    # number goal update endpoint
    conn = _db()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (title, period, period_start, kind, target_num, current_num, created) "
            "VALUES ('Newsletter','month',?,'number',500,438,?)",
            (current_period_start("month"), now_iso()))
        nid = cur.lastrowid
    conn.close()
    client.post(f"/goals/{nid}/update", data={"current": "450"})
    conn = _db()
    row = conn.execute("SELECT current_num FROM goals WHERE id=?", (nid,)).fetchone()
    conn.close()
    assert row["current_num"] == 450


def test_csrf_blocks_unprotected_post(client):
    """A raw client (no CSRF token) must be rejected on a mutating request.
    make_test_client() sets a CSRF-injecting client class globally, so restore the
    stock FlaskClient to prove the guard actually rejects a token-less POST."""
    from flask.testing import FlaskClient
    saved = web_core.app.test_client_class
    web_core.app.test_client_class = FlaskClient
    try:
        c = web_core.app.test_client()
        r = c.post("/capture", data={"text": "t: nope"})
    finally:
        web_core.app.test_client_class = saved
    assert r.status_code == 403


def test_capture_ai_patches_instead_of_reloading(client, monkeypatch):
    """The AI capture bar must hand back re-rendered cards so the page swaps ONE node.
    A full reload is the fallback of last resort, not the default for every mutation."""
    import json as _json
    import ai.claude_cli
    from ai import router
    from core.db import connect
    from domain.capture import create_task
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    with conn:
        tid = create_task(conn, "existing thing", col="week")
    conn.close()
    monkeypatch.setattr(ai.claude_cli, "has_claude", lambda: True)   # see test_capture_endpoint

    # a brand-new task splices via week_html — no reload
    monkeypatch.setattr(router, "call_claude",
                        lambda p, *a, **k: _json.dumps({"action": "create_task", "title": "fresh"}))
    j = client.post("/capture", data={"text": "something vague to file", "type": "auto"},
                    headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    assert j["ai"] and j["reload"] is False and j["week_html"]

    # changing an EXISTING task comes back as a card to swap, still no reload
    monkeypatch.setattr(router, "call_claude",
                        lambda p, *a, **k: _json.dumps({"action": "complete_task", "id": tid}))
    j = client.post("/capture", data={"text": "mark existing thing done", "type": "auto"},
                    headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    assert j["reload"] is False, "a task edit should patch in place, not reload"
    assert j["cards"] and j["cards"][0]["id"] == tid and j["cards"][0]["html"]

    # a DELETE renders no card — empty html tells the page to remove that node
    monkeypatch.setattr(router, "call_claude",
                        lambda p, *a, **k: _json.dumps({"action": "delete_task", "id": tid}))
    j = client.post("/capture", data={"text": "drop existing thing", "type": "auto"},
                    headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    assert j["cards"][0]["html"] == ""


def test_capture_questions_skip_claude(client, monkeypatch):
    """An unambiguous list question must be answered by the deterministic `queries` tier —
    the same one the phone bot uses. Web used to spend 5-10s of claude on these. If the
    router is reached at all this test fails loudly."""
    from ai import router
    from domain.capture import create_task

    def _boom(*a, **k):
        raise AssertionError("the AI router must not be reached for a list question")
    monkeypatch.setattr(router, "call_claude", _boom)

    conn = connect(os.environ["LIFEOS_DB_PATH"])
    with conn:
        create_task(conn, "an overdue thing", col="week", due_date="2020-01-01")
    conn.close()

    j = client.post("/capture", data={"text": "what's overdue?", "type": "auto"},
                    headers={"X-Requested-With": "XMLHttpRequest"}).get_json()
    assert j["status"] == "ok" and j["applied"] == ["answer"] and j["reload"] is False
    assert "an overdue thing" in j["reply"]


def test_upcoming_events_are_cached(monkeypatch):
    """The calendar is a live network call on EVERY capture once Google is connected —
    it must be cached, and a failure must never be cached (else one hiccup blinds the
    router for the whole TTL)."""
    from ai import router, google_client
    calls = []
    router._events_cache.clear()
    monkeypatch.setattr(router, "today_iso", lambda: "2026-07-15")
    monkeypatch.setattr(google_client, "is_configured", lambda: True)

    def _range(a, b):
        calls.append(1)
        return [{"summary": "standup"}]
    monkeypatch.setattr(google_client, "calendar_range", _range)

    assert router._upcoming_events(now=1000.0) == [{"summary": "standup"}]
    assert router._upcoming_events(now=1030.0) == [{"summary": "standup"}]   # within TTL
    assert len(calls) == 1, "a second capture inside the TTL must not re-hit the network"
    router._upcoming_events(now=1000.0 + router._EVENTS_TTL + 1)             # TTL expired
    assert len(calls) == 2

    # a failure must NOT be cached — one hiccup shouldn't blind the router for a whole TTL
    router._events_cache.clear()

    def _boom(a, b):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(google_client, "calendar_range", _boom)
    assert router._upcoming_events(now=2000.0) == []
    assert router._events_cache == {}, "a calendar hiccup must not be cached"
