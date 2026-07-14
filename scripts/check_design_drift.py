#!/usr/bin/env python3
"""Design-drift gate for the pre-commit hook.

Keeps colour + layout decisions flowing through the CSS token system instead of
leaking into templates. Two rules, checked on STAGED ADDED LINES only (so the
existing inline styles don't block every commit — this gates NEW drift):

  ERROR  raw hex colour in a web/templates/*.html file  → colour belongs in
         app.css tokens/classes, never a template. (Pure #fff/#000 allowed for
         inline SVG icon fills.)
  WARN   new inline  style="…"  in a template           → prefer a class; a
         computed/dynamic style is fine, so this only nudges, never blocks.

Usage:
  check_design_drift.py            # gate the staged diff (pre-commit)
  check_design_drift.py --all      # audit the whole tree (prints, never fails)

Exit 1 only on an ERROR in staged mode; --all is report-only (exit 0)."""

from __future__ import annotations

import re
import subprocess
import sys

# hex like #abc / #aabbcc (3,4,6,8 digits), minus pure black/white which are
# legitimate in the icon SVGs inside _macros.html.
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
_ALLOWED_HEX = {"#fff", "#ffffff", "#000", "#000000"}
# Third-party BRAND-logo fills (Google, Dropbox, Claude, Telegram). These aren't part of
# our palette and can't be tokenised — a brand mark must use the brand's own colours — so
# they're allowlisted rather than blocked. Add a hue here only for a genuine third-party logo.
_BRAND_HEX = {"#4285f4", "#34a853", "#fbbc05", "#ea4335", "#0061ff", "#d97757", "#26a5e4"}
_INLINE_STYLE = re.compile(r'style\s*=\s*["\']')


def _hex_hits(line: str) -> list[str]:
    return [h for h in _HEX.findall(line)
            if h.lower() not in _ALLOWED_HEX and h.lower() not in _BRAND_HEX]


def _staged_added() -> dict[str, list[str]]:
    """{path: [added lines]} for staged .html templates in the current diff."""
    diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--", "web/templates/*.html"],
        capture_output=True, text=True).stdout
    out: dict[str, list[str]] = {}
    path = None
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            path = ln[6:]
            out.setdefault(path, [])
        elif ln.startswith("+") and not ln.startswith("+++") and path:
            out[path].append(ln[1:])
    return {p: v for p, v in out.items() if v}


def gate_staged() -> int:
    errors, warns = [], []
    for path, lines in _staged_added().items():
        for line in lines:
            for h in _hex_hits(line):
                errors.append(f"{path}: raw hex {h} → use an app.css token/class")
            if _INLINE_STYLE.search(line):
                warns.append(f"{path}: inline style= → prefer a class if it's static")
    for w in warns:
        print(f"  warn: {w}", file=sys.stderr)
    for e in errors:
        print(f"  DRIFT: {e}", file=sys.stderr)
    if errors:
        print("BLOCKED: colour belongs in CSS tokens, not templates. Move it to "
              "app.css (add a --var/class) and reference that.", file=sys.stderr)
        return 1
    return 0


def audit_all() -> int:
    """Whole-tree report (existing debt): every template hex + inline style."""
    import glob
    for path in sorted(glob.glob("web/templates/*.html")):
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                for h in _hex_hits(line):
                    print(f"{path}:{i}: hex {h}")
                if _INLINE_STYLE.search(line):
                    print(f"{path}:{i}: inline style=")
    return 0


if __name__ == "__main__":
    sys.exit(audit_all() if "--all" in sys.argv[1:] else gate_staged())
