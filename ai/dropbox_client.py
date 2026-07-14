"""Dropbox integration — portable document access via Dropbox's own API + OAuth.

Unlike the local-folder approach (which assumes Synology Cloud Sync), this connects any
Dropbox account with a browser button: paste the app key/secret once (from a one-time
Dropbox app registration), click Connect, and the token is stored server-side. Document
search / retrieval / the facts scan then work against the Dropbox API directly — no NAS
sync required, so it's portable to anyone's setup.

Single-account by design (one connected Dropbox), matching the app's single-user model.
All SDK imports are deferred so the app stays Flask-only until Dropbox is configured;
every data function takes an injectable `client` so tests never touch the network.
"""

from __future__ import annotations

import os
import tempfile

from core.db import get_setting

_DOC_EXTS = (".pdf", ".md", ".txt", ".jpg", ".jpeg", ".png", ".csv", ".docx", ".xlsx")


def sdk_available() -> bool:
    try:
        import dropbox  # noqa: F401
        return True
    except ImportError:
        return False


def is_configured(conn) -> bool:
    return bool(get_setting(conn, "dropbox_token")) and sdk_available()


def build_flow(app_key: str, app_secret: str, redirect_uri: str, sess):
    """A redirect OAuth flow bound to the Flask session (CSRF). token_access_type=offline
    yields a refresh token so the connection survives without re-consent."""
    from dropbox import DropboxOAuth2Flow
    return DropboxOAuth2Flow(app_key, redirect_uri, sess, "dropbox-auth-csrf-token",
                             consumer_secret=app_secret, token_access_type="offline")


def _client(conn, client=None):
    if client is not None:
        return client
    import dropbox
    return dropbox.Dropbox(oauth2_refresh_token=get_setting(conn, "dropbox_token"),
                           app_key=get_setting(conn, "dropbox_app_key"),
                           app_secret=get_setting(conn, "dropbox_app_secret"))


def _beat(ok: bool, err: str = "") -> None:
    """Record a Dropbox connection heartbeat (real calls only) for the Settings status."""
    try:
        from core.db import connect, set_setting, delete_setting, now_iso
        c = connect()
        if ok:
            set_setting(c, "dropbox_last_ok", now_iso())
            delete_setting(c, "dropbox_last_err")
        else:
            set_setting(c, "dropbox_last_err", (err or "failed")[:150])
        c.close()
    except Exception:
        pass


def search(conn, query: str, limit: int = 5, client=None) -> list:
    """Server-side filename search. Returns [{name, dbx_path, source:'dropbox'}]. []- on
    any failure so a Dropbox outage never breaks the local search."""
    try:
        dbx = _client(conn, client)
        # Filename-only: without this, Dropbox ranks CONTENT matches first, burying real
        # filename hits (a passport .png vanished under OCR'd invoices that mention 'passport').
        try:
            from dropbox.files import SearchOptions
            res = dbx.files_search_v2(query, options=SearchOptions(filename_only=True))
        except Exception:
            res = dbx.files_search_v2(query)
        out = []
        for m in res.matches:
            md = getattr(m.metadata, "get_metadata", lambda: None)()
            if md is None:
                md = getattr(m.metadata, "metadata", None)
            name = getattr(md, "name", "")
            path = getattr(md, "path_lower", None) or getattr(md, "path_display", None)
            if name and path and name.lower().endswith(_DOC_EXTS):
                out.append({"name": name, "dbx_path": path, "source": "dropbox"})
            if len(out) >= limit:
                break
        if client is None:
            _beat(True)
        return out
    except Exception as e:
        if client is None:
            _beat(False, str(e))
        return []


def download_tmp(conn, dbx_path: str, client=None) -> str | None:
    """Download a Dropbox file to a temp path (for sending or reading). None on failure."""
    try:
        dbx = _client(conn, client)
        fd, tmp = tempfile.mkstemp(suffix="-" + os.path.basename(dbx_path))
        os.close(fd)
        dbx.files_download_to_file(tmp, dbx_path)
        return tmp
    except Exception:
        return None


def temporary_link(conn, dbx_path: str, client=None) -> str | None:
    """A Dropbox-hosted temporary link (the 'link' delivery mode for a Dropbox file)."""
    try:
        dbx = _client(conn, client)
        return dbx.files_get_temporary_link(dbx_path).link
    except Exception:
        return None


def list_documents(conn, cap: int = 2000, client=None) -> list:
    """Every document file in the account (recursive) — for the facts scan. [{name,
    dbx_path, rev}] so the scan can skip already-seen revisions."""
    try:
        dbx = _client(conn, client)
        out = []
        res = dbx.files_list_folder("", recursive=True)
        while True:
            for e in res.entries:
                name = getattr(e, "name", "")
                if name.lower().endswith(_DOC_EXTS) and getattr(e, "path_lower", None):
                    out.append({"name": name, "dbx_path": e.path_lower,
                                "rev": getattr(e, "rev", "")})
                if len(out) >= cap:
                    return out
            if not res.has_more:
                break
            res = dbx.files_list_folder_continue(res.cursor)
        return out
    except Exception:
        return []


def forget(conn) -> None:
    from core.db import delete_setting
    delete_setting(conn, "dropbox_token")
