# Life OS

A personal task / notes / journal / goals dashboard, self-hosted on a Synology
DS423+ and reachable from anywhere over Tailscale. Phone-first, dark, no login
(Tailscale is the perimeter). Captured from the web composer today and from a
Telegram bot in Phase 2.

Flask + SQLite + Jinja + vanilla JS. No build step, no external services, no
pay-per-use APIs.

## What's here (Phase 1 + Journal + Goals)
- **Today** — the hero: quick-add composer, today's tasks with subtask progress
  rings, day-score ring, goals rail, "captured today" feed.
- **Tasks** — kanban (Backlog / This week / Done) with drag-to-reorder (order = your
  priority), subtasks with a fill-up ring that auto-completes the parent, recurrence,
  ☀ plan-for-today, categories, per-task editor.
- **Notes** — a markdown vault (`vault/notes/*.md`, Obsidian-compatible): pinned +
  recent gallery, tag filter chips, live search, modal editor with autosave, pin,
  soft-delete-with-undo.
- **Journal** — one free-form markdown page per day (`vault/journal/YYYY-MM-DD.md`),
  timestamped entries, an "On this day" flashback rail, and a "today so far" digest.
- **Goals** — weekly + monthly; `rollup` (counts linked tasks) or `number` (tap to
  update); progress bars.
- **Quick capture** — `POST /capture`: `t:` task, `n:` note, `i:` idea note, `j:`
  journal, a bare URL → link note (+idea for instagram/youtube/tiktok), `!` = high
  priority; anything else → an `#unsorted` note. This is the same router the Telegram
  daemon will call in Phase 2.

## Quick start (Mac dev)
```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python3 server.py            # → http://localhost:5070
pytest                       # run the test suite
```
The SQLite schema is created on first run under `data/app.db`.

## Deploying to the NAS
See [`deploy/README.md`](deploy/README.md). Short version: Container Manager +
docker compose; code bind-mounted read-only from the Synology Drive synced folder;
`app.db` on a separate read-write, **unsynced** data volume (single-writer rule);
nightly `sqlite3 .backup` into the synced tree.

## Phase 2 (not yet wired up)
Telegram capture (`capture_daemon.py`) and Claude triage (`triage/`) are written but
untested and **no-op without their env vars** (`TELEGRAM_BOT_TOKEN`,
`CLAUDE_CODE_OAUTH_TOKEN`). Google Calendar (Phase 4) and imports are intentionally
not built.

The approved design contract lives in [`design/mockup.html`](design/mockup.html).
