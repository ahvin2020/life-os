"""Phase 4 Google (ai/google_client.py) against stub services — no SDK/OAuth needed.
Covers calendar/gmail parsing, draft creation, the NO-SEND invariant, the graceful
'not connected' path, and the router create_event (suggest-then-confirm) + draft_email.
"""

import inspect
import json
import os

from ai import router, google_client


def _db():
    from core.db import connect
    return connect(os.environ["LIFEOS_DB_PATH"])


class _Stub:
    """Records the method-call chain and returns canned data — stands in for a Google
    service resource so tests bypass auth entirely."""
    def __init__(self, result=None, sink=None):
        self._result = result or {}
        self.sink = sink if sink is not None else []
    def __getattr__(self, name):
        self.sink.append(name)
        return lambda *a, **k: self
    def execute(self):
        return self._result


def test_calendar_today_parses(client):
    svc = _Stub({"items": [
        {"summary": "Dentist", "start": {"dateTime": "2026-07-13T10:00:00+08:00"}},
        {"summary": "Holiday", "start": {"date": "2026-07-13"}}]})
    out = google_client.calendar_today("2026-07-13", service=svc)
    assert out[0]["summary"] == "Dentist" and out[0]["all_day"] is False
    assert out[1]["all_day"] is True


def test_calendar_range_parses_and_tags_date(client):
    svc = _Stub({"items": [
        {"summary": "Dentist", "start": {"dateTime": "2026-07-14T10:00:00+08:00"},
         "end": {"dateTime": "2026-07-14T11:00:00+08:00"}},
        {"summary": "Trip", "start": {"date": "2026-07-16"}, "end": {"date": "2026-07-17"}}]})
    out = google_client.calendar_range("2026-07-14", "2026-07-20", service=svc)
    assert out[0]["date"] == "2026-07-14" and out[0]["all_day"] is False
    assert out[1]["date"] == "2026-07-16" and out[1]["all_day"] is True


def test_calendar_events_route_not_connected(client, monkeypatch):
    monkeypatch.setattr(google_client, "is_configured", lambda: False)
    d = client.get("/calendar/events?start=2026-07-14&end=2026-07-14").get_json()
    assert d["connected"] is False and d["events"] == []


def test_calendar_events_route_returns_events(client, monkeypatch):
    monkeypatch.setattr(google_client, "is_configured", lambda: True)
    monkeypatch.setattr(google_client, "calendar_range",
                        lambda s, e: [{"summary": "Standup", "start": "2026-07-14T09:00:00+08:00",
                                       "all_day": False, "date": "2026-07-14"}])
    d = client.get("/calendar/events?start=2026-07-14&end=2026-07-14").get_json()
    assert d["connected"] is True and d["events"][0]["summary"] == "Standup"


def test_create_event_invites_guests(client):
    captured = {}
    class Svc:
        def events(self): return self
        def insert(self, calendarId=None, body=None, sendUpdates=None):
            captured["body"] = body; captured["send"] = sendUpdates; return self
        def execute(self): return {"id": "1", "htmlLink": "http://x"}
    google_client.create_event("Lunch", "2026-07-20", "12:00", None,
                               attendees=["a@b.com", ""], service=Svc())
    assert captured["body"]["attendees"] == [{"email": "a@b.com"}]
    assert captured["send"] == "all"          # Google emails the invite
    # no guests → no attendees key, no invites sent
    captured.clear()
    google_client.create_event("Solo", "2026-07-20", None, None, service=Svc())
    assert "attendees" not in captured["body"] and captured["send"] == "none"


def test_router_create_event_carries_guests(client):
    from ai import router
    conn = _db()
    act = {"action": "create_event", "title": "Sync", "date": "2026-07-20",
           "start": "10:00", "guests": ["lee.junkai@example.com", "notanemail"]}
    reply, _ = router.apply_action(conn, act, {"today": "2026-07-14"})
    assert "lee.junkai@example.com" in reply     # confirm prompt names the guest
    # the pending payload keeps only the valid email
    import json
    from core.db import get_setting
    pend = json.loads(get_setting(conn, "pending_action") or "{}")
    conn.close()
    assert pend.get("payload", {}).get("guests") == ["lee.junkai@example.com"]


def test_create_draft_builds_message_and_never_sends(client):
    calls = []
    svc = _Stub({"id": "draft1"}, sink=calls)
    out = google_client.create_draft("a@b.com", "Hi", "body", service=svc)
    assert out["id"] == "draft1"
    assert "drafts" in calls and "create" in calls and "send" not in calls


def test_module_has_no_send_call():
    # Hard invariant: the module must contain no messages().send / .send( anywhere.
    src = inspect.getsource(google_client)
    assert "messages().send" not in src and ".send(" not in src


def test_is_configured_false_without_token():
    assert google_client.is_configured() is False       # no token file in the test env


def test_message_attachments_finds_readable_files():
    """Booking detail (passenger manifest, e-ticket) lives in attachments, not the body —
    _message_attachments must surface pdf/image parts (with their attachmentId) and skip
    inline body parts + unreadable types."""
    payload = {"parts": [
        {"mimeType": "text/plain", "body": {"data": "x"}},                       # body, not an attachment
        {"mimeType": "application/pdf", "filename": "Itinerary AB12CD.pdf",
         "body": {"attachmentId": "att1"}},
        {"mimeType": "image/png", "filename": "boarding.png", "body": {"attachmentId": "att2"}},
        {"mimeType": "application/zip", "filename": "misc.zip", "body": {"attachmentId": "att3"}},  # not readable
    ]}
    atts = google_client._message_attachments(payload)
    names = {a["filename"] for a in atts}
    assert names == {"Itinerary AB12CD.pdf", "boarding.png"}        # zip skipped, body skipped
    ids = {a["filename"]: a["attachment_id"] for a in atts}
    assert ids["Itinerary AB12CD.pdf"] == "att1"


def test_gmail_search_returns_id_and_attachments(client):
    """body=True must carry the message id (needed to fetch an attachment) + the attachment
    list, so the retrieval loop can read the itinerary PDF."""
    payload = {"headers": [{"name": "Subject", "value": "Scoot booking"}],
               "parts": [{"mimeType": "application/pdf", "filename": "Itinerary.pdf",
                          "body": {"attachmentId": "a9"}}]}
    class _S:
        def users(self): return self
        def messages(self): return self
        def list(self, **k): self._m = "list"; return self
        def get(self, **k): self._m = "get"; return self
        def execute(self):
            return {"messages": [{"id": "m1"}]} if self._m == "list" else {"snippet": "s", "payload": payload}
    hits = google_client.gmail_search("scoot", service=_S(), body=True)
    assert hits[0]["id"] == "m1"
    assert hits[0]["attachments"][0]["filename"] == "Itinerary.pdf"


def test_router_create_event_is_suggest_then_confirm(client):
    conn = _db()
    out = router.route(conn, "put dentist on friday 10am", claude_fn=lambda p: json.dumps(
        {"action": "create_event", "title": "Dentist", "date": "2026-07-17",
         "start": "10:00", "end": None}))
    assert "Reply yes" in out["reply"]
    p = router.peek_pending(conn)
    conn.close()
    assert p["kind"] == "gcal_create" and p["payload"]["title"] == "Dentist"


def test_router_draft_email_graceful_when_unconfigured(client):
    conn = _db()
    out = router.route(conn, "draft a reply to the sponsor", claude_fn=lambda p: json.dumps(
        {"action": "draft_email", "to": "s@x.com", "subject": "Re", "body": "yes"}))
    conn.close()
    assert "isn't connected" in out["reply"]             # no OAuth in test → graceful
