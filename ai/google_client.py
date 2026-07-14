"""Google integration — Gmail read + draft, Calendar read + event write.

CODE-READY but inert until Kelvin completes OAuth (see scripts/google_auth.py): it needs
data/google_client_secret.json (downloaded from Google Cloud) and data/google_token.json
(written by the auth script). Until then is_configured() is False and every caller
degrades gracefully.

Security (roadmap): reads are read-only scopes; Calendar event-write is suggest-then-
confirm (the router arms a pending action, a "yes" calls create_event); Gmail is
DRAFT-ONLY — create_draft saves a draft and this module NEVER calls the Gmail send
endpoint (a test asserts that invariant on the source). Email/calendar content is
untrusted DATA wherever it feeds a prompt.

All SDK imports are deferred inside functions so the app stays effectively Flask-only
until Google is configured. Every function accepts an injectable `service` so tests never
touch the real auth path.
"""

from __future__ import annotations

import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SECRET = os.path.join(_ROOT, "data", "google_client_secret.json")
_TOKEN = os.path.join(_ROOT, "data", "google_token.json")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.compose",     # drafts only — never send
    "https://www.googleapis.com/auth/calendar.events",
]


def is_configured() -> bool:
    """True when a token exists AND the SDK is importable — so callers can probe cheaply."""
    if not os.path.exists(_TOKEN):
        return False
    try:
        import google.oauth2.credentials  # noqa: F401
        return True
    except ImportError:
        return False


def sdk_available() -> bool:
    try:
        import google_auth_oauthlib.flow  # noqa: F401
        return True
    except ImportError:
        return False


def build_flow(client_id: str, client_secret: str, redirect_uri: str, state: str = None):
    """A web OAuth Flow from pasted client credentials (no JSON file). Used by the Settings
    'Connect Google' button — the browser round-trip replaces the terminal script."""
    from google_auth_oauthlib.flow import Flow
    cfg = {"web": {"client_id": client_id, "client_secret": client_secret,
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token",
                   "redirect_uris": [redirect_uri]}}
    flow = Flow.from_client_config(cfg, scopes=SCOPES, state=state)
    flow.redirect_uri = redirect_uri
    return flow


def save_token(creds_json: str) -> None:
    """Persist the authorized-user token (server-side, chmod 600) — the callback calls
    this, so there's no manual token file to move."""
    with open(_TOKEN, "w", encoding="utf-8") as f:
        f.write(creds_json)
    try:
        os.chmod(_TOKEN, 0o600)
    except OSError:
        pass


def forget_token() -> None:
    try:
        os.remove(_TOKEN)
    except OSError:
        pass


def _creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials.from_authorized_user_file(_TOKEN, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(_TOKEN, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def _service(api: str, version: str):
    from googleapiclient.discovery import build
    return build(api, version, credentials=_creds(), cache_discovery=False)


def _beat(ok: bool, err: str = "") -> None:
    """Record a Google connection heartbeat so Settings can show 'connected' vs 'failing'.
    Only fires for REAL calls (service is None); injected-service test calls skip it."""
    try:
        from core.db import connect, set_setting, delete_setting, now_iso
        c = connect()
        if ok:
            set_setting(c, "google_last_ok", now_iso())
            delete_setting(c, "google_last_err")
        else:
            set_setting(c, "google_last_err", (err or "failed")[:150])
        c.close()
    except Exception:
        pass


# ── Gmail (read) ──────────────────────────────────────────────────────────────
def gmail_highlights(n: int = 8, service=None) -> list:
    """Recent inbox subjects/snippets — DATA for the morning brief. []- on any failure."""
    try:
        svc = service or _service("gmail", "v1")
        listed = svc.users().messages().list(userId="me", q="in:inbox newer_than:2d",
                                             maxResults=n).execute()
        out = []
        for m in listed.get("messages", []):
            msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                            metadataHeaders=["From", "Subject", "Date"]).execute()
            hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            out.append({"from": hdrs.get("From", ""), "subject": hdrs.get("Subject", ""),
                        "date": hdrs.get("Date", ""), "snippet": msg.get("snippet", "")})
        if service is None:
            _beat(True)
        return out
    except Exception as e:
        if service is None:
            _beat(False, str(e))
        return []


def gmail_address(service=None) -> str:
    """The connected account's own email address (via gmail.readonly getProfile) — a
    signal for auto-identity. "" on any failure."""
    try:
        svc = service or _service("gmail", "v1")
        return svc.users().getProfile(userId="me").execute().get("emailAddress", "") or ""
    except Exception:
        return ""


def _message_body(payload: dict, cap: int = 2500) -> str:
    """Plain-text body from a Gmail message payload (walks multipart, prefers text/plain,
    falls back to a de-tagged text/html). Truncated — DATA for the retrieval synthesis, so
    the model can read the flight/booking DATE that a metadata snippet omits."""
    import base64
    import re as _re

    def _decode(data):
        try:
            return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "ignore")
        except Exception:
            return ""

    plain, html = "", ""
    stack = [payload or {}]
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain += _decode(data)
        elif data and mime == "text/html" and not html:
            html = _decode(data)
        stack.extend(part.get("parts", []) or [])
    if plain.strip():
        text = plain
    else:                                    # de-tag HTML — drop <style>/<script> BODIES
        html = _re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html)
        text = _re.sub(r"<[^>]+>", " ", html)
    return _re.sub(r"\s+", " ", text).strip()[:cap]


