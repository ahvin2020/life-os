"""On-demand idea pull from the clustered library.

Covers: topic→cluster mapping (incl. the "cpf" alias), the multi-concept candidate
pool builder (closest-cluster members UNION cross-cutting keyword hits), the Claude
selection-call JSON application, the deterministic fallback, the router emitting +
dispatching `library_ideas`, and the exchange-memory follow-up ("save #2 …" resolves
against the compact numbered titles stored in memory). Claude is always mocked.
"""

import json
import os

from domain import library
from ai import router
from domain import vault_store
from core.db import connect


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _lib_note(title, cluster, body="", created="2026-07-01T12:00:00+08:00", url=None):
    """Create an imported-style library note (carries one cluster tag)."""
    b = body or title
    if url:
        b = b + "\n" + url
    n = vault_store.create_note(title=title, body=b, tags=["video", "idea", "imported", cluster])
    # create_note stamps `created` = now; rewrite it so recency ordering is deterministic.
    vault_store.write_note(n["slug"], n["title"], n["tags"], b, False, created=created)
    return vault_store.read_note(n["slug"])


# ── topic → cluster mapping ────────────────────────────────────────────────────
def test_match_clusters_alias_cpf():
    assert "cpf-epf-retirement" in library.match_clusters("cpf")
    assert "cpf-epf-retirement" in library.match_clusters("about my CPF and retirement")


def test_match_clusters_name_token_and_alias():
    assert library.match_clusters("options") == ["options-trading"]
    assert "brokers-platforms" in library.match_clusters("which broker")
    assert "banks-cards" in library.match_clusters("bank promos")


def test_match_clusters_multi_concept():
    cs = library.match_clusters("editing with ai")
    assert "creator-craft" in cs and "ai-investing-tools" in cs


def test_match_clusters_unmapped_is_empty():
    assert library.match_clusters("something totally unrelated zzz") == []


# ── multi-concept candidate pool builder ───────────────────────────────────────
def test_build_pool_unions_cluster_and_keyword_hits(client):
    # closest cluster for "editing with ai" is creator-craft (+ ai-investing-tools)
    craft = _lib_note("Faster thumbnails workflow", "creator-craft")
    ai = _lib_note("Best AI tool for scripts", "ai-investing-tools")
    # a cross-cutting caption tagged ELSEWHERE — only reachable via OR-keyword recall
    cross = _lib_note("Micron earnings recap", "market-investing",
                      body="cut your edit time in half with this tool")
    # a genuinely unrelated note that must NOT enter the pool
    off = _lib_note("HDB downpayment math", "property-housing")

    notes = library.imported_notes()
    pool = library.build_pool("editing with ai", notes)
    slugs = {n["slug"] for n in pool}

    assert craft["slug"] in slugs            # closest-cluster member
    assert ai["slug"] in slugs               # second matched cluster
    assert cross["slug"] in slugs            # keyword hit 'editing'→'edit time', other cluster
    assert off["slug"] not in slugs          # unrelated stays out


def test_build_pool_caps_by_recency(client):
    for i in range(90):
        _lib_note(f"CPF note {i:02d}", "cpf-epf-retirement",
                  created=f"2026-05-{(i % 28) + 1:02d}T12:00:00+08:00")
    notes = library.imported_notes()
    pool = library.build_pool("cpf", notes, cap=80)
    assert len(pool) == 80                   # trimmed to the cap
    # pool preserves newest-first order (list_notes sorts by created desc)
    assert pool[0]["created"] >= pool[-1]["created"]


# ── selection-call JSON application ────────────────────────────────────────────
def test_pull_ideas_applies_selection_json(client):
    a = _lib_note("What you may not know about CPF", "cpf-epf-retirement",
                  created="2026-07-05T12:00:00+08:00",
                  url="https://www.instagram.com/reel/AAA/")
    b = _lib_note("CPF vs SRS explained", "cpf-epf-retirement",
                  created="2026-07-06T12:00:00+08:00",
                  url="https://www.instagram.com/reel/BBB/")
    _lib_note("Unrelated options wheel", "options-trading")

    def fake(prompt):
        return json.dumps({"picks": [
            {"slug": b["slug"], "why": "Timely — SRS top-up season"},
            {"slug": a["slug"], "why": "Evergreen CPF explainer"}]})

    conn = _db()
    reply, mem = library.pull_ideas(conn, "cpf", count=2, claude_fn=fake)
    conn.close()
    assert reply.startswith("💡 2 ideas about cpf:")
    assert "CPF vs SRS explained" in reply and "What you may not know about CPF" in reply
    assert "https://www.instagram.com/reel/BBB/" in reply     # url on its own line
    assert "Timely — SRS top-up season" in reply
    # compact memory carries the numbering + titles for follow-ups
    assert "1. CPF vs SRS explained" in mem and "2. What you may not know about CPF" in mem


