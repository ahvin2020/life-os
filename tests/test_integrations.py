"""Connect/disconnect for Google + Dropbox (Settings routes) and the Dropbox document
source unioned into docs. The provider SDKs aren't installed in the test env, so the
connect routes exercise the graceful 'not installed / no creds' paths; data functions use
injected clients."""

import os

from core.db import connect, get_setting, set_setting
from domain import docs
from ai import google_client, dropbox_client


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


# ── Settings: credentials + connect/disconnect ────────────────────────────────
def test_settings_shows_integration_cards(client):
    html = client.get("/settings").data.decode()
    assert "Connections" in html and ">Google<" in html and ">Dropbox<" in html
    assert 'action="/settings/dropbox-creds"' in html
    assert 'class="brand"' in html                    # logo marks render
    # State machine: fresh (no creds) shows Save credentials, NOT a Connect button yet.
    assert "Save credentials" in html
    assert 'href="/settings/google/connect"' not in html


def test_connect_appears_once_creds_saved(client):
    conn = _db()
    set_setting(conn, "google_client_id", "cid")
    set_setting(conn, "google_client_secret", "csec")
    conn.close()
    html = client.get("/settings").data.decode()
    assert 'href="/settings/google/connect"' in html   # creds saved → Connect becomes the action
    assert "Credentials saved" in html                 # setup collapses to a Replace line


def test_nav_badge_nudges_half_finished_only(client):
    """Saving creds without connecting counts toward the Settings nav badge (finish your
    setup); an untouched optional integration does not nag."""
    from core.web_core import _integration_pending
    conn = _db()
    assert _integration_pending(conn) == 0                 # nothing set up → no nag
    set_setting(conn, "dropbox_app_key", "k")
    set_setting(conn, "dropbox_app_secret", "s")
    assert _integration_pending(conn) == 1                 # creds saved, not connected → nudge
    set_setting(conn, "dropbox_token", "tok")
    assert _integration_pending(conn) == 0                 # connected → nudge clears
    conn.close()


def test_nav_badge_counts_connected_but_failing(client, monkeypatch):
    from core.web_core import _integration_pending
    conn = _db()
    monkeypatch.setattr(google_client, "is_configured", lambda: True)   # connected
    assert _integration_pending(conn) == 0                              # healthy → no nag
    set_setting(conn, "google_last_err", "401 unauthorized")           # a call failed
    assert _integration_pending(conn) == 1                              # connected-but-failing → nag
    conn.close()


