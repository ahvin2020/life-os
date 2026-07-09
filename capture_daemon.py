#!/usr/bin/env python3
"""Telegram capture daemon — Phase 2, MAC-FIRST.

Long-polls Telegram getUpdates and files each message through the SAME router the
web composer uses (capture.route_capture), so the phone bot and the web twin file
things identically. Voice notes are transcribed locally with mlx-whisper (base) —
no audio ever leaves Kelvin's hardware — and the original recording is kept in
vault/.audio/ with a pointer in the note frontmatter.

Also drives the outbound side: a debounced Claude triage run after ambiguous
captures, a morning digest (+ Sunday stale-backlog nudge), and a heartbeat that
feeds the dashboard's health dots.

Security: only messages from TELEGRAM_ALLOWED_USER_ID are processed; everything
else is ignored. The daemon polls outward, so no inbound port is exposed.

Env (from repo-root .env via envload):
  TELEGRAM_BOT_TOKEN        BotFather token (required to run)
  TELEGRAM_ALLOWED_USER_ID  numeric Telegram user id allowed to capture
  LIFEOS_DB_PATH            app.db location (optional override)

Run:  .venv/bin/python capture_daemon.py
The daemon is intended to run under launchd (see deploy/). Leave it STOPPED in
dev; launchd/Kelvin starts it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

# Heavy third-party imports (requests, mlx_whisper) are deferred into the
# functions that need them so `import capture_daemon` stays cheap for the tests.

_ROOT = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_ROOT, "data", "capture_daemon.log")

# Debounce window: a burst of ambiguous captures collapses into one triage run.
# Kept tight so the ack → classify → outcome round-trip feels snappy for Kelvin
# (unprefixed text/voice is the norm; triage is the PRIMARY router, not a fallback).
TRIAGE_DEBOUNCE_S = 75
# Heartbeat/staleness is read by web_core.health_status; keep these in sync.
POLL_TIMEOUT_S = 50


# ── logging ───────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} [capture_daemon] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── settings helpers (offset persistence, heartbeats, digest bookkeeping) ─────
def _get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(conn, key, value):
    with conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _stamp_heartbeat(conn):
    from db import now_iso
    _set_setting(conn, "capture_last_ran", now_iso())


# ── Telegram API ──────────────────────────────────────────────────────────────
class Telegram:
    """Thin wrapper over the Telegram Bot HTTP API (only the calls we use)."""

    def __init__(self, token: str):
        self.api = f"https://api.telegram.org/bot{token}"

    def _call(self, method, **params):
        import requests
        r = requests.get(f"{self.api}/{method}", params=params, timeout=POLL_TIMEOUT_S + 15)
        return r.json()

    def get_updates(self, offset, timeout=POLL_TIMEOUT_S):
        return self._call("getUpdates", offset=offset, timeout=timeout).get("result", [])

    def send_message(self, chat_id, text):
        return self._call("sendMessage", chat_id=chat_id, text=text)

    def get_me(self):
        return self._call("getMe")

    def get_file_path(self, file_id):
        j = self._call("getFile", file_id=file_id)
        return (j.get("result") or {}).get("file_path")

    def download_file(self, file_path, dest):
        import requests
        # NOTE: file downloads use /file/bot<token>/<path>, not the method API.
        url = self.api.replace("/bot", "/file/bot", 1) + "/" + file_path
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        return dest


# ── voice transcription (mlx-whisper, local) ──────────────────────────────────
_WHISPER_MODEL = "mlx-community/whisper-base-mlx"


def transcribe_wav(wav_path: str) -> str:
    """Transcribe a wav with mlx-whisper (base). Weights auto-download once."""
    import mlx_whisper
    out = mlx_whisper.transcribe(wav_path, path_or_hf_repo=_WHISPER_MODEL)
    return (out.get("text") or "").strip()


def oga_to_wav(oga_path: str, wav_path: str) -> str:
    """ffmpeg: .oga → 16 kHz mono wav (what whisper wants)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", oga_path, "-ar", "16000", "-ac", "1", wav_path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return wav_path


_SPOKEN_TASK = ("task ", "todo ", "to-do ", "to do ")
_SPOKEN_JOURNAL = ("journal ", "diary ", "dear diary ")
_SPOKEN_IDEA = ("idea ", "video idea ")


