"""Resolve and invoke the local, subscription-authed Claude CLI.

ONE place that finds the `claude` binary and shells out to it, so every caller
(agentic router, triage sweep, read-only Q&A) shares the same resolution logic.

Why this exists: under launchd the daemon inherits a minimal PATH that does NOT
include ~/.local/bin, where `claude` is installed — so `subprocess.run(["claude"…])`
died with FileNotFoundError and every ambiguous capture silently stayed #unsorted.
Resolving the binary by absolute path here fixes that at the code level (and also
helps the NAS, where claude may live elsewhere and auth via CLAUDE_CODE_OAUTH_TOKEN).
"""

from __future__ import annotations

import os
import shutil
import subprocess

# Common install locations, tried in order when `claude` isn't already on PATH.
_CANDIDATES = (
    os.path.expanduser("~/.local/bin/claude"),
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
)


def claude_bin() -> str:
    """Absolute path to the `claude` executable, or the bare name as a last resort."""
    found = shutil.which("claude")
    if found:
        return found
    for c in _CANDIDATES:
        if os.path.exists(c):
            return c
    return "claude"


def has_claude() -> bool:
    return claude_bin() != "claude" or shutil.which("claude") is not None


def call_claude(prompt: str, timeout: int = 60) -> str:
    """Run `claude -p` headlessly (subscription auth, no API key) and return stdout.
    Raises subprocess.TimeoutExpired on timeout and FileNotFoundError if unresolved."""
    proc = subprocess.run(
        [claude_bin(), "-p"], input=prompt, capture_output=True, text=True, timeout=timeout)
    return proc.stdout
