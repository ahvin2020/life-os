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

# Routing is now INLINE (router.py calls `claude -p` per message), so the old
# debounced triage is gone. run_triage.py survives only as the --sweep safety net
# for #unsorted leftovers: a sweep is scheduled shortly after a fallback capture,
# and once daily. This short delay lets a transient claude hiccup settle first.
SWEEP_DELAY_S = 45
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

    def send_message(self, chat_id, text, reply_markup=None):
        import json as _json
        params = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = _json.dumps(reply_markup)
        return self._call("sendMessage", **params)

    def send_chat_action(self, chat_id, action="typing"):
        return self._call("sendChatAction", chat_id=chat_id, action=action)

    def answer_callback_query(self, callback_id, text=None):
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        return self._call("answerCallbackQuery", **params)

    def edit_reply_markup(self, chat_id, message_id):
        """Drop the inline keyboard from a message (after its Undo is spent)."""
        return self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id)

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
    sweep_due_at = None        # epoch seconds; set after a fallback capture
    _log(f"starting long-poll loop (offset={offset})")

    while True:
        try:
            updates = tg.get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                _set_setting(conn, "telegram_offset", offset)
                if "callback_query" in upd:
                    _process_callback(conn, tg, allowed, upd)
                else:
                    sweep_due_at = _process_update(conn, tg, allowed, upd, sweep_due_at)

            _stamp_heartbeat(conn)

            # Sweep the #unsorted inbox: shortly after a fallback capture, and once
            # daily as a floor. run_triage.py is the ONLY thing that reads it now.
            if sweep_due_at is not None and time.time() >= sweep_due_at:
                sweep_due_at = None
                try:
                    run_triage_now(conn, tg, allowed or None)
                except Exception as e:
                    _log(f"sweep failed: {e}")
            elif allowed:
                try:
                    maybe_daily_sweep(conn, tg, allowed)
                except Exception as e:
                    _log(f"daily sweep failed: {e}")

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


def maybe_daily_sweep(conn, tg, chat_id) -> bool:
    """Run the #unsorted sweep at most once per day (a floor under the on-fallback
    sweep, so leftovers never rot). Returns True if it ran."""
    from db import today_iso
    today = today_iso()
    if _get_setting(conn, "sweep_last_day") == today:
        return False
    _set_setting(conn, "sweep_last_day", today)
    if capture_has_unsorted():
        run_triage_now(conn, tg, chat_id)
    return True


def capture_has_unsorted() -> bool:
    from capture import list_unsorted_notes
    return bool(list_unsorted_notes())


def _process_callback(conn, tg, allowed, upd):
    """Handle an inline-keyboard tap (Undo). Applies the inverse op, acknowledges the
    tap, and strips the spent button from the original message."""
    cq = upd.get("callback_query") or {}
    uid = str((cq.get("from") or {}).get("id", ""))
    if allowed and uid != allowed:
        return
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    try:
        import router
        result = router.handle_callback(conn, data)
    except Exception as e:
        _log(f"callback {data!r} failed: {e}")
        result = "Couldn't undo that."
    try:
        tg.answer_callback_query(cq.get("id"), result)
        if chat_id and message_id:
            tg.edit_reply_markup(chat_id, message_id)
    except Exception as e:
        _log(f"callback ack failed: {e}")


def _process_update(conn, tg, allowed, upd, sweep_due_at):
    """Handle one Telegram update. Returns the (possibly updated) sweep timer —
    a fallback capture schedules a sweep so #unsorted leftovers never rot."""
    msg = upd.get("message") or upd.get("edited_message") or {}
    uid = str((msg.get("from") or {}).get("id", ""))
    chat_id = (msg.get("chat") or {}).get("id")
    if allowed and uid != allowed:
        _log(f"ignoring message from unauthorised user {uid}")
        return sweep_due_at

    try:
        if "text" in msg and msg["text"].lstrip().startswith("/"):
            tg.send_message(chat_id, _command_reply(msg["text"]))
        elif "text" in msg:
            if _handle_text(conn, tg, chat_id, msg["text"]):
                sweep_due_at = time.time() + SWEEP_DELAY_S
        elif "voice" in msg or "audio" in msg:
            if _handle_voice(conn, tg, msg, chat_id):
                sweep_due_at = time.time() + SWEEP_DELAY_S
        else:
            tg.send_message(chat_id, "I can handle text, links and voice notes for now.")
    except Exception as e:
        _log(f"handling update {upd.get('update_id')} failed: {e}")
        if chat_id:
            try:
                tg.send_message(chat_id, "⚠️ Sorry — that one failed. It's logged and safe.")
            except Exception:
                pass
    return sweep_due_at