def route_voice(conn, text: str, oga_path: str | None):
    """File a transcribed voice note. A leading spoken keyword maps to task/journal;
    otherwise it lands as an #unsorted note (triage will refine). The original .oga
    is kept in vault/.audio/<slug>.oga and pointed at from the note frontmatter."""
    from capture import route_capture, _strip_prefix
    import vault_store

    low = text.lower().lstrip()
    if low.startswith(_SPOKEN_TASK):
        stripped = text.split(None, 1)[1] if " " in text.strip() else text
        return route_capture(conn, "t: " + stripped, source="voice", forced="task")
    if low.startswith(_SPOKEN_JOURNAL):
        stripped = text.split(None, 1)[1] if " " in text.strip() else text
        return route_capture(conn, "j: " + stripped, source="voice", forced="journal")

    tags = ["unsorted"]
    if low.startswith(_SPOKEN_IDEA):
        tags = ["idea", "unsorted"]
    title = (text.strip().splitlines()[0] if text.strip() else "Voice note")[:60]
    note = vault_store.create_note(title=title, body=text, tags=tags)
    # Persist the original audio next to the note and record the pointer.
    if oga_path and os.path.exists(oga_path):
        import shutil
        dest = os.path.join(vault_store.audio_dir(), note["slug"] + ".oga")
        try:
            shutil.copyfile(oga_path, dest)
            note = vault_store.write_note(
                note["slug"], note["title"], note["tags"], note["body"],
                note["pinned"], note["created"], audio=f"vault/.audio/{note['slug']}.oga")
        except OSError as e:
            _log(f"could not store audio: {e}")
    tag_str = " ".join("#" + t for t in tags)
    return {"kind": "note", "slug": note["slug"], "label": "Notes · " + tag_str,
            "tags": tags}


# ── reply formatting ──────────────────────────────────────────────────────────
def format_reply(result: dict) -> str:
    """Human confirmation of where a capture was filed (direct/prefix shortcut)."""
    kind = result.get("kind")
    if kind == "task":
        return "✓ " + result.get("label", "→ Tasks")
    if kind == "note":
        return "✓ " + result.get("label", "→ Notes")
    if kind == "journal":
        return "✓ → today's Journal"
    return "✓ filed"


def filing_reply(result: dict) -> str:
    """What to reply the instant an item is captured. Ambiguous items (the norm —
    plain text / voice) get an ack while triage classifies; prefixed shortcuts get
    their destination straight away."""
    if _is_ambiguous(result):
        return "📥 saved — filing…"
    return format_reply(result)


# ── outbound: morning digest ──────────────────────────────────────────────────
def _digest_tasks(conn, today):
    """Open tasks that matter today: due today, overdue, or ☀ planned."""
    rows = conn.execute(
        """SELECT title, due_date, planned_on, priority, category FROM tasks
             WHERE parent_id IS NULL AND archived_at IS NULL AND deleted_at IS NULL AND done = 0 AND (
               due_date = ? OR (due_date IS NOT NULL AND due_date < ?) OR planned_on = ?)
           ORDER BY (due_date IS NULL), due_date, sort_order""",
        (today, today, today)).fetchall()
    return rows


def _stale_backlog(conn, today, days=30):
    """Backlog tasks untouched for `days`+ (the Sunday do-or-delete nudge)."""
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).date().isoformat()
    return conn.execute(
        "SELECT title, updated FROM tasks WHERE parent_id IS NULL AND archived_at IS NULL "
        "AND deleted_at IS NULL AND done = 0 AND substr(updated,1,10) < ? ORDER BY updated",
        (cutoff,)).fetchall()


