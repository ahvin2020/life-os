#!/usr/bin/env python3
"""Life OS triage runner — Phase 2.

Event-driven: the capture daemon calls run() after a debounced quiet period once
ambiguous (#unsorted) captures arrive; a daily fallback sweep runs `--sweep`.
NOT long-running. Serialised against transcription by the daemon (never concurrent).

It gathers every #unsorted note, builds ONE prompt that includes vault/profile.md
(the personal classification context), asks the Mac's already-authed Claude CLI
(`claude -p`, drawing on the subscription — no per-use API key) for STRICT JSON
decisions, and applies them via the shared capture helpers (retag / note→task /
journal) — no duplicate mutation logic. Writes settings.triage_last_ran.

Auth: the Mac is logged in via `claude` already (verified). On the NAS use
`claude setup-token` → CLAUDE_CODE_OAUTH_TOKEN. Do NOT use --bare (ignores the token).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)

_PROMPT_PATH = os.path.join(_HERE, "prompt.md")
_PROFILE_PATH = os.path.join(_REPO, "vault", "profile.md")

_STARTER_PROFILE = """\
# profile.md — triage context

This file is canonical for triage classification. If it conflicts with guesses
from message content, THIS FILE WINS. Keep it distilled and imperative — it is
injected into every triage call, so keep it lean.

## Who I am
- Singapore-based investing/finance YouTuber, channel @KelvinLearnsInvesting.
- Run a sponsor business (brand deals, sponsor emails, rate cards, invoices).
- Personal life: family, errands, health, admin.

## Categories (content | business | personal)
- content — video ideas, scripts, thumbnails, research for videos.
- business — sponsors, brand deals, invoices, anything money-owed.
- personal — errands, admin, family, health, appointments.

## Tags in use
`#idea` `#link` `#research` `#business` `#content` `#personal` `#unsorted`
- Links about investing/markets or reels → keep `#link`, add `#idea`.
- Replace `#unsorted` with the best-fitting tags; never leave `#unsorted` on output.

## People
- TODO: add people you mention often and how to route them.

## Patterns
- TODO: add "X always means Y" shortcuts.
"""


def ensure_profile() -> None:
    """Create a starter profile.md if none exists. NEVER overwrites an existing one."""
    if os.path.exists(_PROFILE_PATH):
        return
    os.makedirs(os.path.dirname(_PROFILE_PATH), exist_ok=True)
    with open(_PROFILE_PATH, "w", encoding="utf-8") as f:
        f.write(_STARTER_PROFILE)


def _slug_from_path(path: str) -> str:
    return os.path.splitext(os.path.basename(path or ""))[0]


def build_prompt(profile_text: str, notes: list) -> str:
    """One prompt: profile context + the triage instructions + the items to decide."""
    with open(_PROMPT_PATH, encoding="utf-8") as f:
        instructions = f.read()
    items = []
    for n in notes:
        items.append(json.dumps({
            "path": f"vault/notes/{n['slug']}.md",
            "text": n["body"] or n["title"],
            "current_tags": n["tags"],
        }, ensure_ascii=False))
    return (
        "=== vault/profile.md (YOUR classification context) ===\n"
        f"{profile_text}\n\n"
        "=== TRIAGE INSTRUCTIONS ===\n"
        f"{instructions}\n\n"
        "=== ITEMS TO CLASSIFY (one JSON object per line) ===\n"
        + "\n".join(items)
        + "\n\nRespond with ONLY the JSON array of decisions, no prose, no code fences."
    )


def call_claude(prompt: str, timeout: int = 180) -> str:
    """Run the local, authed Claude CLI headlessly and return its stdout. Shared by
    triage and the daemon's free-form Q&A (which passes a tighter timeout)."""
    proc = subprocess.run(
        ["claude", "-p"], input=prompt, capture_output=True, text=True, timeout=timeout)
    return proc.stdout


def parse_decisions(raw: str) -> list:
    """Extract the JSON array of decisions from Claude's output (tolerates fences/prose)."""
    if not raw:
        return []
    raw = raw.strip()
    # Strip code fences if present.
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    # Grab the outermost JSON array.
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def apply_decisions(conn, decisions: list) -> list:
    """Apply triage decisions via the shared capture helpers. Returns human strings
    describing what actually changed (for the Telegram reply)."""
    import capture
    import vault_store
    from db import today_iso

    applied = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        slug = _slug_from_path(d.get("path", ""))
        action = d.get("action")
        if not slug or not vault_store.read_note(slug):
            continue
        short = (vault_store.read_note(slug)["title"] or slug)[:32]
        if action == "to_task":
            res = capture.convert_note_to_task(
                conn, slug, title=d.get("title"), category=d.get("category"),
                priority=d.get("priority"), due_date=d.get("due_date"))
            if res:
                bits = [b for b in (d.get("category"),
                                    f"due {d['due_date']}" if d.get("due_date") else None,
                                    d.get("priority") if d.get("priority") == "high" else None) if b]
                tail = (" · " + " · ".join(bits)) if bits else ""
                applied.append(f"'{short}…' → Tasks{tail}")
        elif action == "retag":
            tags = d.get("tags") or []
            if tags:
                capture.retag_note(slug, tags)
                applied.append(f"'{short}…' → Notes · #" + " #".join(tags))
        elif action == "to_journal":
            vault_store.append_journal_entry(today_iso(), vault_store.read_note(slug)["body"], source="triage")
            vault_store.delete_note(slug)
            applied.append(f"'{short}…' → today's Journal")
    return applied


def run(conn=None, claude_fn=None) -> list:
    """Gather #unsorted notes → prompt → claude → apply. Returns applied-change strings.
    `claude_fn` lets tests inject a fake model. Idempotent; safe to call repeatedly."""
    import capture
    from db import connect, now_iso

    ensure_profile()
    own_conn = conn is None
    if own_conn:
        conn = connect()

    notes = capture.list_unsorted_notes()
    applied = []
    if notes:
        profile_text = ""
        if os.path.exists(_PROFILE_PATH):
            with open(_PROFILE_PATH, encoding="utf-8") as f:
                profile_text = f.read()
        prompt = build_prompt(profile_text, notes)
        runner = claude_fn or call_claude
        try:
            raw = runner(prompt)
        except Exception as e:
            print(f"[run_triage] claude call failed: {e}", file=sys.stderr)
            raw = ""
        decisions = parse_decisions(raw)
        applied = apply_decisions(conn, decisions)

    with conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('triage_last_ran', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_iso(),))

    if own_conn:
        conn.close()
    for a in applied:
        print(f"[run_triage] {a}", file=sys.stderr)
    return applied


def main() -> int:
    from envload import load_env
    load_env()
    sweep = "--sweep" in sys.argv
    if not _has_claude():
        print("[run_triage] 'claude' CLI not found — triage is a no-op.", file=sys.stderr)
        # Still stamp the heartbeat so the dashboard shows a recent (rules-only) run.
    applied = run()
    print(f"[run_triage] {'sweep ' if sweep else ''}applied {len(applied)} change(s).",
          file=sys.stderr)
    return 0


def _has_claude() -> bool:
    from shutil import which
    return which("claude") is not None


if __name__ == "__main__":
    sys.exit(main())
