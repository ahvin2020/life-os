# Deploying Life OS to the Synology DS423+

> **Current model (2026-07): image-based, `git push` = deploy.** GitHub Actions builds
> both images on every push to `main`, publishes them to `ghcr.io`, and **Watchtower**
> on the NAS auto-pulls them. The sections below the line are the earlier Mac-first /
> build-on-NAS history, kept for reference. The live compose is
> `deploy/docker-compose.yml` (mirrors the NAS copy under
> `/volume1/docker/projects/life-os-compose/`).

## Production (image-based) — the current setup

**Deploy:** `git push origin main` → CI builds `life-os-app` + `life-os-capture` →
`ghcr.io/ahvin2020/*:latest` → Watchtower pulls (nightly, or immediately via
Container Manager → **Stop → Build**, since `pull_policy: always`).

**What runs where:** `life-os-app` (web on :5070, Flask) and `life-os-capture` (the
Telegram daemon + proactive AI). Both mount the persisted `/data` volume (`app.db`,
backups, whisper cache) and the synced `vault/`.

**Nightly backup** is now **in-container** (no DSM task, no launchd): `server.py`
schedules it in the app container, once per app-tz day at/after 03:00, writing to
`/data/backups` on the volume (survives redeploys). Offsite mirror is opt-in — set a
`backup_location` on the Settings page to a synced folder if you mount one. Trigger a
backup any time from **Settings → System health → Nightly backup → Run now**.

**Voice:** the NAS has no Apple `mlx`, so `ai/voice.py` falls back to
**faster-whisper** running `large-v3` on CPU (accurate, ~20-60 s/clip on the Celeron).
The ~3 GB weights download once to `HF_HOME=/data/hf-cache` and persist.

**Claude token (rotate without SSH):** the agentic bot + proactive AI + notes "Ask"
shell out to `claude -p`, authed by `CLAUDE_CODE_OAUTH_TOKEN`. Instead of editing
`.env` + restarting, generate a token on the Mac with `claude setup-token` and **paste
it into Settings → AI connection → Claude token**. Both containers read it live from
the shared DB — no redeploy. **Settings → System health → AI (Claude) → Test** probes
it; the sidebar dot goes red and the bot DMs you *"AI is offline — your token may have
expired"* if a call ever fails on auth. (Never paste a token into chat — revoke +
regenerate if you do.)

**Dev vs prod bot (separate tokens):** one Telegram token can only be polled by ONE
process — running the Mac daemon and the NAS daemon on the same token causes 409
conflicts and dropped messages. So use a **second BotFather bot for dev**: create it,
put its token as `TELEGRAM_BOT_TOKEN` (and your same user id) in the **Mac** repo-root
`.env`, and start the Mac capture daemon only when actively developing the bot. The NAS
keeps the production bot. They never collide because they poll different tokens.

---

Life OS runs as a Container Manager (docker compose) project on the NAS. The Mac is
for development only; code reaches the NAS through Synology Drive sync, and a
container restart is the whole deploy step.

## Trust model (why the volumes are split)
- **Code** — the Drive-synced `Documents/life-os` folder, mounted **read-only**.
- **Data** — `/volume1/docker/life-os/data`, mounted **read-write**, **NOT synced**.
  `app.db`, `secret_key`, and backups live here. The NAS is the *single writer* of
  `app.db`, so it never round-trips through file-sync → no SQLite corruption risk.
- **Vault** — `repo/vault`, read-write; notes + journal markdown, synced (conflicts
  become Drive conflict-copies, never corruption).

## NAS pre-flight (Kelvin, once)
1. **Package Center** → install **Container Manager** and **Tailscale**.
2. Enable **SSH** (Control Panel → Terminal & SNMP) for the initial setup.
3. Install the **Tailscale** app on your phone; sign in to the same tailnet.
4. Create the data dir:
   ```sh
   sudo mkdir -p /volume1/docker/life-os/data
   ```
5. Confirm the exact Drive-synced path for `Documents/life-os` (commonly
   `/volume1/homes/kelvin/Drive/Documents/life-os`) and update the two host paths
   in `docker-compose.yml` if they differ.

