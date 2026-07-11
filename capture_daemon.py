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
import sys
import tempfile
import threading
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
# Promoted to db.py so web + daemon share one accessor; names kept for call sites.
from db import get_setting as _get_setting, set_setting as _set_setting


def _stamp_heartbeat(conn):
    from db import now_iso
    _set_setting(conn, "capture_last_ran", now_iso())


# ── Telegram API ──────────────────────────────────────────────────────────────
# The Telegram HTTP client lives in telegram_api.py now.
from telegram_api import Telegram

# ── voice transcription (mlx-whisper, local) ──────────────────────────────────
# The pure transcode/transcribe primitives live in voice.py. `route_voice` and
# `_handle_voice` (which log + touch the vault + route) stay here. The constants
# are re-imported so `_handle_voice`'s settings-defaults and tests that monkeypatch
# `capture_daemon.oga_to_wav` / `.transcribe_wav` keep resolving here.
from voice import transcribe_wav, oga_to_wav, _WHISPER_MODEL, _VOICE_LANGUAGE


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
    """Human confirmation of a prefix/deterministic capture — lead with the item's own
    title, not just its destination."""
    kind = result.get("kind")
    title = (result.get("title") or "").strip()
    if kind == "task":
        tail = " · high" if result.get("priority") == "high" else ""
        return f"✓ Task: {title}{tail}" if title else "✓ " + result.get("label", "→ Tasks")
    if kind == "note":
        return f"📝 Saved: {title}" if title else "📝 " + result.get("label", "→ Notes")
    if kind == "journal":
        return "✦ Added to today's journal"
    return "✓ filed"


# ── outbound: morning digest ──────────────────────────────────────────────────
# The digest composer (build_digest + _digest_tasks/_stale_backlog) lives in
# proactive.py now — it's the deterministic FALLBACK body of the AI morning brief,
# and keeping it there kills the old capture_daemon⇄proactive import cycle.
def maybe_send_digest(conn, tg, chat_id, now=None) -> bool:
    """Send the AI morning brief once per day at/after digest_hour (default 7). On
    Sundays a fresh backlog triage is woven into the brief. Returns True if sent.
    proactive.build_digest remains the deterministic fallback inside proactive."""
    from db import today_iso, now_sg
    import proactive
    if _get_setting(conn, "brief_enabled", "1") == "0":
        return False
    now = now or now_sg()
    today = today_iso()
    h, m = _parse_hhmm(_get_setting(conn, "digest_hour", "7"), 7, 0)
    if (now.hour, now.minute) < (h, m):
        return False
    if _get_setting(conn, "digest_last_sent") == today:
        return False
    # Backlog triage is now its own scheduled surface (maybe_send_backlog_triage),
    # independent of the brief — no longer woven in on Sundays.
    text = proactive.morning_brief(conn, today, now)
    tg.send_message(chat_id, text)
    _set_setting(conn, "digest_last_sent", today)
    _log("morning brief sent")
    return True


def _parse_hhmm(val, default_h, default_m):
    """Parse an 'HH:MM' (or bare 'HH') setting into (hour, minute)."""
    try:
        s = str(val)
        if ":" in s:
            h, m = s.split(":", 1)
            return int(h), int(m)
        return int(s), 0
    except (TypeError, ValueError):
        return default_h, default_m


def maybe_send_reflection(conn, tg, chat_id, now=None) -> bool:
    """Send the evening journal reflection once per day at/after reflection_hour
    (default 21:30). Returns True if sent."""
    from db import today_iso, now_sg
    import proactive
    if _get_setting(conn, "reflection_enabled", "1") == "0":
        return False
    now = now or now_sg()
    today = today_iso()
    h, m = _parse_hhmm(_get_setting(conn, "reflection_hour", "21:30"), 21, 30)
    if (now.hour, now.minute) < (h, m):
        return False
    if _get_setting(conn, "reflection_last_sent") == today:
        return False
    text = proactive.evening_reflection(conn, today, now)
    tg.send_message(chat_id, text)
    _set_setting(conn, "reflection_last_sent", today)
    _log("evening reflection sent")
    return True


