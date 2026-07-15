# UX Standards — the vet rubric

North star (Sam): **clean · delightful · reduce wastage.** Every element earns its pixels.
Sources distilled: Anthony Hobday's Safe Rules, Laws of UX, Nielsen's heuristics, Refactoring UI.
Any screen or change in this app is judged against this file. When in doubt: remove, don't add.

## Behavior laws (apply to every interaction)
- **Jakob's Law** — work like the apps he already knows (Todoist/Notion/Telegram conventions). Novelty needs a reason.
- **Fitts's Law** — targets big and close: ≥44px touch, primary actions near the thumb/cursor path.
  (Nav is the deliberate exception — it lives in the phone top bar's hamburger, out of thumb
  reach on purpose: you navigate rarely, and the reachable spots are worth more to the FAB.)
- **Hick's Law / Choice overload** — fewer visible choices; secondary actions behind hover or ⋯.
- **Doherty Threshold** — respond <400ms; anything slower shows immediate feedback (typing indicator, optimistic UI).
- **Miller/Chunking** — group into scannable clusters (cards, sections); never a wall of rows.
- **Nielsen: status visibility** — every action confirms (toast, tick, "saved ✓"); every background job has a health signal.
- **Nielsen: user control** — undo everywhere, no confirm dialogs, esc always closes, nothing irreversible.
- **Recognition over recall** — labels not glyphs ("Do today", not ☀ alone); no syntax to memorize (bot: natural language).
- **Von Restorff** — one emphasized thing per view (amber = needs-attention-now, nothing else).
- **Peak-End** — polish the completion moments (task done, ring fills, goal achieved) — they carry the feel.
- **Tesler** — absorb complexity in code (auto-classify, auto-archive), never push it to the user.

## Visual safe rules (Hobday, filtered for this dark app)
- Near-black/near-white only; saturated neutrals in ONE temperature (ours: cool greys + amber accent is the sanctioned exception — keep neutrals cool).
- High contrast reserved for important elements; lower icon contrast when paired with text.
- **No drop shadows in dark UI** — elevation = lighter surface, not shadow. (Audit hover-lift styles.)
- Everything aligns with something; measurements mathematically related (4/8px scale); outer padding ≥ inner padding.
- Body ≥16px; line length ≤~70ch; larger text → tighter letter/line spacing.
- Button horizontal padding ≈ 2× vertical. Two typefaces max (sans + mono-for-data).
- Palette brightness values distinct; container borders contrast with both sides; no two hard divides adjacent.
- Nested corner radii: inner = outer − gap.

## Product rules (this app specifically)
- Capture is instant; classification is async; correction is one tap. Input is never lost.
- Icons decorate, words explain. No unlabeled inputs — ever. No native/browser-default controls.
- Small actions update in place (no page reload); structural changes may reload.
- Empty states teach the next action; first-run onboards. No explanatory footers on working UI.
- Row actions hidden until hover (desktop) / behind ⋯ (mobile).
- Data text (dates, counts, slugs) in mono; prose in sans. Amber = attention; green = done/positive; red = overdue/danger only.
- Mobile: top bar + hamburger drawer for nav, FAB reachable one-thumb (amended 2026-07-15 —
  the drawer slides in the same sidebar desktop uses, so nav is defined once; the FAB keeps
  the one action that genuinely needs the thumb). Nothing smaller than 44px effective.
- Every view must be judged at 1440px AND 390px before shipping, with human eyes on screenshots.

## The three vet questions per screen
1. **Clean** — what can be removed without losing function?
2. **Delightful** — which interaction here feels mechanical, and what would make it feel great?
3. **Wastage** — what space, attention, or clicks are spent without earning their cost?