## First run (Phase 1 — web app only)
```sh
cd /volume1/homes/kelvin/Drive/Documents/life-os/deploy
sudo docker compose up -d --build app
```
Open `http://<nas-tailscale-name>:5070` from your phone (on Tailscale) or
`http://localhost:5070` on the NAS LAN. The schema is created automatically at
`/volume1/docker/life-os/data/app.db` on first start.

Dev loop thereafter: edit on the Mac → wait for Drive to sync → `sudo docker compose restart app`.

## Nightly backup (DSM Task Scheduler)
Create a scheduled root task (daily, ~03:00) running:
```sh
D=/volume1/docker/life-os/data
B=/volume1/homes/kelvin/Drive/Documents/life-os/data-backups
mkdir -p "$B"
sqlite3 "$D/app.db" ".backup '$B/app.$(date +%F).db'"
ls -1t "$B"/app.*.db | tail -n +8 | xargs -r rm   # keep 7
```
Backups land in the synced tree (write-once files → no sync risk) and mirror to the
Mac, covering total NAS failure.

## Phase 2 — Telegram capture + triage (Mac-first, via launchd)

Phase 2 runs on the **Mac** first (the NAS deploy is the last phase). Telegram queues
messages server-side while the Mac sleeps, so capture is delayed-but-lossless. Both
the web server and the capture daemon run under **launchd** so they survive reboots.

Secrets live in the repo-root `.env` (gitignored), already populated with two keys:
the BotFather bot token and the allowed Telegram user id (for @kelvin_lifeos_bot).
Triage uses the Mac's already-authed `claude` CLI (subscription, no API key). Voice
notes are transcribed locally with **mlx-whisper** (already `pip install`ed into
`.venv`); `ffmpeg` must be on PATH (it is, via Homebrew).

### Load the launchd services (Kelvin, once)
```sh
cp deploy/com.kelvin.lifeos.web.plist     ~/Library/LaunchAgents/
cp deploy/com.kelvin.lifeos.capture.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kelvin.lifeos.web.plist
launchctl load ~/Library/LaunchAgents/com.kelvin.lifeos.capture.plist
```
Check they're running: `launchctl list | grep lifeos`. Logs land in `data/`
(`capture.daemon.err.log`, `web.err.log`, and the daemon's own `capture_daemon.log`).
To stop: `launchctl unload ~/Library/LaunchAgents/com.kelvin.lifeos.<web|capture>.plist`.

> The web plist runs `server.py --port 5070`; if you already run the server another
> way, don't load the web plist (two servers can't share the port).

### First-use flow
1. Open Telegram → **@kelvin_lifeos_bot** → press **Start** (already done).
2. Send it anything — plain text or a voice note. It replies `📥 saved — filing…`,
   then within ~1–2 min the triage step sorts it into a **task**, **note**, or
   **journal** entry and replies with the outcome. `t:`/`n:`/`i:`/`j:` prefixes and
   pasted links still work as instant shortcuts.
3. Ask questions too: *"what are my todos"*, *"any overdue?"*, *"goals"*,
   *"find rate card"*, or open questions like *"how was my week?"* (read-only; nothing
   is saved).

### Personalise triage — `vault/profile.md`
`vault/profile.md` is the distilled context injected into every triage call. Edit it
to add the people you mention, your projects, and "X always means Y" shortcuts — the
TODO lines mark what to fill in. Keep it lean; it's paid for on every triage call.

### Digest hour
The morning digest (today's tasks + goals + Sunday stale-backlog nudge) is sent at
08:00 SGT by default. Change it by setting the `digest_hour` row in `settings`:
```sh
sqlite3 data/app.db "INSERT INTO settings(key,value) VALUES('digest_hour','7')
  ON CONFLICT(key) DO UPDATE SET value=excluded.value;"
```

### On the NAS (later)
The `capture` compose service (profile `capture`) runs the same daemon; there,
transcription uses `faster-whisper` and triage auth is `claude setup-token` →
`CLAUDE_CODE_OAUTH_TOKEN`. Do that when the always-on gap starts to annoy you.