_WEEKDAY_NUM = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def maybe_send_backlog_triage(conn, tg, chat_id, now=None) -> bool:
    """Send the Do/Defer/Delete backlog triage once on its scheduled day at/after its
    time (settings triage_day + triage_time; default Sunday 09:00). Independent of the
    morning brief. On-demand triage ("triage my backlog") still works separately."""
    from db import today_iso, now_sg
    import proactive
    if _get_setting(conn, "triage_enabled", "1") == "0":
        return False
    now = now or now_sg()
    today = today_iso()
    day = (_get_setting(conn, "triage_day", "sun") or "sun").lower()
    if day != "daily" and now.weekday() != _WEEKDAY_NUM.get(day, 6):
        return False
    h, m = _parse_hhmm(_get_setting(conn, "triage_time", "09:00"), 9, 0)
    if (now.hour, now.minute) < (h, m):
        return False
    if _get_setting(conn, "triage_scheduled_sent") == today:
        return False
    try:
        text = proactive.backlog_triage(conn)
    except Exception as e:
        _log(f"scheduled backlog triage failed: {e}")
        return False
    tg.send_message(chat_id, text)
    _set_setting(conn, "triage_scheduled_sent", today)
    _log("backlog triage sent")
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

            # Outbound: morning brief + evening reflection (checked each poll cycle).
            if allowed:
                try:
                    maybe_send_digest(conn, tg, allowed)
                except Exception as e:
                    _log(f"digest failed: {e}")
                try:
                    maybe_send_reflection(conn, tg, allowed)
                except Exception as e:
                    _log(f"reflection failed: {e}")
                try:
                    maybe_send_backlog_triage(conn, tg, allowed)
                except Exception as e:
                    _log(f"triage schedule failed: {e}")

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
        elif "photo" in msg or _is_image_document(msg):
            if _handle_photo(conn, tg, msg, chat_id):
                sweep_due_at = time.time() + SWEEP_DELAY_S
        elif "voice" in msg or "audio" in msg:
            if _handle_voice(conn, tg, msg, chat_id):
                sweep_due_at = time.time() + SWEEP_DELAY_S
        else:
            tg.send_message(chat_id, "I can handle text, links, photos and voice notes for now.")
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

    # Fast path 1: prefix shortcuts → deterministic capture, no Claude. (Prefix wins over
    # URL detection, so `n: <url>` files a plain note exactly as before.)
    if low.startswith(("t:", "n:", "i:", "j:")):
        from capture import route_capture
        result = route_capture(conn, text, source="telegram")
        tg.send_message(chat_id, format_reply(result))
        return False

    # Fast path 1b: a bare link → instant ack, then edit into the enriched reply.
    if _looks_like_url(text):
        _handle_link(conn, tg, chat_id, text)
        return False

    # Fast path 2: unambiguous list questions → instant deterministic answer, no Claude.
    from queries import is_query, answer_query
    if is_query(text):
        ans = answer_query(conn, text)
        if ans is not None:
            tg.send_message(chat_id, ans)
            return False

    # Fast path 3: an explicit backlog-triage request → run backlog intelligence and
    # reply, skipping the router (its only job would be to decide to run this anyway).
    import proactive
    if proactive.is_backlog_triage_request(text):
        try:
            tg.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        tg.send_message(chat_id, proactive.backlog_triage(conn))
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


def format_link_reply(note: dict, summary: str = "") -> str:
    """Rich link confirmation: enriched title, one-line why-it-matters, tags (minus the
    bare 'link' plumbing tag), and the URL LAST on its own line so Telegram renders a
    preview."""
    import capture
    title = (note.get("title") or "").strip() or "link"
    lines = [f"📎 {title}"]
    if summary:
        lines.append(f"   {summary.strip()}")
    tags = [t for t in (note.get("tags") or []) if t != "link"]
    if tags:
        lines.append("   " + " ".join("#" + t for t in tags))
    url = note.get("url") or capture.first_url(note.get("body") or "")
    if url:
        lines.append(f"   {url}")
    return "\n".join(lines)


def _handle_link(conn, tg, chat_id, text) -> None:
    """A bare link: acknowledge INSTANTLY, then enrich it in the background and EDIT the
    ack into the rich reply. Re-shares report 'already saved'; enrichment off/failure
    degrades to a plain 'Saved: <title>' — the ack is never left dangling. The save
    itself runs on this (main) thread; only the slow claude enrichment is offloaded, and
    it touches vault files + Telegram only (no DB), matching capture.schedule_enrichment."""
    import capture

    sent = tg.send_message(chat_id, "📎 Saved — reading it…")
    message_id = ((sent or {}).get("result") or {}).get("message_id")

    def _edit(msg):
        try:
            if message_id:
                tg.edit_message_text(chat_id, message_id, msg)
            else:
                tg.send_message(chat_id, msg)
        except Exception:
            pass

    result = capture.route_capture(conn, text, source="telegram", enrich="off")
    slug = result.get("slug")
    title = result.get("title") or "link"
    if result.get("deduped"):
        _edit(f"📎 Already saved: {title}")
        return
    if not slug or not capture._enrich_enabled():
        _edit(f"📎 Saved: {title}")
        return

    def _run():
        try:
            note, summary = capture.enrich_link(slug)
        except Exception:
            note, summary = None, ""
        _edit(format_link_reply(note, summary) if note else f"📎 Saved: {title}")

    threading.Thread(target=_run, name="tg-link", daemon=True).start()


