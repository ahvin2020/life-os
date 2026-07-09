#!/usr/bin/env bash
# Life OS triage runner — PHASE 2 SCAFFOLD (untested).
#
# Event-driven: the capture daemon triggers this after a debounced quiet period
# when unclassified items arrive, plus one daily fallback sweep (DSM Task
# Scheduler). NOT long-running. Serialised against transcription by the daemon
# (never concurrent — RAM budget).
#
# Auth: `claude setup-token` on the Mac → 1-year OAuth token exported as
# CLAUDE_CODE_OAUTH_TOKEN on the NAS. Requires Pro/Max. Do NOT use --bare (it
# ignores the token). Draws from the subscription allowance — no per-use API key.
#
# GUARD: exits 0 with a message if CLAUDE_CODE_OAUTH_TOKEN is unset, so it is a
# harmless no-op until Phase 2 is set up.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
PROMPT="$HERE/prompt.md"
PROFILE="$REPO/vault/profile.md"

if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
  echo "[run_triage] CLAUDE_CODE_OAUTH_TOKEN not set — triage is a no-op until Phase 2." >&2
  exit 0
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "[run_triage] 'claude' CLI not found (npm i -g @anthropic-ai/claude-code)." >&2
  exit 0
fi

# Gather unsorted notes (Phase 2: a python helper lists #unsorted notes; here we
# just pass the vault so the prompt can inspect them). Feed profile + prompt to
# headless Claude, capture the JSON decisions, then apply them.
PROFILE_TEXT=""
[[ -f "$PROFILE" ]] && PROFILE_TEXT="$(cat "$PROFILE")"

DECISIONS="$(
  {
    echo "=== vault/profile.md ==="
    echo "$PROFILE_TEXT"
    echo
    echo "=== TRIAGE PROMPT ==="
    cat "$PROMPT"
  } | claude -p 2>/dev/null || true
)"

echo "[run_triage] decisions:" >&2
echo "$DECISIONS" >&2

# Phase 2: apply_decisions.py parses $DECISIONS and mutates the DB/vault.
# python3 "$HERE/apply_decisions.py" <<< "$DECISIONS"
echo "[run_triage] apply step is a Phase-2 TODO." >&2
