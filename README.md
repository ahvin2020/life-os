# Life OS

A personal task / notes / journal / goals dashboard, self-hosted on a Synology
DS423+ and reachable from anywhere over Tailscale. Phone-first, dark, no login
(Tailscale is the perimeter). Captured from the web composer and from a genuinely
agentic Telegram bot.

Flask + SQLite + Jinja + vanilla JS. No build step, no external services, no
pay-per-use APIs. The only AI is `claude -p` on a Claude subscription (agentic bot +
triage) and local Whisper for voice — never a metered API.

## What's here
- **Today** — the hero: quick-add composer, today's tasks with subtask progress
  rings, day-score ring, goals rail, "captured today" feed, a view-only "this week"
  pool.
- **Tasks** — kanban (Backlog / This week / Done) with drag-to-reorder (order = your
  priority), subtasks with a fill-up ring that auto-completes the parent, recurrence,
  ☀ plan-for-today (sticky — rolls over until done), categories, per-task editor.
- **Notes** — a markdown vault (`vault/notes/*.md`, Obsidian-compatible): pinned +
  recent gallery, tag filter chips, live search, link thumbnails, a read-only "Ask"
  over your notes, modal editor with autosave, pin, soft-delete-with-undo.
- **Journal** — one free-form markdown page per day (`vault/journal/YYYY-MM-DD.md`),
  timestamped entries, an "On this day" flashback rail, and a "today so far" digest.
- **Goals** — flexible (schema v3): a goal is just a title; timeframe is
  week / month / quarter / year / by_date / ongoing, and its shape *derives* from the
  fields you fill — `measure` (current/target + unit), `rollup` (counts linked tasks),
  `milestone` (an achieved toggle), or `both`.
- **Settings** — timezone, digest/reflection hours, voice language, staleness/purge
  windows, and one-tap "run now" for the scheduled jobs.
- **Quick capture** — `POST /capture`: `t:` task, `n:` note, `i:` idea note, `j:`
  journal, a bare URL → link note (+idea for instagram/youtube/tiktok), `!` = high
  priority; anything else → an `#unsorted` note. The same deterministic router the
  Telegram bot uses for its prefix/URL fast paths.

## Quick start (Mac dev)
```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python3 server.py            # → http://localhost:5070
pytest                       # run the test suite
```
The SQLite schema is created (and migrated) on first run under `data/app.db`.

## Deploying to the NAS
See [`deploy/README.md`](deploy/README.md). Short version: Container Manager +
docker compose; code bind-mounted read-only from the Synology Drive synced folder;
`app.db` on a separate read-write, **unsynced** data volume (single-writer rule);
nightly `sqlite3 .backup` into the synced tree.

## The Telegram bot — an agentic assistant
`capture_daemon.py` long-polls Telegram and accepts messages only from one allowed
user id. **Just send it anything** — plain text, a voice note, or a photo:
- **It acts, not just files.** Every unprefixed message goes to one `claude -p` router
  (`router.py`) that reads your live tasks/goals and *does* things: "mark the CPF video
  done", "push the invoice to Friday", "add a subtask to the reel", "log that I skipped
  the gym". Ids are validated against context; deletes are soft with an inline **Undo**
  button; it remembers the last few turns so "yes" / "the second one" resolve.
- **It answers.** "what are my todos", "any overdue?", "goals", "find <term>" return
  instantly from a deterministic tier (no Claude call); anything open goes to the
  router's `answer` action.
- **Voice** → local **mlx-whisper** (original audio kept in `vault/.audio/`, pointer in
  the note). **Photos** → downloaded to `vault/.media/`, and Claude reads the image
  before deciding what to do.
- `t:`/`n:`/`i:`/`j:` prefixes and bare URLs are optional instant shortcuts.
  `triage/run_triage.py` survives only as a `--sweep` safety net for any `#unsorted`
  leftovers.

## Proactive AI (`proactive.py`)
Scheduled surfaces, each a reasoned `claude -p` call with a deterministic fallback so a
send is never dropped:
- **Morning brief** at `settings.digest_hour` (07:00 default) — names the single most
  important item and why, flags deadline collisions and goals behind pace. Sundays weave
  in a backlog triage.
- **Backlog triage** — Do / Defer / Delete verdicts over the stalest tasks, on Sundays
  and on-demand ("triage my backlog").
- **Evening reflection** at `settings.reflection_hour` (21:30 default) — 2–3 journal
  prompts grounded in concrete events from the day.

Proactive AI only ever *suggests* — it never mutates your data on its own. Sidebar
**health dots** show daemon / triage / backup heartbeats.

Runs under **launchd** on the Mac — see [`deploy/README.md`](deploy/README.md) for the
`launchctl load` steps, first-use flow, and `profile.md` personalisation. Google
Calendar (Phase 4) and bulk imports are intentionally not built.

The approved design contract lives in [`design/mockup.html`](design/mockup.html); the
architecture map and working rules are in [`CLAUDE.md`](CLAUDE.md).
