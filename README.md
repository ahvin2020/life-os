# Life OS

A personal task / notes / journal / goals dashboard, self-hosted on a Synology
DS423+ and reachable from anywhere over Tailscale. Phone-first, dark, no login
(Tailscale is the perimeter). Captured from the web composer and from a genuinely
agentic Telegram bot.

Flask + SQLite + Jinja + vanilla JS. No build step, no external services, no
pay-per-use APIs. The only AI is `claude -p` on a Claude subscription (agentic bot,
triage, and the proactive surfaces) and local Whisper for voice — never a metered
API. The assistant only ever touches Kelvin's **own** data; no Claude call in the app
ever holds web tools.

## What's here
- **Today** — the hero: quick-add composer, today's tasks with subtask progress
  rings, day-score ring, goals rail, "captured today" feed, a view-only "this week"
  pool, and a read-only Google Calendar strip.
- **Tasks** — kanban (Backlog / This week / Done) with drag-to-reorder (order = your
  priority), subtasks with a fill-up ring that auto-completes the parent, recurrence,
  ☀ plan-for-today (sticky — rolls over until done), colour-coded categories, per-task
  editor. On-today tasks pin to the top of This week; staleness is confessed in muted
  mono past thresholds.
- **Notes** — a markdown vault (`vault/notes/*.md`, Obsidian-compatible): pinned +
  recent gallery, tag filter chips, live search, link thumbnails, a read-only "Ask"
  over your notes, modal editor with autosave, pin, soft-delete-with-undo.
- **Journal** — one free-form markdown page per day (`vault/journal/YYYY-MM-DD.md`),
  timestamped entries, an "On this day" flashback rail, and a "today so far" digest.
- **Goals** — flexible (schema v3): a goal is just a title; timeframe is
  week / month / quarter / year / by_date / ongoing, and its shape *derives* from the
  fields you fill — `measure` (current/target + unit), `rollup` (counts linked tasks),
  `milestone` (an achieved toggle), or `both`.
- **Settings** — timezone, digest/reflection/weekly/monthly hours, voice language,
  staleness/purge windows, document roots, Connections (Telegram / Google / Dropbox),
  and one-tap "run now" for the scheduled jobs — all AJAX, updating in place.
- **Quick capture** — `POST /capture`: `t:` task, `n:` note, `i:` idea note, `j:`
  journal, a bare URL → link note (+idea for instagram/youtube/tiktok), `!` = high
  priority; anything else → an `#unsorted` note. The same deterministic router the
  Telegram bot uses for its prefix/URL fast paths.

## Quick start (Mac dev)
```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python3 server.py            # → http://localhost:5070
pytest                       # run the test suite (throwaway DB + vault)
```
The SQLite schema is created (and migrated) on first run under `data/app.db`.

## Deploying to the NAS
See [`deploy/README.md`](deploy/README.md). Short version: Container Manager +
docker compose; code bind-mounted read-only from the Synology Drive synced folder;
`app.db` on a separate read-write, **unsynced** data volume (single-writer rule);
nightly `sqlite3 .backup` into the synced tree. Synology Drive syncing the code folder
*is* the deploy channel — a container restart on the NAS picks up changes.

## The Telegram bot — an agentic assistant
`capture_daemon.py` long-polls Telegram and accepts messages only from one allowed
user id. **Just send it anything** — plain text, a voice note, a photo, or a document:
- **It acts, not just files.** Every unprefixed message goes to one `claude -p` router
  (`ai/router.py`) that reads your live tasks/goals and *does* things: "mark the CPF
  video done", "push the invoice to Friday", "add a subtask to the reel", "log that I
  skipped the gym". Ids are validated against context; deletes are soft with an inline
  **Undo** button; it remembers the last few turns so "yes" / "the second one" resolve.
- **It answers.** "what are my todos", "any overdue?", "goals", "find <term>" return
  instantly from a deterministic tier (no Claude call); anything open goes to the
  router's `answer` action.