def test_pull_ideas_passes_raw_phrasing_to_claude(client):
    _lib_note("Some AI editing note", "creator-craft",
              body="edit your videos with AI")
    box = []

    def fake(prompt):
        box.append(prompt)
        return json.dumps({"picks": []})     # force a fallback, we only care about the prompt

    conn = _db()
    library.pull_ideas(conn, "editing with ai", claude_fn=fake)
    conn.close()
    assert 'His exact words: "editing with ai"' in box[0]
    assert "CANDIDATE SAVES" in box[0]


# ── deterministic fallback (claude fails / empty picks) ────────────────────────
def test_pull_ideas_fallback_on_failure(client):
    old = _lib_note("Old CPF idea", "cpf-epf-retirement", created="2026-06-01T12:00:00+08:00")
    mid = _lib_note("Mid CPF idea", "cpf-epf-retirement", created="2026-06-15T12:00:00+08:00")
    new = _lib_note("New CPF idea", "cpf-epf-retirement", created="2026-07-01T12:00:00+08:00")

    def boom(prompt):
        raise RuntimeError("claude down")

    conn = _db()
    reply, mem = library.pull_ideas(conn, "cpf", count=3, claude_fn=boom)
    conn.close()
    # fallback = the N most recent notes in the matched cluster, newest first
    assert "New CPF idea" in reply and "Mid CPF idea" in reply and "Old CPF idea" in reply
    assert reply.index("New CPF idea") < reply.index("Old CPF idea")
    assert new and mid and old


def test_pull_ideas_empty_topic_pool(client):
    """No saves about a topic → a graceful message, never a crash."""
    _lib_note("A CPF note", "cpf-epf-retirement")

    def fake(prompt):
        return json.dumps({"picks": []})

    conn = _db()
    reply, mem = library.pull_ideas(conn, "quantum tunnelling zzz", claude_fn=fake)
    conn.close()
    assert reply.startswith("🔍 Nothing saved about")
    assert mem == ""


# ── router emits + dispatches library_ideas ────────────────────────────────────
def test_router_dispatches_library_ideas(client, monkeypatch):
    captured = {}

    def stub(conn, topic, count=None, claude_fn=None):
        captured["topic"] = topic
        captured["count"] = count
        return ("💡 stub reply", "Ideas about cpf: 1. A 2. B")

    monkeypatch.setattr(library, "pull_ideas", stub)
    conn = _db()
    out = router.route(conn, "give me 5 ideas about cpf", claude_fn=lambda p: json.dumps(
        {"action": "library_ideas", "topic": "cpf", "count": 5}))
    # the compact memory (not the sent reply) is what gets stored for follow-ups
    pairs = router.load_exchanges(conn)
    conn.close()
    assert out["reply"] == "💡 stub reply"
    assert captured["topic"] == "cpf" and captured["count"] == 5
    assert pairs[-1]["b"].startswith("Ideas about cpf: 1. A")


def test_router_library_shelf_line_in_context(client):
    conn = _db()
    ctx = router.build_context(conn)
    conn.close()
    # the shelf census (from data/cluster_log.json) is injected for the model
    assert "IDEA LIBRARY" in ctx["text"]


