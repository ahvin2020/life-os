"""One way to put a retrieved source into a prompt — so "empty" can never mean two things.

THE bug this exists to prevent, which has now cost four separate production failures in four
different files:

  gmail_search          truncated at rank 5  ≡  "you have no cruise booking"
  _gmail_hits           widened to a generic query  ≡  "these results are relevant"
  retrieve._fetch       Google not connected  ≡  "no matching email exists"
  proactive brief       a genuinely free day  ≡  "Google is not connected"

Every one is the same mistake: a source that DIDN'T RUN, or ran and was DEGRADED, rendered
identically to a source that ran cleanly and found nothing. The model then reports a confident
true negative — fast, sourced, and wrong — and nothing in the evidence gives it a reason to
doubt. A silent failure is indistinguishable from a real answer; that is the whole problem.

It hides in two shapes, and only one of them is greppable:

    "\n".join(...) or "(none)"     a placeholder that lies
    if items: lines += [...]       the block is OMITTED — the model isn't misled, it's blind,
                                   and there's no text for it to be suspicious of

`source_block` closes both. `ran` is a REQUIRED keyword: you cannot render a source here
without stating whether it executed. That is the point — not the formatting.
"""

from __future__ import annotations


def source_block(title: str, items, line_fn=None, *, ran: bool,
                 unavailable: str = "", empty: str = "(none)", note: str = "") -> str:
    """Render ONE source as a prompt block. ALWAYS returns a block — never "" — because an
    absent block is how the router went silently calendar-blind.

    ran=False   → says the source was not checked, and that this is NOT evidence of absence.
                  `unavailable` gives the reason ("Google is not connected").
    ran + empty → says it really was checked and really is empty, in the source's own words
                  ("nothing scheduled today"), which is a FACT the model may rely on.
    ran + items → the rows, plus `note` for any degradation that still applies (truncated to
                  N of M, widened from X to Y). A degraded hit is not a clean one — say so.
    """
    lines = [title]
    if not ran:
        why = unavailable or "this source is unavailable"
        lines.append(f"  ⚠ NOT CHECKED — {why}. This is NOT evidence of absence: do NOT say "
                     "the thing doesn't exist or that there is nothing here. Say you couldn't "
                     "look, and name the source.")
        return "\n".join(lines)
    rows = [line_fn(i) if line_fn else str(i) for i in (items or [])]
    if not rows:
        lines.append(f"  {empty}")
        return "\n".join(lines)
    if note:
        lines.append(f"  ⚠ {note}")
    lines.extend(rows)
    return "\n".join(lines)
