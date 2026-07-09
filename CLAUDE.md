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
- `db_init.py` — `TABLES` + idempotent `init_db()` + `migrate()` gated on `meta.schema_version` (currently **2**; v2 added `tasks.deleted_at` for soft-delete). Tasks column is **`col`** ("column" is a keyword). Tasks soft-delete via `deleted_at` (undo, not confirm) — filter `deleted_at IS NULL` in every task query; `purge_deleted` hard-removes after 30 days.
- `web_core.py` — Flask `app`, persisted secret key, hand-rolled **CSRF** (session token + `before_request` guard; fetch/form patch in `base.html`), `respond()` AJAX-or-redirect, `db()`, `make_test_client()`, filters (`fdate`/`days_ago`/`due_label`), nav-count + `today` context processors.
- `server.py` — thin entry: argparse `--port/--db`, set `web_core._DB_PATH`, `init_db`, register `routes_*.bp`.
- `capture.py` — **the one router** `route_capture(conn, text, forced)` (web `/capture` today, Telegram daemon in Phase 2) + single-source `create_task()`.
- `vault_store.py` — notes/journal are markdown files under `vault/` (NOT in the DB); tiny YAML-frontmatter subset (title/tags/created/pinned/`audio`); notes soft-delete to `vault/.trash/`; voice originals kept in `vault/.audio/`; `LIFEOS_VAULT_DIR` override.
- `vault/profile.md` — **distilled triage context, injected into every `claude -p` triage call** so keep it LEAN and imperative (routing rules, not biography); it's canonical for classification (wins over message-content guesses). Longer personal reference material belongs in ordinary vault notes, never in profile.md. Starter is auto-created (never overwritten) by `triage/run_triage.py`.
- `capture_daemon.py` / `triage/run_triage.py` — Phase 2. Unprefixed text & voice are the norm → filed instantly as `#unsorted` note, then **Claude triage is the PRIMARY router** (task/note/journal) on a ~75s debounce; `t:/n:/i:/j:` still work as an undocumented shortcut. Heartbeats in `settings` (`capture_last_ran`/`triage_last_ran`/`backup_last_ran`) drive the sidebar health dots via `web_core.health_status`.
- `routes_tasks.py` — kanban CRUD **and** shared helpers (`today_tasks`, `complete_task` w/ recurrence respawn + parent auto-complete, `subtask_progress`, `archive_old_done`, `next_due_date`) imported by `routes_main`.
- `routes_main` (Today), `routes_notes`, `routes_journal`, `routes_goals` — one blueprint each.
- `web/templates/` — `base.html` (sidebar, bottom nav, FAB, note+task editor overlays, CSRF JS), `_macros.html` (task rows/cards once), per-view pages. `web/static/app.css` + `app.js` lift `design/mockup.html`.

### Design contract (`design/mockup.html` is approved — match it when touching UI)
- **Dark only. No purple, ever** — style `a, a:visited` explicitly (`--link` blue).
- **Monospace only for data** (dates, counts, slugs); sans for prose.
- **Amber = needs-attention-now only**; green/red = done/overdue only.
- **Undo toasts, never confirm dialogs.** Phone-first: bottom nav + amber quick-add FAB; touch targets ≥ 20px.
- Subtask ring: SVG circumference **100** so `stroke-dasharray = percent`.

### Conventions that bite
- **TZ Asia/Singapore** for ALL "today" logic (never UTC).
- **Today membership**: a task shows on Today iff `due==today` OR (`overdue` & not done) OR `planned_on==today` OR completed-today (dimmed). **Nothing auto-appears.**
- **Capture rules** (`t:` task, `n:` note, `i:` idea, `j:` journal, bare URL → link note, `!` → high priority, else `#unsorted`) live in **one** function (`capture.route_capture`).
- **Recurrence**: `daily | weekly:<mon..sun> | monthly:<1-28>`; completing respawns a copy at next due. **Done archives after 7 days** (`archived_at`, still queryable).
- **Goals**: `rollup` counts linked tasks (a linked parent counts once, not subtasks); `number` is tap-to-update. Week `period_start` = Monday.
- Schema change ⇒ edit `db_init.py`, bump `SCHEMA_VERSION`, add to `migrate()`.

### Scope guardrails — CUT, do not reintroduce without Kelvin asking
Habits · calendar page · time-blocking · reading-list module · content/YouTube features (those live in `youtube-assistant`) · channel analytics · nutrition/health/finance.
**No pay-per-use LLM APIs, ever** — AI means `claude -p` on the subscription (triage) + local Whisper.

### Phase state
- **Built + tested**: Phase 1 (tasks, notes) + Journal + Goals + web `/capture`.
- **Built + tested (Phase 2, Mac-first)**: `capture_daemon.py` (Telegram long-poll, sender allowlist, text/URL/voice via mlx-whisper, ack→classify→outcome replies, morning digest + Sunday stale nudge, heartbeat → health dots), event-driven Claude triage (`triage/run_triage.py`, the PRIMARY router into task/note/journal), web Change/refile on Today's feed, launchd plists in `deploy/`. Runs under launchd (see `deploy/README.md`); left STOPPED in dev. `queries.py` — the bot also ANSWERS read-only questions; intent detection is conservative (ambiguous → capture, never lose data). Two tiers: deterministic handlers first (instant/free — "what are my todos", "any overdue?", "goals", "find <term>"), then a **free-form Claude fallback** (`answer_freeform`) for open questions ("how was my week?") — builds a read-only ~12k-char context bundle (open tasks, done-this-week, goals, 7-day journal, note titles + up to 3 matching bodies, profile.md), `claude -p` 60s, strictly read-only (never mutates).
- **Not built**: Phase 4 Google Calendar (read-only, no calendar page), imports (deferred post-Phase-1). Don't build without a decision.
- Full history / decisions: `~/.claude/plans/https-www-instagram-com-p-dv9-kemevxm-ig-enumerated-tarjan.md`.