# ── exchange-memory follow-up: "save #2 as a task to film" resolves ────────────
def test_followup_save_second_idea_resolves(client):
    """End-to-end: an idea pull stores compact numbered titles in memory; the next turn
    replays them so 'save #2' can create a task with the right title."""
    a = _lib_note("Idea one about CPF", "cpf-epf-retirement", created="2026-07-05T12:00:00+08:00")
    b = _lib_note("Idea two about CPF", "cpf-epf-retirement", created="2026-07-06T12:00:00+08:00")

    def turn1_claude(prompt):
        if "CANDIDATE SAVES" in prompt:              # the library selection call
            return json.dumps({"picks": [
                {"slug": a["slug"], "why": "why one"},
                {"slug": b["slug"], "why": "why two"}]})
        return json.dumps({"action": "library_ideas", "topic": "cpf", "count": 2})

    conn = _db()
    router.route(conn, "give me 2 ideas about cpf", claude_fn=turn1_claude)

    # memory must hold the numbered titles (resolvable, under the per-entry cap)
    pairs = router.load_exchanges(conn)
    assert "2. Idea two about CPF" in pairs[-1]["b"]
    assert len(pairs[-1]["b"]) <= router._MEM_ENTRY_CAP

    # Turn 2: "save #2 as a task to film" — the prompt must replay the numbered titles so
    # real Claude could resolve #2; the mock (standing in for Claude) creates that task.
    box = []

    def turn2_claude(prompt):
        box.append(prompt)
        return json.dumps({"action": "create_task", "title": "Film: Idea two about CPF",
                           "category": "content"})

    out = router.route(conn, "save #2 as a task to film", claude_fn=turn2_claude)
    task = conn.execute("SELECT title FROM tasks WHERE title=?",
                        ("Film: Idea two about CPF",)).fetchone()
    conn.close()
    assert "2. Idea two about CPF" in box[0]          # numbered titles replayed into context
    assert task is not None                           # the follow-up created the right task
    assert out["applied"] == ["create_task"]


# ── queries.py does not swallow idea-pull messages ─────────────────────────────
def test_queries_ignores_idea_pulls():
    from domain.queries import is_query
    assert is_query("give me 5 ideas about cpf") is False
    assert is_query("find me some ideas for my next video") is False
    assert is_query("what have i saved about bank promos") is False
    # a genuine deterministic query is unaffected
    assert is_query("what are my todos") is True
    assert is_query("any overdue?") is True


# ── web /notes/ask (structured sibling of pull_ideas) ──────────────────────────
def test_rank_notes_structured_picks(client):
    c1 = _lib_note("CPF top-up hacks", "cpf-epf-retirement", created="2026-07-05T09:00:00+08:00",
                   url="https://youtu.be/abc")
    _lib_note("Retirement sum explained", "cpf-epf-retirement", created="2026-07-01T09:00:00+08:00")

    def fake(prompt):
        return json.dumps({"picks": [{"slug": c1["slug"], "why": "punchy CPF hook"}]})

    results, fallback = library.rank_notes("cpf", claude_fn=fake)
    assert fallback is False and len(results) == 1
    r = results[0]
    assert set(r.keys()) == {"slug", "title", "why", "url", "cluster"}
    assert r["slug"] == c1["slug"] and r["why"] == "punchy CPF hook"
    assert r["cluster"] == "cpf-epf-retirement" and r["url"] == "https://youtu.be/abc"


def test_rank_notes_fallback_on_claude_failure(client):
    newer = _lib_note("Newer CPF idea", "cpf-epf-retirement", created="2026-07-06T09:00:00+08:00")
    _lib_note("Older CPF idea", "cpf-epf-retirement", created="2026-07-01T09:00:00+08:00")

    def boom(prompt):
        raise RuntimeError("claude down")

    results, fallback = library.rank_notes("cpf", claude_fn=boom)
    assert fallback is True and results
    assert results[0]["slug"] == newer["slug"]          # newest-first recency fallback
    assert results[0]["why"] == ""                       # fallback carries no why


def test_rank_notes_empty_topic_no_match(client):
    # empty library → nothing to rank
    results, fallback = library.rank_notes("cpf")
    assert results == [] and fallback is False


def test_notes_ask_endpoint_shape(client):
    r = client.post("/notes/ask", data={"q": ""},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["status"] == "ok" and j["html"] == "" and j["count"] == 0
    assert j["fallback"] is False and j["q"] == ""


def test_notes_ask_renders_real_note_cards(client):
    # Claude is unavailable in tests → deterministic recency fallback, but the endpoint
    # still renders the SAME note_card macro as the grid (the visual-parity fix).
    c1 = _lib_note("CPF top-up hacks", "cpf-epf-retirement",
                   created="2026-07-05T09:00:00+08:00", url="https://youtu.be/abc")
    r = client.post("/notes/ask", data={"q": "cpf"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["count"] >= 1
    # Same macro as the grid → same card DOM (note card in an .ngrid, tag pill, data-slug).
    assert 'class="ngrid"' in j["html"] and 'class="note' in j["html"]
    assert 'class="ntag"' in j["html"]
    assert f'data-slug="{c1["slug"]}"' in j["html"]
