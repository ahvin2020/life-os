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
- `db_init.py` — `TABLES` + idempotent `init_db()` + `migrate()` gated on `meta.schema_version` (currently **1**). Tasks column is **`col`** ("column" is a keyword).
- `web_core.py` — Flask `app`, persisted secret key, hand-rolled **CSRF** (session token + `before_request` guard; fetch/form patch in `base.html`), `respond()` AJAX-or-redirect, `db()`, `make_test_client()`, filters (`fdate`/`days_ago`/`due_label`), nav-count + `today` context processors.
- `server.py` — thin entry: argparse `--port/--db`, set `web_core._DB_PATH`, `init_db`, register `routes_*.bp`.
- `capture.py` — **the one router** `route_capture(conn, text, forced)` (web `/capture` today, Telegram daemon in Phase 2) + single-source `create_task()`.
- `vault_store.py` — notes/journal are markdown files under `vault/` (NOT in the DB); tiny YAML-frontmatter subset; notes soft-delete to `vault/.trash/`; `LIFEOS_VAULT_DIR` override.
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
- **Scaffold, untested, no-op without env**: `capture_daemon.py` (needs `TELEGRAM_BOT_TOKEN` + BotFather bot), `triage/` (needs `CLAUDE_CODE_OAUTH_TOKEN` via `claude setup-token`).
- **Not built**: Phase 4 Google Calendar (read-only, no calendar page), imports (deferred post-Phase-1). Don't build without a decision.
- Full history / decisions: `~/.claude/plans/https-www-instagram-com-p-dv9-kemevxm-ig-enumerated-tarjan.md`.
