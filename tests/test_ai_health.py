"""Tests for the production-hardening bits added when Life OS moved to the NAS:
  - ai.voice._fw_model_name  — mapping the stored mlx model id to a faster-whisper size
  - web_core.ai_health       — Claude health state from the call heartbeats
  - claude_cli._looks_like_auth_error / token-from-settings
"""

import os

from ai.voice import _fw_model_name
from core.web_core import ai_health, health_reasons
from core.db import connect, set_setting, now_iso, get_setting
from ai import claude_cli


# ── faster-whisper model-name mapping ─────────────────────────────────────────
def test_fw_model_name_maps_mlx_repo_to_bare_size():
    assert _fw_model_name("mlx-community/whisper-large-v3-mlx") == "large-v3"


def test_fw_model_name_passes_through_bare_size():
    assert _fw_model_name("small") == "small"


def test_fw_model_name_defaults_when_blank():
    assert _fw_model_name(None) == "large-v3"
    assert _fw_model_name("") == "large-v3"


# ── "why is this red" health reasons ──────────────────────────────────────────
def test_health_reasons_explain_stale_and_off(client):
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    with conn:
        set_setting(conn, "capture_last_ran", "2026-07-10T00:00:00Z")  # stale (budget 10 min)
        # triage/backup never stamped → 'off'
    why = health_reasons(conn)
    conn.close()
    assert "capture daemon looks stopped" in why["capture"].lower()
    assert why["triage"] and why["backup"]           # off → a reason, not blank
    # a healthy job has no reason
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    with conn:
        set_setting(conn, "capture_last_ran", now_iso())
    assert health_reasons(conn)["capture"] == ""
    conn.close()


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


# ── provider picker (OAuth, not API keys) ─────────────────────────────────────
def test_token_endpoint_saves_provider_and_token(client, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    client.post("/settings/claude-token", data={"ai_provider": "claude", "oauth_token": "tok-1"})
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    assert get_setting(conn, "ai_provider") == "claude"
    assert get_setting(conn, "claude_oauth_token") == "tok-1"
    conn.close()
    assert claude_cli.token_from_settings() == "tok-1"


def test_token_endpoint_rejects_unwired_provider(client):
    # a not-yet-wired provider must not be accepted (no token written under it)
    client.post("/settings/claude-token", data={"ai_provider": "gemini", "oauth_token": "x"})
    conn = connect(os.environ["LIFEOS_DB_PATH"])
    assert get_setting(conn, "gemini_oauth_token") is None
    conn.close()