def _handle_text(conn, tg, chat_id, text) -> bool:
    """Route one text message. Fast paths (prefix/URL, instant deterministic query)
    skip Claude; everything else goes through the agentic router. Returns True if a
    router fallback fired (so the caller schedules a sweep)."""
    low = text.strip().lower()

    # Fast path 1: prefix shortcuts + bare URL → deterministic capture, no Claude.
    if low.startswith(("t:", "n:", "i:", "j:")) or _looks_like_url(text):
        from capture import route_capture
        result = route_capture(conn, text, source="telegram")
        tg.send_message(chat_id, format_reply(result))
        return False

    # Fast path 2: unambiguous list questions → instant deterministic answer, no Claude.
    from queries import is_query, answer_query
    if is_query(text):
        ans = answer_query(conn, text)
        if ans is not None:
            tg.send_message(chat_id, ans)
            return False

    # The agentic router — the ONE Claude entry point. Acts on instructions,
    # answers open questions, files captures, all in a single call.
    import router
    try:
        tg.send_chat_action(chat_id, "typing")
    except Exception:
        pass
    out = router.route(conn, text, source="telegram")
    tg.send_message(chat_id, out["reply"], reply_markup=out.get("keyboard"))
    if out.get("fell_back"):
        _log(f"router fell back to #unsorted: {text[:80]}")
    return bool(out.get("fell_back"))


def _looks_like_url(text: str) -> bool:
    from capture import _looks_like_url as _u
    return _u(text)


_HELP_TEXT = (
    "👋 I'm your Life OS assistant.\n\n"
    "Send me anything — tasks, thoughts, journal entries, instructions "
    "('mark X done', 'push Y to next week') or questions. I read your open tasks, "
    "goals and journal, work out what you mean, and act on it.\n\n"
    "Try:\n"
    "• reply to the sponsor email tomorrow\n"
    "• mark the CPF video done\n"
    "• push the invoice to Friday\n"
    "• how many videos have I done this week?\n\n"
    "Undo lives on a button under anything I change. Power-user shortcuts still "
    "work: t: (task), n: (note), i: (idea), j: (journal), or paste a link.")


def _command_reply(text: str) -> str:
    """Reply to bot slash-commands (/start, /help) without filing them."""
    return _HELP_TEXT


def _handle_voice(conn, tg, msg, chat_id) -> bool:
    """Transcribe a voice note locally, preserve the original audio, then route the
    TEXT through the agentic router exactly like a typed message. Returns True if the
    router fell back (so the caller schedules a sweep)."""
    voice = msg.get("voice") or msg.get("audio") or {}
    file_id = voice.get("file_id")
    if not file_id:
        return False
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
        return False
    if not text:
        tg.send_message(chat_id, "🔇 Heard silence — nothing to file.")
        return False
    _preserve_audio(oga)                              # audio is never lost
    snippet = text if len(text) <= 80 else text[:77] + "…"
    tg.send_message(chat_id, f"🎙 \"{snippet}\"")
    import router
    try:
        tg.send_chat_action(chat_id, "typing")
    except Exception:
        pass
    out = router.route(conn, text, source="voice")
    tg.send_message(chat_id, out["reply"], reply_markup=out.get("keyboard"))
    return bool(out.get("fell_back"))


def _preserve_audio(oga_path: str) -> None:
    """Keep the original recording in vault/.audio/ so a voice note is never lost,
    even when the router acts on it (task/journal) rather than filing a note."""
    try:
        if not (oga_path and os.path.exists(oga_path)):
            return
        import shutil
        import vault_store
        from db import now_sg
        dest = os.path.join(vault_store.audio_dir(),
                            "voice-" + now_sg().strftime("%Y%m%d-%H%M%S") + ".oga")
        shutil.copyfile(oga_path, dest)
    except OSError as e:
        _log(f"could not store audio: {e}")


if __name__ == "__main__":
    sys.exit(main())
