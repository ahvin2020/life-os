"""Tests for the production-hardening bits added when Life OS moved to the NAS:
  - ai.voice._fw_model_name  — mapping the stored mlx model id to a faster-whisper size
  - web_core.ai_health       — Claude health state from the call heartbeats
  - claude_cli._looks_like_auth_error / token-from-settings
"""

import os

from ai.voice import _fw_model_name
from core.web_core import ai_health
from core.db import connect, set_setting, now_iso
from ai import claude_cli


# ── faster-whisper model-name mapping ─────────────────────────────────────────
def test_fw_model_name_maps_mlx_repo_to_bare_size():
    assert _fw_model_name("mlx-community/whisper-large-v3-mlx") == "large-v3"


def test_fw_model_name_passes_through_bare_size():
    assert _fw_model_name("small") == "small"


def test_fw_model_name_defaults_when_blank():
    assert _fw_model_name(None) == "large-v3"
    assert _fw_model_name("") == "large-v3"


# ── AI/Claude health state ────────────────────────────────────────────────────
def test_ai_health_off_when_never_called(client):
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    assert ai_health(conn)["state"] == "off"
    conn.close()


def test_ai_health_ok_after_success(client):
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    with conn:
        set_setting(conn, "claude_last_ok", now_iso())
    assert ai_health(conn)["state"] == "ok"
    conn.close()


def test_ai_health_error_when_failure_is_newer(client):
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    with conn:
        set_setting(conn, "claude_last_ok", "2020-01-01T00:00:00Z")
        set_setting(conn, "claude_last_err", now_iso() + " | auth: token expired")
    st = ai_health(conn)
    conn.close()
    assert st["state"] == "error"
    assert st["auth"] is True                      # flagged as a token problem


def test_ai_health_recovers_when_success_is_newer(client):
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    with conn:
        set_setting(conn, "claude_last_err", "2020-01-01T00:00:00Z | boom")
        set_setting(conn, "claude_last_ok", now_iso())
    assert ai_health(conn)["state"] == "ok"
    conn.close()


# ── auth-error classification ─────────────────────────────────────────────────
def test_looks_like_auth_error():
    assert claude_cli._looks_like_auth_error("Error: OAuth token expired")
    assert claude_cli._looks_like_auth_error("401 Unauthorized")
    assert not claude_cli._looks_like_auth_error("model timed out reading the file")


def test_resolve_token_prefers_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok")
    assert claude_cli._resolve_token() == "env-tok"


def test_resolve_token_falls_back_to_settings(client, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    with conn:
        set_setting(conn, "claude_oauth_token", "db-tok")
    conn.close()
    assert claude_cli._resolve_token() == "db-tok"
