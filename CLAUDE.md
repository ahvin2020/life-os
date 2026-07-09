# CLAUDE.md — Life OS

## ⚠️ DATA SAFETY — READ FIRST
- **`vault/` is Kelvin's REAL notes + journal.** Never bulk-edit, regenerate, mass-rename, or delete vault files. Touch a single note/day only when the task explicitly says so.
- **`data/app.db` is production data** (the NAS copy is the live one). Never wipe or migrate it destructively; schema changes go through `db_init.py` `migrate()`.
- **`vault/` and `data/` are gitignored on purpose** — they are user data synced by Synology Drive, not code. Don't commit them, don't "fix" the ignore.

## Layer 1 — How to work here (behavioral rules)
- **Think before coding**: state your assumptions; if the request is ambiguous, surface the interpretations and pick with reason — don't silently guess.
- **Simplicity first**: the minimum code that solves it; no speculative features, no premature abstraction. If 200 lines could be 50, rewrite it.
- **Surgical changes**: touch only what the task needs; match existing style; don't refactor or "fix" adjacent code unasked.
- **Goal-driven / verify**: every change ends with proof — run `pytest` AND exercise the changed flow on the running server before declaring done.
- **Respect scope**: the cut list below is deliberate; don't reintroduce it without Kelvin asking.
- **No new deps lightly**: runtime is Flask-only by design; justify anything more.

## Layer 2 — Project facts (a fresh session can't guess these)
### What this is
- Kelvin's personal **life-os**: tasks / notes / journal / goals. **Single user, no login** (Tailscale is the perimeter), **phone-first**.
- Runs on his **Synology DS423+ via Docker** (Container Manager). The **Mac copy is dev-only**, synced to the NAS by **Synology Drive — the sync IS the deploy channel**; a container restart on the NAS picks up code changes.
- Stack: Flask + SQLite + Jinja + vanilla JS, **no build step**, port **5070**.

### Run / test
```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python3 server.py                 # http://localhost:5070  (--port / --db to override)
pytest                            # throwaway DB+vault via LIFEOS_DB_PATH / LIFEOS_VAULT_DIR
```

