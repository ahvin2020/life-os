# Deploying Life OS to the Synology DS423+

> **`git push` IS the deploy.** GitHub Actions builds both images on every push to
> `main`, publishes them to `ghcr.io`, and **Watchtower** on the NAS auto-pulls them.
> **The IMAGE is the artifact — code is NOT bind-mounted, and Synology Drive is NOT the
> deploy channel** (it only syncs data off the box). An uncommitted or unpushed change is
> simply not on prod. `deploy/docker-compose.yml` is authoritative and mirrors the NAS
> copy under `/volume1/docker/projects/life-os-compose/` — keep the two in sync.

## Production

**Deploy:** `git push origin main` → CI builds `life-os-app` + `life-os-capture` →
`ghcr.io/ahvin2020/*:latest` → Watchtower pulls (nightly, or immediately via Container
Manager → **Stop → Build**, since `pull_policy: always`). Budget ~2 min of CI plus
Watchtower's poll before a change is live.

**What runs where:** two containers from ONE image — `life-os-app` (Flask web on :5070)
and `life-os-capture` (the Telegram daemon + the proactive AI schedules).

**The one mount.** `/volume1/docker/projects/life-os-compose/data` → `/data` (rw) holds
**everything durable**: `app.db`, `backups/`, the **vault** (`LIFEOS_VAULT_DIR=/data/vault`),
and the whisper model cache (`HF_HOME=/data/hf-cache`, set in `Dockerfile.capture`). The
two containers share this and **nothing else**.

