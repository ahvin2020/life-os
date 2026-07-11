"""Telegram reply quality: the instant-ack→edit link flow and the title-forward
capture confirmations. The Telegram API and the slow claude enrichment are both faked,
so these run offline and deterministically (the enrichment thread is made synchronous)."""

import os

import capture
import capture_daemon as cd
import vault_store
from db import connect


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


class _FakeTG:
    """Records what the bot sent and edited."""

    def __init__(self):
        self.sent, self.edited, self._id = [], [], 100

    def send_message(self, chat_id, text, reply_markup=None):
        self._id += 1
        self.sent.append((chat_id, text))
        return {"result": {"message_id": self._id}}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text))
        return {"ok": True}

    def send_chat_action(self, *a, **k):
        pass


class _SyncThread:
    """Run the enrichment 'thread' inline so the test is deterministic."""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


# ── the link flow ─────────────────────────────────────────────────────────────
def test_link_acks_instantly_then_edits_to_rich_reply(client, monkeypatch):
    monkeypatch.setattr(capture, "_url_title", lambda t: "raw yt title")
    monkeypatch.setattr(capture, "_enrich_enabled", lambda: True)
    monkeypatch.setattr(capture, "enrich_link", lambda slug: (
        {"title": "5 SG dividend stocks explained",
         "tags": ["link", "idea", "market-investing"],
         "body": "https://youtu.be/x\n\nGreat angle", "url": "https://youtu.be/x"},
        "Strong index-concentration explainer for the channel"))
    monkeypatch.setattr(cd.threading, "Thread", _SyncThread)

    tg = _FakeTG()
    conn = _db()
    cd._handle_link(conn, tg, 42, "https://youtu.be/x")
    conn.close()

    assert tg.sent[0] == (42, "📎 Saved — reading it…")   # instant ack first
    rich = tg.edited[-1][2]                                # then edited in place
    assert "5 SG dividend stocks explained" in rich
    assert "Strong index-concentration explainer" in rich
    assert "#market-investing" in rich and "#idea" in rich
    assert "#link" not in rich                             # plumbing tag hidden
    assert "https://youtu.be/x" in rich                   # URL last → preview


def test_link_reshare_reports_already_saved(client, monkeypatch):
    vault_store.create_note(title="Existing clip", body="https://youtu.be/x", tags=["link"])
    monkeypatch.setattr(capture, "_url_title", lambda t: "unused")
    monkeypatch.setattr(cd.threading, "Thread", _SyncThread)

    tg = _FakeTG()
    conn = _db()
    cd._handle_link(conn, tg, 1, "https://youtu.be/x?utm_source=share")   # same link, tracking tail
    conn.close()

    assert tg.edited[-1][2] == "📎 Already saved: Existing clip"


def test_link_enrichment_disabled_degrades_to_plain_save(client, monkeypatch):
    # conftest sets LIFEOS_ENRICH_LINKS=0 → _enrich_enabled() is False
    monkeypatch.setattr(capture, "_url_title", lambda t: "YT clip title")
    monkeypatch.setattr(cd.threading, "Thread", _SyncThread)

    tg = _FakeTG()
    conn = _db()
    cd._handle_link(conn, tg, 1, "https://youtu.be/z")
    conn.close()

    assert tg.edited[-1][2] == "📎 Saved: YT clip title"   # no dangling "reading it…"


def test_link_enrichment_failure_degrades_to_plain_save(client, monkeypatch):
    monkeypatch.setattr(capture, "_url_title", lambda t: "fallback title")
    monkeypatch.setattr(capture, "_enrich_enabled", lambda: True)
    monkeypatch.setattr(capture, "enrich_link", lambda slug: (None, ""))   # claude down
    monkeypatch.setattr(cd.threading, "Thread", _SyncThread)

    tg = _FakeTG()
    conn = _db()
    cd._handle_link(conn, tg, 1, "https://youtu.be/z")
    conn.close()

    assert tg.edited[-1][2] == "📎 Saved: fallback title"


# ── title-forward capture confirmations ───────────────────────────────────────
def test_format_reply_leads_with_the_item():
    assert cd.format_reply({"kind": "task", "title": "buy milk", "priority": None}) == "✓ Task: buy milk"
    assert cd.format_reply({"kind": "task", "title": "call bank", "priority": "high"}) == "✓ Task: call bank · high"
    assert cd.format_reply({"kind": "note", "title": "a thought"}) == "📝 Saved: a thought"
    assert cd.format_reply({"kind": "journal"}) == "✦ Added to today's journal"


def test_prefix_task_reply_names_the_task(client, monkeypatch):
    monkeypatch.setattr(cd.threading, "Thread", _SyncThread)
    tg = _FakeTG()
    conn = _db()
    cd._handle_text(conn, tg, 1, "t: renew passport")
    conn.close()
    assert tg.sent[-1] == (1, "✓ Task: renew passport")