def test_google_heartbeat_records_and_clears(client, monkeypatch):
    conn = _db()
    monkeypatch.setattr(google_client, "_service",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    google_client.gmail_highlights()                # real call (service=None) fails → records error
    assert get_setting(conn, "google_last_err")
    google_client._beat(True)                        # a later success clears it
    assert not get_setting(_db(), "google_last_err")
    conn.close()


def test_telegram_user_only_update_keeps_token(client):
    conn = _db()
    set_setting(conn, "telegram_bot_token", "existing-token")
    conn.close()
    client.post("/settings/telegram-creds",
                data={"telegram_bot_token": "", "telegram_allowed_user": "999"})
    conn = _db()
    assert get_setting(conn, "telegram_allowed_user") == "999"
    assert get_setting(conn, "telegram_bot_token") == "existing-token"   # token preserved
    conn.close()


def test_settings_test_route(client, monkeypatch):
    from ai import telegram_api
    sent = {}
    class FakeTG:
        def __init__(self, t): pass
        def send_message(self, chat_id, text, reply_markup=None):
            sent["chat"] = chat_id; sent["text"] = text
            return {"ok": True, "result": {"message_id": 1}}
    monkeypatch.setattr(telegram_api, "Telegram", FakeTG)
    conn = _db()
    set_setting(conn, "telegram_bot_token", "x")
    set_setting(conn, "telegram_allowed_user", "933")
    conn.close()
    ajax = {"X-Requested-With": "XMLHttpRequest"}
    ok = client.post("/settings/test/telegram", headers=ajax)
    assert ok.get_json()["status"] == "ok" and "Telegram" in ok.get_json()["message"]
    assert sent["chat"] == "933"                          # a real message went to the allowed chat
    assert client.post("/settings/test/nope", headers=ajax).status_code == 400
    # unconnected google → "Not connected"
    ng = client.post("/settings/test/google", headers=ajax)
    assert ng.status_code == 400


def test_google_creds_save(client):
    client.post("/settings/google-creds", data={"google_client_id": "cid.apps",
                                                "google_client_secret": "csec"})
    conn = _db()
    assert get_setting(conn, "google_client_id") == "cid.apps"
    assert get_setting(conn, "google_client_secret") == "csec"
    conn.close()


def test_google_connect_without_creds_redirects(client):
    r = client.get("/settings/google/connect")
    assert r.status_code in (301, 302) and "/settings" in r.headers["Location"]


def test_google_connect_without_sdk_redirects(client):
    # creds present but the SDK isn't installed → graceful redirect, no crash
    client.post("/settings/google-creds", data={"google_client_id": "c", "google_client_secret": "s"})
    r = client.get("/settings/google/connect")
    assert r.status_code in (301, 302)


def test_google_disconnect_removes_token(client, tmp_path, monkeypatch):
    tok = tmp_path / "google_token.json"
    tok.write_text("{}")
    monkeypatch.setattr(google_client, "_TOKEN", str(tok))
    client.post("/settings/google/disconnect")
    assert not tok.exists()


def test_dropbox_creds_and_disconnect(client):
    client.post("/settings/dropbox-creds", data={"dropbox_app_key": "k", "dropbox_app_secret": "s"})
    conn = _db()
    set_setting(conn, "dropbox_token", "refresh-tok")
    conn.close()
    client.post("/settings/dropbox/disconnect")
    conn = _db()
    assert get_setting(conn, "dropbox_token") is None
    conn.close()


def test_google_save_token_and_forget(client, tmp_path, monkeypatch):
    tok = tmp_path / "t.json"
    monkeypatch.setattr(google_client, "_TOKEN", str(tok))
    google_client.save_token('{"refresh_token":"x"}')
    assert tok.exists()
    google_client.forget_token()
    assert not tok.exists()


# ── Dropbox document source unioned into docs ─────────────────────────────────
class _FakeDbxClient:
    class _Match:
        def __init__(self, name, path):
            self.metadata = type("M", (), {"metadata": type("F", (), {
                "name": name, "path_lower": path})()})()
    def files_search_v2(self, query):
        return type("R", (), {"matches": [self._Match("Scoot booking.pdf", "/docs/scoot.pdf")]})()
    def files_get_temporary_link(self, path):
        return type("L", (), {"link": "https://dropbox.com/tmp/scoot"})()


def test_dropbox_search_parses(client):
    conn = _db()
    hits = dropbox_client.search(conn, "scoot", client=_FakeDbxClient())
    conn.close()
    assert hits and hits[0]["dbx_path"] == "/docs/scoot.pdf" and hits[0]["source"] == "dropbox"


def test_docs_search_unions_dropbox(client, monkeypatch):
    conn = _db()
    monkeypatch.setattr(dropbox_client, "is_configured", lambda c: True)
    monkeypatch.setattr(dropbox_client, "search",
                        lambda c, q, limit=5, client=None: [
                            {"name": "Scoot booking.pdf", "dbx_path": "/scoot.pdf", "source": "dropbox"}])
    hits = docs.search_documents(conn, "scoot booking")
    conn.close()
    assert any(h.get("source") == "dropbox" for h in hits)


def test_link_for_hit_branches_on_source(client, monkeypatch):
    conn = _db()
    monkeypatch.setattr(dropbox_client, "temporary_link",
                        lambda c, p, client=None: "https://dropbox.com/tmp/x")
    link = docs.link_for_hit(conn, {"source": "dropbox", "dbx_path": "/x.pdf"})
    assert link.startswith("https://dropbox.com")
    conn.close()
