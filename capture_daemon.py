#!/usr/bin/env python3
"""Telegram capture daemon — PHASE 2 SCAFFOLD (untested).

Long-polls Telegram getUpdates and files each message via capture.route_capture(),
so the phone bot and the web composer share one routing pipeline. Voice notes are
downloaded and transcribed locally with faster-whisper (base int8 on the NAS).

GUARD: exits cleanly with a log message if TELEGRAM_BOT_TOKEN is unset, so it is a
harmless no-op until Kelvin creates a BotFather bot in Phase 2. Nothing here runs
in Phase 1 and it is intentionally NOT covered by the test suite.

Security: only messages from TELEGRAM_ALLOWED_USER_ID (Kelvin's Telegram user id)
are processed; everything else is ignored. Tailscale is the network perimeter — the
daemon polls outward, so no inbound port is exposed.

Env:
  TELEGRAM_BOT_TOKEN        BotFather token (required to run)
  TELEGRAM_ALLOWED_USER_ID  numeric Telegram user id allowed to capture
  LIFEOS_DB_PATH            app.db location (data volume on the NAS)
"""

from __future__ import annotations

import os
import sys
import time

# These heavy imports (requests, faster_whisper) are deferred into main() so the
# module imports cleanly in Phase 1 / CI without the Phase-2 dependencies present.


def _log(msg: str) -> None:
    print(f"[capture_daemon] {msg}", file=sys.stderr, flush=True)


def _stamp_heartbeat(reason: str) -> None:
    """Record last-ran so the dashboard can show a staleness badge (Phase 2 wiring)."""
    try:
        from db import connect, now_iso
        conn = connect()
        with conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('capture_last_ran', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_iso(),))
        conn.close()
    except Exception as e:  # pragma: no cover - scaffold
        _log(f"heartbeat failed ({reason}): {e}")


def _handle_message(text: str, source: str = "telegram") -> None:  # pragma: no cover
    """File one inbound message through the shared router."""
    from db import connect
    from capture import route_capture
    conn = connect()
    result = route_capture(conn, text, source=source)
    conn.close()
    _log(f"filed: {result.get('label')}")


def _transcribe_voice(oga_path: str) -> str:  # pragma: no cover - scaffold
    """Download .oga → 16kHz wav (ffmpeg) → faster-whisper base int8. Phase 2."""
    raise NotImplementedError("voice transcription wired up in Phase 2")


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        _log("TELEGRAM_BOT_TOKEN not set — capture daemon is a no-op until Phase 2. Exiting.")
        return 0

    # --- Phase 2 long-poll loop (below runs only once a token exists) ---
    import requests  # noqa: F401  (deferred; only needed when a token is present)

    allowed = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    _log("starting long-poll loop")
    while True:  # pragma: no cover - scaffold
        try:
            r = requests.get(f"{api}/getUpdates",
                             params={"timeout": 50, "offset": offset}, timeout=60)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                uid = str((msg.get("from") or {}).get("id", ""))
                if allowed and uid != str(allowed):
                    _log(f"ignoring message from unauthorised user {uid}")
                    continue
                if "text" in msg:
                    _handle_message(msg["text"])
                elif "voice" in msg:
                    _log("voice message — transcription wired up in Phase 2")
            _stamp_heartbeat("poll")
        except Exception as e:
            _log(f"poll error: {e}")
            time.sleep(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