def build_digest(conn, day=None, now=None) -> str:
    """Compose the morning-digest text: today's tasks, goal progress, journal nudge,
    and (Sundays) stale backlog + set-goals reminder. Pure — unit-tested directly."""
    from db import today_iso, now_sg
    from routes_goals import goal_progress
    day = day or today_iso()
    now = now or now_sg()

    lines = [f"☀ Good morning — {now.strftime('%A %-d %b')}"]

    tasks = _digest_tasks(conn, day)
    lines.append("")
    if tasks:
        lines.append(f"📋 Today ({len(tasks)}):")
        for t in tasks:
            mark = ""
            if t["due_date"] and t["due_date"] < day:
                mark = " · overdue"
            elif t["due_date"] == day:
                mark = " · due today"
            elif t["planned_on"] == day:
                mark = " · ☀ planned"
            if t["priority"] == "high":
                mark += " · high"
            lines.append(f"  • {t['title']}{mark}")
    else:
        lines.append("📋 Nothing due or planned today — a clear board.")

    goals = conn.execute(
        "SELECT * FROM goals WHERE archived_at IS NULL ORDER BY period, created").fetchall()
    if goals:
        lines.append("")
        lines.append("🎯 Goals:")
        for g in goals:
            p = goal_progress(conn, g)
            if g["kind"] == "number":
                lines.append(f"  • {g['title']}: {int(p.get('current', 0))}/{int(p.get('target', 0))}")
            else:
                lines.append(f"  • {g['title']}: {p.get('done', 0)}/{p.get('total', 0)}")

    # Journal nudge if yesterday had no entry.
    import vault_store
    yesterday = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
    if not vault_store.read_journal(yesterday):
        lines.append("")
        lines.append("✦ No journal entry yesterday — how did the day go?")

    # Sunday extras: stale backlog + set next week's goals.
    if now.weekday() == 6:
        stale = _stale_backlog(conn, day)
        lines.append("")
        if stale:
            lines.append(f"🧹 Stale backlog — do or delete ({len(stale)}):")
            for s in stale:
                lines.append(f"  • {s['title']}")
        lines.append("🗓 Weekly review: set next week's goals.")

    return "\n".join(lines)


def maybe_send_digest(conn, tg, chat_id, now=None) -> bool:
    """Send the digest once per day at/after digest_hour. Returns True if sent."""
    from db import today_iso, now_sg
    now = now or now_sg()
    today = today_iso()
    try:
        hour = int(_get_setting(conn, "digest_hour", "8"))
    except (TypeError, ValueError):
        hour = 8
    if now.hour < hour:
        return False
    if _get_setting(conn, "digest_last_sent") == today:
        return False
    text = build_digest(conn, today, now)
    tg.send_message(chat_id, text)
    _set_setting(conn, "digest_last_sent", today)
    _log("morning digest sent")
    return True


# ── triage (debounced) ────────────────────────────────────────────────────────
def run_triage_now(conn, tg=None, chat_id=None):
    """Invoke the triage runner and report anything it reclassified back to Kelvin."""
    import triage.run_triage as rt
    applied = rt.run(conn)
    if applied and tg and chat_id:
        for a in applied[:5]:
            tg.send_message(chat_id, "✓ " + a)
    return applied


# ── main loop ─────────────────────────────────────────────────────────────────
def main() -> int:
    from envload import load_env
    load_env()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        _log("TELEGRAM_BOT_TOKEN not set — nothing to do. Exiting.")
        return 0
    allowed = str(os.environ.get("TELEGRAM_ALLOWED_USER_ID") or "")

    from db import connect
    conn = connect()
    tg = Telegram(token)

    offset = int(_get_setting(conn, "telegram_offset", "0") or "0")
    triage_due_at = None       # epoch seconds; set when an ambiguous item arrives
    _log(f"starting long-poll loop (offset={offset})")

    while True:
        try:
            updates = tg.get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                _set_setting(conn, "telegram_offset", offset)
                triage_due_at = _process_update(conn, tg, allowed, upd, triage_due_at)

            _stamp_heartbeat(conn)

            # Fire the debounced triage run once the quiet window has elapsed.
            if triage_due_at is not None and time.time() >= triage_due_at:
                triage_due_at = None
                chat = allowed or None
                try:
                    run_triage_now(conn, tg, chat)
                except Exception as e:
                    _log(f"triage run failed: {e}")

            # Outbound: morning digest (checked each poll cycle ~every {POLL_TIMEOUT_S}s).
            if allowed:
                try:
                    maybe_send_digest(conn, tg, allowed)
                except Exception as e:
                    _log(f"digest failed: {e}")

        except Exception as e:                       # never crash the loop
            _log(f"poll error: {e}")
            time.sleep(5)                            # network backoff
    return 0