### Architecture map (one line per file)
- `db.py` — `connect()` (WAL + busy_timeout + FK, deliberate for a synced folder); `today_iso()`/`now_sg()` pinned to Asia/Singapore; `LIFEOS_DB_PATH` override.
- `db_init.py` — `TABLES` + idempotent `init_db()` + `migrate()` gated on `meta.schema_version` (currently **4**; v2 added `tasks.deleted_at`, v3 reshaped goals, **v4 added `tasks.reschedule_count`** — a postpone counter). Tasks column is **`col`** ("column" is a keyword). Tasks soft-delete via `deleted_at` (undo, not confirm) — filter `deleted_at IS NULL` in every task query; `purge_deleted` hard-removes after 30 days.
- `proactive.py` — **the three proactive AI surfaces**: `morning_brief` (07:00 AI brief, replaces the digest body), `backlog_triage` (Do/Defer/Delete over the stale backlog — scheduled Sundays + on-demand), `evening_reflection` (21:30 journal-reflection prompts). Each = a PURE `build_*_context` (unit-testable, no claude) + one prompt + a deterministic **fallback** used verbatim on any claude failure (a scheduled send is never dropped). Reaches claude ONLY via `claude_cli.call_claude`. `is_backlog_triage_request` is the deterministic on-demand trigger-phrase matcher.
- `web_core.py` — Flask `app`, persisted secret key, hand-rolled **CSRF** (session token + `before_request` guard; fetch/form patch in `base.html`), `respond()` AJAX-or-redirect, `db()`, `make_test_client()`, filters (`fdate`/`days_ago`/`due_label`), nav-count + `today` context processors.
- `server.py` — thin entry: argparse `--port/--db`, set `web_core._DB_PATH`, `init_db`, register `routes_*.bp`.
- `capture.py` — deterministic quick-capture `route_capture(conn, text, forced)` (web `/capture`, the bot's prefix/URL fast paths, and the router's #unsorted fallback) + single-source `create_task()`.
- `claude_cli.py` — **the ONE place that resolves + invokes `claude -p`** (`claude_bin()`/`call_claude()`); resolves the binary by absolute path so it survives launchd's minimal PATH (the bug that stranded #unsorted notes). Used by `router.py`, triage, and Q&A.
- `router.py` — **the agentic bot router (v2): the single `claude -p` entry point.** `route(conn, message, image_path=None)` builds compact live context (open + done-today tasks with ids, goals with ids/progress, today, journal count, **+ the last 3 (Kelvin, bot) exchanges**), asks for a STRICT JSON action, validates every id against context, and ACTS via the existing helpers. Actions: create_task/note/goal, append_journal, complete/uncomplete/plan/unplan/set_due/rename/move/delete_task, update_goal_number, answer, clarify, multi. **Images**: when `image_path` is set the prompt tells Claude to view the file with its **Read tool** (the CLI is multimodal in `-p` print mode — plain `claude -p`, prompt on stdin, no special flags) before emitting JSON; a note created from a photo gets a `media:` frontmatter pointer (mirrors `audio:`). **Exchange memory**: last `_MEM_MAX_PAIRS`=3 turns (each side capped `_MEM_ENTRY_CAP`=400 chars) persisted in `settings[router_exchanges]` (survives daemon restarts) and replayed into context EVERY turn, so "yes"/"the second one"/"change it to friday" resolve. Safety rails: raw message → `data/capture_raw.log` BEFORE the call; claude fail/invalid-JSON (after one retry) → fall back to #unsorted note; delete is SOFT; unknown id → clarify. `handle_callback()` applies inline-keyboard Undo inverses.
- `vault_store.py` — notes/journal are markdown files under `vault/` (NOT in the DB); tiny YAML-frontmatter subset (title/tags/created/pinned/`audio`/`media`); notes soft-delete to `vault/.trash/`; voice originals kept in `vault/.audio/`; photo captures kept in `vault/.media/` (pointed at by a note's `media:` frontmatter); `LIFEOS_VAULT_DIR` override.
- `vault/profile.md` — **distilled triage context, injected into every `claude -p` triage call** so keep it LEAN and imperative (routing rules, not biography); it's canonical for classification (wins over message-content guesses). Longer personal reference material belongs in ordinary vault notes, never in profile.md. Starter is auto-created (never overwritten) by `triage/run_triage.py`.
- `capture_daemon.py` — Telegram long-poll. Each message: prefix/URL → deterministic `route_capture` (no claude); unambiguous list question → `queries.answer_query` (no claude); **everything else → `router.route` (the ONE claude call), with a "typing" indicator.** Voice is transcribed locally (mlx-whisper), the .oga preserved in `vault/.audio/`, then the text goes through the same router. **Photos + image documents** (`_handle_photo`) download the highest-res copy via getFile → `vault/.media/<YYYYMMDD-HHMMSS>-<file_unique_id>.jpg`, then route through the SAME router with the caption (Claude reads the file with its Read tool). Inline-keyboard Undo taps arrive as `callback_query` → `_process_callback` → `router.handle_callback`. `triage/run_triage.py` is now ONLY the `--sweep` fallback for #unsorted leftovers — scheduled ~45s after any router fallback + once daily (`sweep_last_day`); the old debounce is gone. Heartbeats in `settings` (`capture_last_ran`/`triage_last_ran`/`backup_last_ran`) drive the sidebar health dots via `web_core.health_status`. **Outbound proactive AI** (bodies in `proactive.py`): `maybe_send_digest` → the AI **morning brief** at `digest_hour` (Sundays weave in a backlog triage); `maybe_send_reflection` → the **evening reflection** at `reflection_hour`; both guarded once-per-day. The on-demand backlog triage is a deterministic fast-path in `_handle_text` (before the router) when `proactive.is_backlog_triage_request` matches ("triage my backlog", "clean up my tasks").
- `routes_tasks.py` — kanban CRUD **and** shared helpers (`today_tasks`, `complete_task` w/ recurrence respawn + parent auto-complete, `subtask_progress`, `archive_old_done`, `next_due_date`) imported by `routes_main`.
- `routes_main` (Today), `routes_notes`, `routes_journal`, `routes_goals` — one blueprint each.
- `web/templates/` — `base.html` (sidebar, bottom nav, FAB, note+task editor overlays, CSRF JS), `_macros.html` (task rows/cards once), per-view pages. `web/static/app.css` + `app.js` lift `design/mockup.html`.

### Design contract (`design/mockup.html` is approved — match it when touching UI)
- **`design/ux-standards.md` is the vet rubric** (Hobday safe rules, Laws of UX, Nielsen, Refactoring UI — distilled); judge every screen/change against it. Notable: **no drop shadows in dark UI** (elevation = lighter surface + brighter border), labels not glyphs, row actions behind hover/⋯, judge at 1440px AND 390px with real screenshots.
- **North star: "clean, delightful, reduce wastage."** Every element earns its pixels — prefer hover-reveal/⋯ over always-visible actions, kill explanatory footers and duplicate affordances, nothing stretched, nothing microscopic, nothing that makes him think. Interactions feel polished (motion, undo, in-place updates). When in doubt on any detail: **remove rather than add.**
- **All form controls use the global dark form styles; no native/white defaults** anywhere.
- **Dark only. No purple, ever** — style `a, a:visited` explicitly (`--link` blue).
- **Monospace only for data** (dates, counts, slugs); sans for prose.
- **Amber = needs-attention-now only**; green/red = done/overdue only.
- **Undo toasts, never confirm dialogs.** Phone-first: bottom nav + amber quick-add FAB; touch targets ≥ 20px.
- Subtask ring: SVG circumference **100** so `stroke-dasharray = percent`.
- **Base 16px, main column centered** (`.main { margin-inline:auto }`, max-width kept; sidebar fixed left) — user-amended 2026-07-09.
- **Motion is additive, never floaty**: CSS props `--dur-fast/--dur/--dur-slow` (120/180/260ms) + one signature ease `--ease: cubic-bezier(.2,.8,.2,1)`; page-load rise, press-scale ~.97, card-hover lift, 0→value ring/bar fills, toast/modal/kanban-drag transitions. Everything sits under the `prefers-reduced-motion` kill-switch (disables all transition+animation). No blink/bounce/parallax/confetti.

### Conventions that bite
- **TZ Asia/Singapore** for ALL "today" logic (never UTC).
- **Voice transcription**: mlx-whisper with an EXPLICIT `language` (default `en`, settings key `voice_language`) + `condition_on_previous_text=False`, model `whisper-medium-mlx` (settings key `whisper_model`). Never rely on auto-detect — on short Singaporean-accented clips it mis-detected English as Malay and drifted into repetition loops.
- **Today membership**: a task shows on Today iff `due==today` OR (`overdue` & not done) OR `planned_on==today` OR completed-today (dimmed). **Nothing auto-appears.**
- **Capture rules** (`t:` task, `n:` note, `i:` idea, `j:` journal, bare URL → link note, `!` → high priority, else `#unsorted`) live in **one** function (`capture.route_capture`).
- **Recurrence**: `daily | weekly:<mon..sun> | monthly:<1-28>`; completing respawns a copy at next due. **Done archives after 7 days** (`archived_at`, still queryable).
- **Goals (schema v3)**: a goal is a TITLE; everything else optional. `timeframe` = week|month|quarter|year|by_date|ongoing (legacy `period`/`kind` kept, deprecated). Shape derives from fields, not `kind`: measure (`target_num`/`current_num`+`unit`) → number bar; linked tasks → rollup (a linked parent counts once, not subtasks); neither → milestone with `achieved_at` toggle (POST `/goals/<id>/achieve`, bot `mark_goal_achieved`). `ongoing` never auto-archives; `by_date` archives after `end_date`. Week `period_start` = Monday.
- Schema change ⇒ edit `db_init.py`, bump `SCHEMA_VERSION`, add to `migrate()`.
- **`settings` keys** (key/value table, all optional with code defaults):
  - `digest_hour` (int, default **7**) + `digest_last_sent` (date) — morning-brief schedule + once-per-day guard.
  - `reflection_hour` (**"HH:MM"** or bare hour, default **"21:30"**) + `reflection_last_sent` (date) — evening-reflection schedule + guard.
  - `sweep_last_day` (date) — daily #unsorted sweep floor. `telegram_offset` — long-poll cursor. `router_exchanges` (JSON) — rolling bot memory. `voice_language`/`whisper_model` — transcription overrides.
  - **`tasks.reschedule_count`** (v4) increments when a task's `due_date` moves strictly later or a set `planned_on` is cleared (via `routes_tasks.bump_reschedule`, called from `task_plan`/`task_edit` and the router's `set_due`/`unplan`); it feeds the backlog-intelligence "postponed N×" signal.

### Scope guardrails — CUT, do not reintroduce without Kelvin asking
Habits · calendar page · time-blocking · reading-list module · content/YouTube features (those live in `youtube-assistant`) · channel analytics · nutrition/health/finance.
**No pay-per-use LLM APIs, ever** — AI means `claude -p` on the subscription (triage) + local Whisper.

### Phase state
- **Built + tested**: Phase 1 (tasks, notes) + Journal + Goals + web `/capture`.
- **Built + tested (Phase 2 → v2 agentic bot, Mac-first)**: the Telegram bot is now genuinely agentic — one `claude -p` router (`router.py`) that ACTS on instructions ("mark the CPF video done", "push the invoice to Friday"), answers questions, and files captures, instead of just recording #unsorted words. `capture_daemon.py` (long-poll, sender allowlist, text/URL/voice via mlx-whisper, "typing" indicator, inline-keyboard Undo via callback_query, morning digest + Sunday stale nudge, heartbeat → health dots). `queries.py` remains the instant deterministic tier (unambiguous "what are my todos"/"any overdue?"/"goals"/"find <term>") that skips claude; anything ambiguous goes to the router, whose `answer` action is now the ONLY free-form Q&A path (`answer_freeform`/`build_context` remain as a read-only helper but the daemon no longer routes through them). `triage/run_triage.py` survives ONLY as the `--sweep` net for #unsorted fallbacks. Web Change/refile on Today's feed; launchd plists in `deploy/` (capture plist PATH now includes `~/.local/bin` so `claude` resolves). Runs under launchd; the router falls back to an #unsorted note (input first appended to `data/capture_raw.log`) if claude ever fails.
- **Built + tested (Phase 3 → proactive AI, `proactive.py`)**: the scheduled surfaces are no longer dumb templates — each is a reasoned `claude -p` call with a deterministic fallback. (1) **AI morning brief** at `digest_hour` (07:00): names THE single most important item with the deadline/dependency that makes it #1, 4-6 reasoned items, deadline×overdue-prep collision flags, and a goal-pace alert when a goal is behind pace with no linked open task (goal-pace math from period-end dates); Sundays weave in the backlog triage. (2) **Backlog intelligence**: Do/Defer/Delete verdicts over the stalest ~10 open tasks + one behavioral-pattern observation + one clarifying question — scheduled Sundays and on-demand ("triage my backlog"/"clean up my tasks"). (3) **Evening reflection** at `reflection_hour` (21:30): 2-3 reflection prompts naming concrete events from the day, building on today's journal if already written. Schema v4 (`tasks.reschedule_count`) feeds the "postponed N×" backlog signal.
- **Not built**: Phase 4 Google Calendar (read-only, no calendar page), imports (deferred post-Phase-1). Don't build without a decision.
- Full history / decisions: `~/.claude/plans/https-www-instagram-com-p-dv9-kemevxm-ig-enumerated-tarjan.md`.
