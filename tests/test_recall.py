"""Vault recall (domain/recall.py) + the router vault_recall action.

Retrieval is deterministic over the throwaway test vault; the single synthesis call is
mocked. Covers term scoring, date windows, excerpt windowing, the no-hits short-circuit
(NO claude call), claude-failure fallback, and the router wiring.
"""

import json
import os

from ai import router
from domain import recall, vault_store
from core.db import connect


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


def _note(slug, title, body, created, tags=None):
    # write_note lets us backdate `created` so date-window tests are deterministic.
    vault_store.write_note(slug, title, tags or [], body, False,
                           created=created + "T09:00:00+08:00")


def test_search_scores_and_orders_by_terms(client):
    _note("aircon", "Aircon service", "Serviced the aircon, replaced the filter", "2026-03-10")
    _note("reno", "Kitchen reno", "decided to go with the oak worktop", "2026-05-01")
    hits = recall.search_vault(["aircon", "filter"])
    assert hits and hits[0]["ref"] == "aircon"
    assert hits[0]["score"] == 2                       # both terms hit
    assert "filter" in hits[0]["excerpt"]


def test_search_date_window_filters(client):
    _note("old", "Old note", "budget planning", "2026-01-05")
    _note("new", "New note", "budget planning", "2026-06-05")
    hits = recall.search_vault(["budget"], since="2026-06-01")
    assert [h["ref"] for h in hits] == ["new"]


def test_search_journal_window_without_terms(client):
    vault_store.append_journal_entry("2026-04-02", "felt good after the run")
    hits = recall.search_vault([], since="2026-04-01", until="2026-04-30")
    assert any(h["kind"] == "journal" and h["ref"] == "2026-04-02" for h in hits)


def test_no_hits_short_circuits_without_claude(client):
    def boom(_p):
        raise AssertionError("claude must not be called when there are no hits")
    out = recall.recall_answer(_db(), "when did I service the boat?", ["boat"],
                               claude_fn=boom)
    assert "couldn't find" in out.lower()


def test_recall_answer_uses_excerpts_and_falls_back(client):
    _note("aircon", "Aircon service", "serviced the aircon in March", "2026-03-10")
    box = []
    out = recall.recall_answer(_db(), "when did I last service the aircon?", ["aircon"],
                               claude_fn=lambda p: box.append(p) or "You serviced it on 2026-03-10.")
    assert "2026-03-10" in out
    assert "DATA" in box[0] and "aircon" in box[0].lower()     # excerpts + rail present

    # claude failure → deterministic fallback, never a dropped reply
    out2 = recall.recall_answer(_db(), "aircon?", ["aircon"],
                                claude_fn=lambda p: (_ for _ in ()).throw(RuntimeError()))
    assert "your vault has" in out2.lower()


def test_router_vault_recall_action(client):
    _note("reno", "Kitchen reno", "decided on the oak worktop, budget 12k", "2026-05-01")
    conn = _db()
    # Stateful fake: first call returns the action JSON, second is the synthesis call.
    calls = []

    def fake(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps({"action": "vault_recall", "question": "what did I decide about the reno?",
                               "terms": ["reno", "worktop"], "since": None, "until": None})
        return "You chose the oak worktop (2026-05-01)."

    out = router.route(conn, "what did I decide about the reno?", claude_fn=fake)
    conn.close()
    assert out["applied"] == ["vault_recall"]
    assert "oak worktop" in out["reply"]
    assert len(calls) == 2                             # router decide + recall synthesis