> Anything durable must go through `core.db.data_dir()` (= the DB's directory), never
> `<repo>/data` — that's `/app/data` inside the image, which `.dockerignore` excludes, so
> it's container-local and wiped by every Watchtower pull. This bit hard once: the OAuth
> token lived there, so the web container had one and the bot did not.

Because the vault lives on that volume too, prod is self-contained: **one Cloud Sync task
covering `life-os-compose/` backs up db + backups + vault**. The vault used to be a second
bind mount of a personal home folder, which made prod depend on a DSM account and gave dev
and prod one shared, Drive-synced set of notes (two writers → conflict copies). It isn't
any more; the Mac's vault is dev's own fixture and diverges by design.

**Secrets** live in a sibling `.env` (chmod 600, gitignored): `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USER_ID`. The Claude token is **not** required there — see below.

**Nightly backup** is in-container: the NAS has no launchd, so `server.py` schedules it
in the **app** container, once per app-tz day at/after 03:00, and `scripts/backup_db.py`
writes a consistent snapshot of the live WAL DB into `/data/backups` (keeps the most
recent 7; override with `settings.backup_keep`). Run one any time from **Settings →
System health → Nightly backup → Run now**.

> That snapshot is the whole job, and it's the one thing only this app can do. **Getting
> the bytes off the box is Synology's job** — Cloud Sync / Hyper Backup pointed at
> `life-os-compose/`. The old app-side "offsite location" was cut (2026-07-16): it lied
> both ways — unset it silently no-op'd behind a green health dot, and set it created any
> path blind (in the container, the ephemeral `/app`) and reported success. The app can't
> verify a destination is durable, so it must not claim to. Don't re-add it.

**Voice:** the NAS has no Apple `mlx`, so `ai/voice.py` falls back to **faster-whisper**
running `large-v3` on CPU (accurate, ~20–60 s/clip on the Celeron). The ~3 GB weights
download once to `HF_HOME=/data/hf-cache` and persist across redeploys.

**Claude token (rotate without SSH):** the bot, the proactive AI, and notes "Ask" shell
out to `claude -p`, authed by `CLAUDE_CODE_OAUTH_TOKEN`. Generate a token on the Mac with
`claude setup-token` and **paste it into Settings → AI connection → Claude token**. Both
containers read it live from the shared DB — no redeploy, no `.env` edit. **Settings →
System health → AI (Claude) → Test** probes it; the sidebar dot goes red and the bot DMs
you *"AI is offline — your token may have expired"* if a call fails on auth. (Never paste
a token into chat — revoke and regenerate if you do.)

**Dev vs prod bot (separate tokens).** One Telegram token can only be polled by ONE
process — running the Mac daemon and the NAS daemon on the same token causes 409 conflicts
and dropped messages. So dev uses a **second BotFather bot**: put its token as
`TELEGRAM_BOT_TOKEN` (with your same user id) in the **Mac** repo-root `.env`. Dev and prod
also have **separate databases**, so divergent task counts are expected, not corruption —
and verifying a fix on the Mac says nothing about prod.

---

## Dev on the Mac (launchd)

The Mac is development only. Both the web server and the capture daemon run under
**launchd** so they survive reboots. Telegram queues messages server-side while the Mac
sleeps, so dev capture is delayed-but-lossless.

Secrets live in the repo-root `.env` (gitignored): the dev bot token and the allowed
Telegram user id (both from BotFather). `claude` is the Mac's already-authed CLI
(subscription, no API key). Voice notes are transcribed locally with **mlx-whisper**
(already installed into `.venv`); `ffmpeg` must be on PATH (via Homebrew).

### Load the launchd services
The plists are templated — `__REPO_DIR__` and `__HOME__` are expanded at install time, so
run this from the repo root:
```sh
for f in web capture backup; do
  sed -e "s|__REPO_DIR__|$PWD|g" -e "s|__HOME__|$HOME|g" \
      deploy/com.lifeos.$f.plist > ~/Library/LaunchAgents/com.lifeos.$f.plist
  launchctl load ~/Library/LaunchAgents/com.lifeos.$f.plist
done
```
**Upgrading from an older install (the labels were renamed)?** Do this FIRST, or you'll run
two copies of the daemon — both long-polling the same bot, so every message is handled
twice. List what's loaded, then unload + delete anything that isn't `com.lifeos.*`:
```sh
launchctl list | grep lifeos                      # find any old label
launchctl unload ~/Library/LaunchAgents/<old-label>.plist
rm -f ~/Library/LaunchAgents/<old-label>.plist
```
Check they're running: `launchctl list | grep lifeos`. Logs land in `data/`
(`capture.daemon.err.log`, `web.err.log`, and the daemon's own `capture_daemon.log`).
To stop: `launchctl unload ~/Library/LaunchAgents/com.lifeos.<web|capture>.plist`.
Settings → **Restart capture** kickstarts `com.lifeos.capture` (see `routes/settings.py`),
so that label must be the loaded one or the button 400s.

> The `capture` plist's `PATH` must include `~/.local/bin` (where `claude` lives) and the
> nvm node bin, or the router's `claude -p` dies with `FileNotFoundError` under launchd and
> every message falls back to `#unsorted`. The web plist runs `server.py --port 5070`; if
> you already run the server another way, don't load it (two servers can't share the port).

### First-use flow
1. Open Telegram → your dev bot → press **Start**.
2. Send it anything — plain text, a voice note, a photo, a link. **There are no prefixes**:
   the agentic router classifies natural language and either files it, acts on it
   ("mark the CPF video done"), or answers. Unambiguous shapes (a bare URL, a task verb, a
   parseable reminder, an instant list question) are answered deterministically in
   milliseconds; anything else costs one `claude -p` call (~5 s).
3. Ask questions too: *"what are my todos"*, *"any overdue?"*, *"goals"*, *"what's on
   tomorrow"* — read-only; nothing is saved.

### Personalise — `vault/profile.md`
`vault/profile.md` is the distilled context injected into **every** `claude -p` call, so
keep it lean and imperative: routing rules, not biography. It's canonical for
classification — it beats guesses made from the message text. Longer personal reference
material belongs in ordinary vault notes.

### Schedules
The morning brief, evening reflection, weekly review, monthly retro, and backlog triage all
have their hour/day toggles on the **Settings** page (defaults: brief 07:00, reflection
21:30, weekly review Sun 18:00). No SQL needed.
