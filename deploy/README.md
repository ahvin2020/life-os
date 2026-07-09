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
Open `http://<nas-tailscale-name>:5060` from your phone (on Tailscale) or
`http://localhost:5060` on the NAS LAN. The schema is created automatically at
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

## Phase 2 (later — Telegram capture + triage)
1. Create a bot with **@BotFather**, note the token; find your numeric Telegram user
   id (e.g. via @userinfobot).
2. Put them in `deploy/.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:abc...
   TELEGRAM_ALLOWED_USER_ID=11111111
   ```
3. Start the capture service:
   ```sh
   sudo docker compose --profile capture up -d --build
   ```
4. Triage auth: run `claude setup-token` on the Mac (needs Pro/Max), export the
   resulting `CLAUDE_CODE_OAUTH_TOKEN` on the NAS, and wire `triage/run_triage.sh`
   into the daemon's debounced trigger + a daily DSM fallback sweep.
5. Write a starter `vault/profile.md` (who you are, projects, categories, people) —
   the triage prompt reads it to classify captures personally.

Until those env vars exist, both the capture daemon and the triage runner exit
cleanly as no-ops, so nothing here blocks Phase 1.
