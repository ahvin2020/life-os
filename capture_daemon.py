#!/usr/bin/env python3
"""Telegram capture daemon — Phase 2, MAC-FIRST.

Long-polls Telegram getUpdates and files each message through the SAME router the
web composer uses (capture.route_capture), so the phone bot and the web twin file
things identically. Voice notes are transcribed locally with mlx-whisper (base) —
no audio ever leaves Sam's hardware — and the original recording is kept in
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
dev; launchd/Sam starts it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

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


# ── last-ditch content preservation (safety rail when the normal path crashes) ─
_RAW_LOG_PATH = os.path.join(_ROOT, "data", "capture_raw.log")


def _safety_log_raw(msg) -> None:
    """Preserve inbound content to capture_raw.log when handling crashed BEFORE its
    own logging (e.g. the crash IS a router import failure, so router.log_raw is
    unreachable). Self-contained — depends on nothing that might be mid-sync — and
    never raises. This is what makes "It's logged and safe" actually true."""
    try:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            if "voice" in msg or "audio" in msg:
                text = "[voice note — transcription lost to crash]"
            elif "photo" in msg or "document" in msg:
                text = "[photo/file — lost to crash]"
            else:
                return
        os.makedirs(os.path.dirname(_RAW_LOG_PATH), exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(_RAW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts}\ttelegram-crash\t{text}\n")
    except Exception:
        pass


# ── settings helpers (offset persistence, heartbeats, digest bookkeeping) ─────
# Promoted to db.py so web + daemon share one accessor; names kept for call sites.
from core.db import get_setting as _get_setting, set_setting as _set_setting


def _stamp_heartbeat(conn):
    from core.db import now_iso
    _set_setting(conn, "capture_last_ran", now_iso())


# ── Telegram API ──────────────────────────────────────────────────────────────
# The Telegram HTTP client lives in telegram_api.py now.
from ai.telegram_api import Telegram

# ── voice transcription (mlx-whisper, local) ──────────────────────────────────
# The pure transcode/transcribe primitives live in voice.py. `_handle_voice` (below)
# is the live path: it transcribes, preserves the .oga via `_preserve_audio`, then
# routes the text through `router.route`. The constants are re-imported so tests that
# monkeypatch `capture_daemon.oga_to_wav` / `.transcribe_wav` keep resolving here.
from ai.voice import transcribe_wav, oga_to_wav, _WHISPER_MODEL, _VOICE_LANGUAGE

# ── outbound scheduler ────────────────────────────────────────────────────────
# The time-of-day cadences (morning brief, reflection, backlog triage, weekly review,
# monthly retro, doc scan, timed reminders, daily sweep, Claude-down nudge) live in
# scheduler.py now. Re-exported here so the main loop below and the tests' existing
# `capture_daemon.maybe_*` call sites resolve unchanged (same pattern as routes/
# re-exporting the domain helpers). scheduler.schedule_doc_scan / scan_documents_now
# are re-exported too so _handle_photo can still fire an on-capture scan.
from scheduler import (
    maybe_send_digest, maybe_send_reflection, maybe_send_backlog_triage,
    maybe_send_weekly_review, maybe_send_monthly_retro, maybe_scan_documents,
    maybe_fire_reminders, maybe_daily_sweep, maybe_notify_claude_down,
    scan_documents_now, schedule_doc_scan,
)


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
# proactive.py; the scheduled cadences that fire it (morning brief, reflection,
# backlog triage, weekly, monthly, doc scan, reminders, sweep, Claude-down nudge)
# live in scheduler.py and are re-exported at the top of this module.
def run_triage_now(conn, tg=None, chat_id=None):
    """Invoke the triage runner and report anything it reclassified back to Sam."""
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

    from core.db import connect
    conn = connect()
    # Prefer the Settings-stored bot token + allowed user (configured in the web UI);
    # fall back to the legacy .env vars for back-compat.
    token = _get_setting(conn, "telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        _log("no Telegram bot token (set it in Settings → Connections) — exiting.")
        conn.close()
        return 0
    allowed = str(_get_setting(conn, "telegram_allowed_user")
                  or os.environ.get("TELEGRAM_ALLOWED_USER_ID") or "")
    tg = Telegram(token)

    offset = int(_get_setting(conn, "telegram_offset", "0") or "0")
    sweep_due_at = None        # epoch seconds; set after a fallback capture
    import reloader
    code_baseline = reloader.snapshot()   # re-exec on a settled .py change OR a newly-connected integration
    _log(f"starting long-poll loop (offset={offset})")

    while True:
        try:
            if reloader.should_reload(code_baseline):   # pick up code edits / a new integration without a manual restart
                _log("code or integration change detected — reloading daemon")
                reloader.reexec(conn.close)
            updates = tg.get_updates(offset)
            for upd in updates:
                if "callback_query" in upd:
                    _process_callback(conn, tg, allowed, upd)
                else:
                    sweep_due_at = _process_update(conn, tg, allowed, upd, sweep_due_at)
                # Advance the cursor only AFTER the update is handled. Persisting it first
                # was at-most-once: a hard death (OOM / Synology-triggered reexec / kill) in
                # the window between the persist and the save dropped the update forever —
                # it's what ate the second of two Instagram links. Both handlers swallow
                # their own exceptions, so reaching here means the message is durably filed
                # (or safety-logged); at-least-once from here on — a re-delivered link is
                # deduped by normalized URL, so the worst case is a touch, never a lost note.
                offset = upd["update_id"] + 1
                _set_setting(conn, "telegram_offset", offset)

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
                    maybe_fire_reminders(conn, tg, allowed)
                except Exception as e:
                    _log(f"reminders failed: {e}")
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
                try:
                    maybe_send_weekly_review(conn, tg, allowed)
                except Exception as e:
                    _log(f"weekly review failed: {e}")
                try:
                    maybe_send_monthly_retro(conn, tg, allowed)
                except Exception as e:
                    _log(f"monthly retro failed: {e}")
                try:
                    maybe_scan_documents(conn)
                except Exception as e:
                    _log(f"document scan schedule failed: {e}")
                try:
                    maybe_notify_claude_down(conn, tg, allowed)
                except Exception as e:
                    _log(f"claude health notify failed: {e}")

        except Exception as e:                       # never crash the loop
            _log(f"poll error: {e}")
            time.sleep(5)                            # network backoff
    return 0


def capture_has_unsorted() -> bool:
    from domain.capture import list_unsorted_notes
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
        from ai import router
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
        elif "photo" in msg or "document" in msg:
            if _handle_photo(conn, tg, msg, chat_id):
                sweep_due_at = time.time() + SWEEP_DELAY_S
        elif "voice" in msg or "audio" in msg:
            if _handle_voice(conn, tg, msg, chat_id):
                sweep_due_at = time.time() + SWEEP_DELAY_S
        else:
            tg.send_message(chat_id, "I can handle text, links, photos and voice notes for now.")
    except Exception as e:
        _log(f"handling update {upd.get('update_id')} failed: {e}")
        _safety_log_raw(msg)   # preserve the content even if the crash was before the normal log
        if chat_id:
            try:
                tg.send_message(chat_id, "⚠️ Sorry — that one failed. It's logged and safe.")
            except Exception:
                pass
    return sweep_due_at


def _handle_text(conn, tg, chat_id, text) -> bool:
    """Route one text message. A multi-item message (several URLs/prefixed items, one per
    line) is split so each line is captured separately — otherwise line 2+ vanished into
    the first note's body. A single capture (or prose/link-with-caption) routes as-is.
    Returns True if any router fallback fired (so the caller schedules a sweep)."""
    from domain.capture import split_capture_lines
    items = split_capture_lines(text)
    if items:
        fell_back = False
        for item in items:
            fell_back = _handle_text_single(conn, tg, chat_id, item) or fell_back
        return fell_back
    return _handle_text_single(conn, tg, chat_id, text)


def _handle_text_single(conn, tg, chat_id, text) -> bool:
    """Route ONE capturable item. Fast paths (prefix/URL, instant deterministic query)
    skip Claude; everything else goes through the agentic router. Returns True if a
    router fallback fired (so the caller schedules a sweep)."""
    low = text.strip().lower()
    from ai.router import record_exchange

    # Fast path 1: prefix shortcuts → deterministic capture, no Claude. (Prefix wins over
    # URL detection, so `n: <url>` files a plain note exactly as before.)
    if low.startswith(("t:", "n:", "i:", "j:")):
        from domain.capture import route_capture
        result = route_capture(conn, text, source="telegram")
        reply = format_reply(result)
        tg.send_message(chat_id, reply)
        record_exchange(conn, text, reply)     # so "rename it"/"make it a task" resolve
        return False

    # Fast path 1b: a bare link → instant ack, then edit into the enriched reply.
    if _looks_like_url(text):
        _handle_link(conn, tg, chat_id, text)
        return False

    # Fast path 1c: a "yes"/"no" answering a pending suggestion (weekly action, profile
    # rule, calendar create). Only fires when something is actually pending, so a stray
    # "yes" otherwise routes normally.
    from ai.router import peek_pending, is_affirmation, is_rejection, execute_pending, clear_pending
    pending = peek_pending(conn)
    if pending and is_affirmation(text):
        reply = execute_pending(conn, pending)
        clear_pending(conn)
        tg.send_message(chat_id, reply)
        record_exchange(conn, text, reply)
        return False
    if pending and is_rejection(text):
        clear_pending(conn)
        tg.send_message(chat_id, "👍 Skipped.")
        record_exchange(conn, text, "👍 Skipped.")
        return False

    # Fast path 2: unambiguous list questions → instant deterministic answer, no Claude.
    from domain.queries import is_query, answer_query
    if is_query(text):
        ans = answer_query(conn, text)
        if ans is not None:
            tg.send_message(chat_id, ans)
            # Record so ordinal follow-ups ("complete the second one") see the list that
            # the deterministic tier answered — the router replays this next turn. Larger
            # cap so a full task list survives instead of truncating mid-list.
            record_exchange(conn, text, ans, reply_cap=1200)
            return False

    # Fast path 2b: a question we can answer instantly from the document facts cache
    # ("what's my Scoot booking number", "cruise price?") — no Claude, no live read.
    from domain import docs
    fact = docs.answer_from_facts(conn, text)
    if fact is not None:
        tg.send_message(chat_id, fact)
        record_exchange(conn, text, fact)
        return False

    # Fast path 3: an explicit backlog-triage request → run backlog intelligence and
    # reply, skipping the router (its only job would be to decide to run this anyway).
    from ai import proactive
    if proactive.is_backlog_triage_request(text):
        try:
            tg.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        reply = proactive.backlog_triage(conn)
        tg.send_message(chat_id, reply)
        record_exchange(conn, text, reply)
        return False

    # The agentic router — the ONE Claude entry point. Acts on instructions,
    # answers open questions, files captures, all in a single call.
    from ai import router
    try:
        tg.send_chat_action(chat_id, "typing")
    except Exception:
        pass
    # Narrate slow lookup steps (a multi-hop fetch fires several seconds-long Claude calls;
    # without this the user just sees a timed-out 'typing…'). Each update is its own message
    # and re-triggers typing so the chat keeps showing activity.
    def _progress(msg):
        try:
            tg.send_message(chat_id, msg)
            tg.send_chat_action(chat_id, "typing")
        except Exception:
            pass

    out = router.route(conn, text, source="telegram", progress=_progress)
    tg.send_message(chat_id, out["reply"], reply_markup=out.get("keyboard"))
    _maybe_send_document(tg, chat_id, out)
    if out.get("fell_back"):
        _log(f"router fell back to #unsorted: {text[:80]}")
    return bool(out.get("fell_back"))


def _maybe_send_document(tg, chat_id, out) -> None:
    """Upload any file(s) the router/lookup selected to the (allowlisted) chat. Supports a
    single `document` (legacy find_document) and a `documents` list (the agentic lookup's
    multi-file fetch). Recipient is ALWAYS this chat_id — never anything the model chose."""
    paths = list(out.get("documents") or [])
    if out.get("document"):
        paths.append(out["document"])
    for path in paths:
        if not path:
            continue
        try:
            tg.send_document(chat_id, path)
        except Exception as e:
            _log(f"document send failed: {e}")
            tg.send_message(chat_id, "⚠️ Couldn't send that file.")


def _looks_like_url(text: str) -> bool:
    from domain.capture import _looks_like_url as _u
    return _u(text)


def format_link_reply(note: dict, summary: str = "") -> str:
    """Rich link confirmation: enriched title, one-line why-it-matters, tags (minus the
    bare 'link' plumbing tag), and the URL LAST on its own line so Telegram renders a
    preview."""
    from domain import capture
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
    from domain import capture

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
    """Download an inbound photo OR document to vault/.media/. Images (photos + image
    documents) route through the agentic router so Claude can VIEW them with its Read
    tool. Non-image documents (PDF, Word, …) can't be viewed, so they're filed directly
    as a note with the file attached (deterministic, no vision call). Returns True if the
    router fell back (so the caller schedules a sweep)."""
    from domain import vault_store
    from core.db import now_sg

    photo = msg.get("photo")
    if photo:                                # Telegram sends sizes ascending → last = largest
        biggest = photo[-1]
    else:
        biggest = msg.get("document") or {}
    file_id = biggest.get("file_id")
    if not file_id:
        return False
    caption = (msg.get("caption") or "").strip()
    is_image = bool(photo) or _is_image_document(msg)

    try:
        tg.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    if is_image:
        # Keep the historical .jpg naming for images (the router reads them by path).
        uniq = biggest.get("file_unique_id") or "img"
        dest = os.path.join(vault_store.media_dir(),
                            f"{now_sg().strftime('%Y%m%d-%H%M%S')}-{uniq}.jpg")
        try:
            tg.download_file(tg.get_file_path(file_id), dest)
        except Exception as e:
            _log(f"photo download failed: {e}")
            tg.send_message(chat_id, "⚠️ Couldn't download that image — try again?")
            return False
        from ai import router
        out = router.route(conn, caption, source="telegram", image_path=dest)
        tg.send_message(chat_id, out["reply"], reply_markup=out.get("keyboard"))
        if out.get("fell_back"):
            _log(f"router fell back on photo: {caption[:80]}")
        return bool(out.get("fell_back"))

    # --- non-image document: save with its real name, file as a note, no vision ---
    orig = biggest.get("file_name") or "document"
    base = vault_store.new_media_basename(orig)
    dest = os.path.join(vault_store.media_dir(), base)
    try:
        tg.download_file(tg.get_file_path(file_id), dest)
    except Exception as e:
        _log(f"document download failed: {e}")
        tg.send_message(chat_id, "⚠️ Couldn't download that file — try again?")
        return False
    from domain import capture
    res = capture.route_capture(conn, caption, source="telegram", forced="note",
                                enrich="off", media="vault/.media/" + base)
    name = vault_store.media_display_name(base)
    tg.send_message(chat_id, f"📎 Saved: {name}" + (f" — {res.get('title')}" if caption else ""))
    schedule_doc_scan()          # a just-arrived booking becomes queryable within seconds
    return False


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
        from ai import router
        try:
            tg.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        # A long memo → ask the router to summarise into a note + pull action items.
        try:
            long_memo = len(text) >= int(_get_setting(conn, "long_voice_chars", "700"))
        except (TypeError, ValueError):
            long_memo = len(text) >= 700
        out = router.route(conn, text, source="voice", audio_path=audio_ptr, long_memo=long_memo)
        tg.send_message(chat_id, out["reply"], reply_markup=out.get("keyboard"))
        _maybe_send_document(tg, chat_id, out)
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
        from domain import vault_store
        from core.db import now_sg
        name = "voice-" + now_sg().strftime("%Y%m%d-%H%M%S") + ".oga"
        shutil.copyfile(oga_path, os.path.join(vault_store.audio_dir(), name))
        return "vault/.audio/" + name
    except OSError as e:
        _log(f"could not store audio: {e}")
        return None


if __name__ == "__main__":
    sys.exit(main())