def _process_update(conn, tg, allowed, upd, triage_due_at):
    """Handle one Telegram update. Returns the (possibly updated) triage timer."""
    msg = upd.get("message") or upd.get("edited_message") or {}
    uid = str((msg.get("from") or {}).get("id", ""))
    chat_id = (msg.get("chat") or {}).get("id")
    if allowed and uid != allowed:
        _log(f"ignoring message from unauthorised user {uid}")
        return triage_due_at

    try:
        if "text" in msg and msg["text"].lstrip().startswith("/"):
            tg.send_message(chat_id, _command_reply(msg["text"]))
        elif "text" in msg and _is_query(msg["text"]):
            # A question about his data — answer, file nothing. Deterministic handlers
            # first (instant/free); anything list-shaped but unmatched falls back to a
            # read-only free-form Claude answer.
            from queries import answer_query, answer_freeform
            ans = answer_query(conn, msg["text"])
            if ans is not None:
                tg.send_message(chat_id, ans)
            else:
                tg.send_message(chat_id, "🤔 thinking…")
                _log(f"free-form Q&A: {msg['text'][:80]}")
                reply = answer_freeform(conn, msg["text"])
                tg.send_message(chat_id, reply or "couldn't get an answer — try again or rephrase")
        elif "text" in msg:
            from capture import route_capture
            result = route_capture(conn, msg["text"], source="telegram")
            tg.send_message(chat_id, filing_reply(result))
            if _is_ambiguous(result):
                triage_due_at = time.time() + TRIAGE_DEBOUNCE_S
        elif "voice" in msg or "audio" in msg:
            result = _handle_voice(conn, tg, msg, chat_id)
            if result and _is_ambiguous(result):
                triage_due_at = time.time() + TRIAGE_DEBOUNCE_S
        else:
            tg.send_message(chat_id, "I can file text, links and voice notes for now.")
    except Exception as e:
        _log(f"handling update {upd.get('update_id')} failed: {e}")
        if chat_id:
            try:
                tg.send_message(chat_id, "⚠️ Sorry — that one failed to file. It's logged.")
            except Exception:
                pass
    return triage_due_at


_HELP_TEXT = (
    "👋 I'm your Life OS capture bot.\n\n"
    "Send me anything to capture it — a plain text thought or a voice note — and "
    "I'll file it. I reply 📥 saved, then sort it into a task, note, or journal "
    "entry within a minute or two.\n\n"
    "Or ask me anything about your tasks, notes, journal and goals, e.g.:\n"
    "• what are my todos\n"
    "• any overdue?\n"
    "• goals\n"
    "• how was my week?\n"
    "• find <term>\n\n"
    "Power-user shortcuts (optional): start with t: (task), n: (note), i: (idea), "
    "j: (journal), or paste a link.")


def _command_reply(text: str) -> str:
    """Reply to bot slash-commands (/start, /help) without filing them."""
    return _HELP_TEXT


def _is_query(text: str) -> bool:
    from queries import is_query
    return is_query(text)


def _is_ambiguous(result: dict) -> bool:
    return result.get("kind") == "note" and "unsorted" in (result.get("tags") or [])


def _handle_voice(conn, tg, msg, chat_id):
    voice = msg.get("voice") or msg.get("audio") or {}
    file_id = voice.get("file_id")
    if not file_id:
        return None
    tmpdir = tempfile.mkdtemp(prefix="lifeos-voice-")
    oga = os.path.join(tmpdir, "in.oga")
    wav = os.path.join(tmpdir, "in.wav")
    try:
        fpath = tg.get_file_path(file_id)
        tg.download_file(fpath, oga)
        oga_to_wav(oga, wav)
        text = transcribe_wav(wav)
    except Exception as e:
        _log(f"voice transcription failed: {e}")
        tg.send_message(chat_id, "⚠️ Could not transcribe that voice note.")
        return None
    if not text:
        tg.send_message(chat_id, "🔇 Heard silence — nothing to file.")
        return None
    result = route_voice(conn, text, oga)
    snippet = text if len(text) <= 80 else text[:77] + "…"
    tg.send_message(chat_id, f"🎙 \"{snippet}\"\n{filing_reply(result)}")
    return result


if __name__ == "__main__":
    sys.exit(main())