_ATTACH_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".csv")


def _message_attachments(payload: dict) -> list:
    """Readable file attachments (pdf/image/text) in a Gmail payload — the booking detail
    (passenger manifest, e-ticket) often lives here, NOT in the body. [{filename, mime,
    attachment_id}]. The bytes are fetched separately (download_attachment) only if read."""
    out = []
    stack = [payload or {}]
    while stack:
        part = stack.pop()
        fn = (part.get("filename") or "").strip()
        att_id = part.get("body", {}).get("attachmentId")
        if fn and att_id and fn.lower().endswith(_ATTACH_EXTS):
            out.append({"filename": fn, "mime": part.get("mimeType", ""), "attachment_id": att_id})
        stack.extend(part.get("parts", []) or [])
    return out


def download_attachment(msg_id: str, attachment_id: str, filename: str = "",
                        service=None) -> str | None:
    """Download ONE Gmail attachment to a temp file and return its path (None on failure).
    The file is read the SAME sandboxed way as any document — tools='Read', untrusted DATA."""
    import base64
    import os
    import tempfile
    try:
        svc = service or _service("gmail", "v1")
        att = svc.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=attachment_id).execute()
        data = base64.urlsafe_b64decode(att.get("data", "").encode())
        ext = os.path.splitext(filename or "")[1] or ".bin"
        fd, path = tempfile.mkstemp(suffix="-" + (os.path.basename(filename) or ("attach" + ext)))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    except Exception:
        return None


def gmail_search(query: str, n: int = 5, service=None, body: bool = False) -> list:
    """Full-text Gmail search (Google's own search over the whole mailbox) — DATA for the
    retrieval brain answering 'what's my flight date / booking ref'. Each hit carries the
    snippet Gmail returns; with body=True it also carries a truncated plain-text body (the
    snippet often omits the date/ref the answer needs). []- on any failure."""
    try:
        svc = service or _service("gmail", "v1")
        listed = svc.users().messages().list(userId="me", q=query or "", maxResults=n).execute()
        fmt = "full" if body else "metadata"
        kw = {} if body else {"metadataHeaders": ["From", "Subject", "Date"]}
        out = []
        for m in listed.get("messages", []):
            msg = svc.users().messages().get(userId="me", id=m["id"], format=fmt, **kw).execute()
            hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            hit = {"id": m["id"], "from": hdrs.get("From", ""), "subject": hdrs.get("Subject", ""),
                   "date": hdrs.get("Date", ""), "snippet": msg.get("snippet", "")}
            if body:
                hit["body"] = _message_body(msg.get("payload", {}))
                hit["attachments"] = _message_attachments(msg.get("payload", {}))
            out.append(hit)
        if service is None:
            _beat(True)
        return out
    except Exception as e:
        if service is None:
            _beat(False, str(e))
        return []


def create_draft(to: str, subject: str, body: str, service=None) -> dict:
    """Save a Gmail DRAFT (never sends — Kelvin reviews and sends it himself)."""
    import base64
    from email.mime.text import MIMEText
    svc = service or _service("gmail", "v1")
    mime = MIMEText(body or "")
    mime["To"] = to or ""
    mime["Subject"] = subject or ""
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    return svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()


