"""Telegram Bot HTTP client — the thin wrapper over the getUpdates/sendMessage/
getFile calls the capture daemon uses. Split out of capture_daemon.py so the
daemon module stays focused on routing/scheduling.

`requests` is imported lazily inside the methods so importing this module stays
cheap for the tests (which fake the client entirely).
"""

from __future__ import annotations

# Long-poll timeout (seconds). The HTTP call itself waits a little longer so the
# server-side long-poll can time out first.
POLL_TIMEOUT_S = 50


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

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        """Rewrite an already-sent message (the instant-ack → rich-reply link flow)."""
        import json as _json
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            params["reply_markup"] = _json.dumps(reply_markup)
        return self._call("editMessageText", **params)

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