- **It recalls your past.** "when did I last service the aircon?", "what did I decide
  about the reno?" → grep-and-synthesize over your notes + journal (`domain/recall.py`).
- **It finds your stuff.** "what's my flight date in August", "send me my passport",
  "how much was the cruise" → a bounded lookup over your documents, Dropbox, vault,
  tasks and goals (`domain/retrieve.py`); a daily background scan pre-extracts booking
  refs / prices / expiry dates into a facts cache so most answers are instant, and a
  just-captured document is re-scanned within seconds so it's queryable right away.
- **Reminders.** "remind me to call the bank at 3pm" → a real timed Telegram push.
- **Voice** → local **mlx-whisper** (original audio kept in `vault/.audio/`, pointer in
  the note). **Photos** → downloaded to `vault/.media/`, and Claude reads the image
  before deciding what to do. **Documents** (PDF/Word/…) → saved with the file attached.
- `t:`/`n:`/`i:`/`j:` prefixes and bare URLs are optional instant shortcuts.
  `triage/run_triage.py` survives only as a `--sweep` safety net for any `#unsorted`
  leftovers.

## Proactive AI (`ai/proactive.py`, scheduled by `scheduler.py`)
Scheduled surfaces, each a reasoned `claude -p` call with a deterministic fallback so a
send is never dropped. All the daily cadences share one gate (enabled → weekday → time
→ once-per-day guard) so the schedules can't drift:
- **Morning brief** at `settings.digest_hour` (07:00 default) — names the single most
  important item and why, flags deadline collisions, goals behind pace, and upcoming
  renewals/expiries. Sundays weave in a backlog triage.
- **Backlog triage** — Do / Defer / Delete verdicts over the stalest tasks, on Sundays
  and on-demand ("triage my backlog").
- **Evening reflection** at `settings.reflection_hour` (21:30 default) — 2–3 journal
  prompts grounded in concrete events from the day.
- **Weekly review** (Sunday) and **monthly retrospective** (first Sunday) — wins, what
  slipped, next focus, over your completions / postpones / goal pace / capture counts.

Proactive AI only ever *suggests* — it never mutates your data on its own (a "yes"
executes a pending action). Sidebar **health dots** (`core/health.py`) show daemon /
triage / backup heartbeats and Claude / integration status.

## Documents, recall & Google
- **Documents** — configurable `document_roots` (Synology Cloud Sync folders + the
  vault) are searchable and retrievable in three modes (answer a fact / send the file /
  link it, Tailscale-only). Facts are cached (`doc_facts`, schema v7) for instant answers.
- **Vault recall** — grep + date retrieval over notes and journal, then one synthesis call.
- **Google (code-ready, blocked on OAuth)** — Gmail read → brief, Calendar read → brief
  + the Today strip, calendar event write (suggest-then-confirm), Gmail **draft-only
  (never send)**. Deps are in `requirements.txt` but all imports are deferred, so the app
  stays Flask-only until you run `python3 scripts/google_auth.py` with your own GCP creds.

## Design
`/design` (`routes/design.py` + `web/templates/design.html`) is the **living style
guide** — it renders every shared component in every state from the real `app.css` +
`_macros.html`, so it can't rot. `tests/test_visual.py` screenshots it at 1440 + 390 and
diffs against committed baselines (`tests/baselines/`), so a shared-component look change
fails the suite (Playwright/Pillow are dev-only; the test skips if absent). The vet rubric
and north-star ("clean, delightful, reduce wastage") live in `design/ux-standards.md`;
`design/mockup.html` is kept only as the original approved snapshot.

Runs under **launchd** on the Mac — see [`deploy/README.md`](deploy/README.md) for the
`launchctl load` steps, first-use flow, and `vault/profile.md` personalisation. The
architecture map and working rules are in [`CLAUDE.md`](CLAUDE.md).