# ── Calendar (read + event write) ─────────────────────────────────────────────
def calendar_today(day: str, service=None) -> list:
    """Today's primary-calendar events — DATA for the brief's collision detection."""
    try:
        from core.db import get_tz
        svc = service or _service("calendar", "v3")
        tz = get_tz()
        from datetime import datetime, time
        lo = datetime.combine(datetime.fromisoformat(day).date(), time.min, tz).isoformat()
        hi = datetime.combine(datetime.fromisoformat(day).date(), time.max, tz).isoformat()
        events = svc.events().list(calendarId="primary", timeMin=lo, timeMax=hi,
                                   singleEvents=True, orderBy="startTime").execute()
        out = []
        for e in events.get("items", []):
            start = e.get("start", {})
            out.append({"summary": e.get("summary", "(no title)"),
                        "start": start.get("dateTime") or start.get("date"),
                        "all_day": "date" in start and "dateTime" not in start})
        if service is None:
            _beat(True)
        return out
    except Exception as e:
        if service is None:
            _beat(False, str(e))
        return []


def calendar_range(start: str, end: str, service=None) -> list:
    """Primary-calendar events between two ISO dates (inclusive) — DATA for the Today
    page calendar. [] on any failure so a Google hiccup never breaks the page."""
    try:
        from core.db import get_tz
        svc = service or _service("calendar", "v3")
        tz = get_tz()
        from datetime import datetime, time
        lo = datetime.combine(datetime.fromisoformat(start).date(), time.min, tz).isoformat()
        hi = datetime.combine(datetime.fromisoformat(end).date(), time.max, tz).isoformat()
        events = svc.events().list(calendarId="primary", timeMin=lo, timeMax=hi,
                                   singleEvents=True, orderBy="startTime").execute()
        out = []
        for e in events.get("items", []):
            s, en = e.get("start", {}), e.get("end", {})
            start_v = s.get("dateTime") or s.get("date")
            out.append({"summary": e.get("summary", "(no title)"),
                        "start": start_v,
                        "end": en.get("dateTime") or en.get("date"),
                        "all_day": "date" in s and "dateTime" not in s,
                        "location": e.get("location", ""),
                        "date": (start_v or "")[:10]})
        if service is None:
            _beat(True)
        return out
    except Exception as e:
        if service is None:
            _beat(False, str(e))
        return []


def create_event(title: str, date: str, start_hhmm: str = None, end_hhmm: str = None,
                 attendees=None, service=None) -> dict:
    """Create a primary-calendar event (reversible — the returned link opens it to delete).
    Timed when start_hhmm is given, else all-day. `attendees` (emails) are invited and
    Google emails them the invitation (sendUpdates='all')."""
    from core.db import get_tz
    from datetime import datetime, timedelta
    svc = service or _service("calendar", "v3")
    tzname = getattr(get_tz(), "key", "UTC")
    if start_hhmm:
        # Build real datetimes so the end can't wrap before the start: a 23:30 event with no
        # explicit end must land at 00:30 the NEXT day, not the same date (Google rejects a
        # negative-length event). Parse tolerantly — a model may emit "9:00" (no zero-pad).
        def _hm(s):
            p = (s or "").split(":")
            return int(p[0]), int(p[1]) if len(p) > 1 and p[1] != "" else 0
        try:
            sh, sm = _hm(start_hhmm)
            start_dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=sh, minute=sm)
        except (ValueError, IndexError):
            return {"ok": False, "error": "bad start time"}
        if end_hhmm:
            try:
                eh, em = _hm(end_hhmm)
                end_dt = start_dt.replace(hour=eh, minute=em)
            except (ValueError, IndexError):
                end_dt = start_dt + timedelta(hours=1)
        else:
            end_dt = start_dt + timedelta(hours=1)
        if end_dt <= start_dt:                          # overnight / defaulted-past-midnight
            end_dt += timedelta(days=1)
        body = {"summary": title,
                "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tzname},
                "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tzname}}
    else:
        body = {"summary": title, "start": {"date": date}, "end": {"date": date}}
    guests = [e for e in (attendees or []) if e]
    if guests:
        body["attendees"] = [{"email": e} for e in guests]
    ev = svc.events().insert(calendarId="primary", body=body,
                             sendUpdates="all" if guests else "none").execute()
    return {"id": ev.get("id"), "link": ev.get("htmlLink")}