def _is_image_document(msg) -> bool:
    """True for a document whose MIME type is an image (photos sent 'as file')."""
    doc = msg.get("document") or {}
    return str(doc.get("mime_type") or "").startswith("image/")


def _handle_photo(conn, tg, msg, chat_id) -> bool:
    """Download the highest-resolution copy of an inbound image to vault/.media/, then
    route it (with any caption) through the SAME agentic router as text and voice. The
    Claude CLI views the file with its Read tool. Returns True if the router fell back
    (so the caller schedules a sweep)."""
    import vault_store
    from db import now_sg

    photo = msg.get("photo")
    if photo:                                # Telegram sends sizes ascending → last = largest
        biggest = photo[-1]
    else:
        biggest = msg.get("document") or {}
    file_id = biggest.get("file_id")
    if not file_id:
        return False
    uniq = biggest.get("file_unique_id") or "img"
    stamp = now_sg().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(vault_store.media_dir(), f"{stamp}-{uniq}.jpg")

    try:
        tg.send_chat_action(chat_id, "typing")
    except Exception:
        pass
    try:
        fpath = tg.get_file_path(file_id)
        tg.download_file(fpath, dest)
    except Exception as e:
        _log(f"photo download failed: {e}")
        tg.send_message(chat_id, "⚠️ Couldn't download that image — try again?")
        return False

    caption = (msg.get("caption") or "").strip()
    import router
    out = router.route(conn, caption, source="telegram", image_path=dest)
    tg.send_message(chat_id, out["reply"], reply_markup=out.get("keyboard"))
    if out.get("fell_back"):
        _log(f"router fell back on photo: {caption[:80]}")
    return bool(out.get("fell_back"))


_HELP_TEXT = (
    "👋 I'm your Life OS assistant.\n\n"
    "Send me anything — tasks, thoughts, journal entries, instructions "
    "('mark X done', 'push Y to next week'), questions, voice notes, or a PHOTO. "
    "I read your open tasks, goals and journal, work out what you mean, and act on it.\n\n"
    "Try:\n"
    "• reply to the sponsor email tomorrow\n"
    "• mark the CPF video done\n"
    "• push the invoice to Friday\n"
    "• how many videos have I done this week?\n"
    "• 📷 photo of a bill + \"split this between me, WL and Jim — I paid\"\n\n"
    "I remember the last few messages, so follow-ups work: reply 'yes' to an offer, "
    "or 'change it to Friday'. Undo lives on a button under anything I change. "
    "Power-user shortcuts still work: t: (task), n: (note), i: (idea), j: (journal), "
    "or paste a link.")


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
    try:                                              # always clean up the scratch dir
        try:
            fpath = tg.get_file_path(file_id)
            tg.download_file(fpath, oga)
            oga_to_wav(oga, wav)
            lang = _get_setting(conn, "voice_language", _VOICE_LANGUAGE) or _VOICE_LANGUAGE
            model = _get_setting(conn, "whisper_model", _WHISPER_MODEL) or _WHISPER_MODEL
            text = transcribe_wav(wav, language=lang, model=model)
        except Exception as e:
            _log(f"voice transcription failed: {e}")
            tg.send_message(chat_id, "⚠️ Could not transcribe that voice note.")
            return False
        if not text:
            tg.send_message(chat_id, "🔇 Heard silence — nothing to file.")
            return False
        audio_ptr = _preserve_audio(oga)             # audio is never lost
        snippet = text if len(text) <= 80 else text[:77] + "…"
        tg.send_message(chat_id, f"🎙 \"{snippet}\"")
        import router
        try:
            tg.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        out = router.route(conn, text, source="voice", audio_path=audio_ptr)
        tg.send_message(chat_id, out["reply"], reply_markup=out.get("keyboard"))
        return bool(out.get("fell_back"))
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _preserve_audio(oga_path: str) -> str | None:
    """Keep the original recording in vault/.audio/ so a voice note is never lost,
    even when the router acts on it (task/journal) rather than filing a note. Returns
    the vault-relative pointer (for the note's `audio:` frontmatter) or None on failure."""
    try:
        if not (oga_path and os.path.exists(oga_path)):
            return None
        import shutil
        import vault_store
        from db import now_sg
        name = "voice-" + now_sg().strftime("%Y%m%d-%H%M%S") + ".oga"
        shutil.copyfile(oga_path, os.path.join(vault_store.audio_dir(), name))
        return "vault/.audio/" + name
    except OSError as e:
        _log(f"could not store audio: {e}")
        return None


if __name__ == "__main__":
    sys.exit(main())
