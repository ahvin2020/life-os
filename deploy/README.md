# Deploying Life OS to the Synology DS423+

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
