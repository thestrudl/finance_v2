# Restore — finance_v2 (finance + renovation planner)

<!-- Template from thestrudl/spaceshuttle_infra docs/RESTORE.template.md.
     Generic procedure (Borg access, extract, dumps, volumes, other host, teardown):
     spaceshuttle_infra docs/restore.md, sections A–I. This file holds only what is
     specific to this app. Keep it current when the app changes. -->

Last verified by a restore drill: never

## What the backup contains

Production runs from `/data/production/finance_v2` on spaceshuttle (compose project `finance_v2`).
Four containers: `finance` and `finance-scheduler` (image `finance-app:latest`), and `renovation` and
`renovation-maintenance` (image `renovation-app:latest`, on with `COMPOSE_PROFILES=renovation`).
Both images are **built locally from the app dir** (no registry), so a restore rebuilds them from
the archived app dir.

| State | Where in production | In the Borg archive | Restore with |
|---|---|---|---|
| finance database (SQLite) | named volume `finance_v2_finance-data`, `/data/finance.db` | `data/backups/dumps/finance_v2/finance-finance.db` | copy into the new volume as `/data/finance.db` |
| renovation database (SQLite) | named volume `finance_v2_renovation-data`, `/data/renovation.db` | `data/backups/dumps/finance_v2/renovation-renovation.db` | copy into the new volume as `/data/renovation.db` |
| rest of the finance volume | `finance_v2_finance-data` minus `finance.db*` and `backups/` | `var/lib/docker/volumes/finance_v2_finance-data/_data` | copy into the new volume (probably empty, see below) |
| rest of the renovation volume | `finance_v2_renovation-data` minus `renovation.db*` and `backups/` | `var/lib/docker/volumes/finance_v2_renovation-data/_data` | copy into the new volume (probably empty) |
| config + secrets | `.env` in the app dir | `data/production/finance_v2/.env` | as is, then the edits under [Start order](#start-order) |
| household categories | `app/household.json` in the app dir (git-ignored, baked into the image at build) | `data/production/finance_v2/app/household.json` | stays in the app dir; the build picks it up |
| source the images were built from | the app dir | `data/production/finance_v2/` | `docker compose build` |
| deployed commit | `.env.deploy` in the app dir (written by `deploy.sh`) | `data/production/finance_v2/.env.deploy` | read it, see below |

Not in the backup on purpose:
- `/data/backups/*.db` in both volumes: the apps' own daily copies, which the central dumps replace. These jobs are marked `RETIRE` in `app/finance/maintenance.py` and `renovation/renovation/maintenance.py` and go after 7 good nights of the central runner (around 1 Dec 2026).
- `finance.db-wal` / `-shm` and `renovation.db-wal` / `-shm`: the dump is a consistent copy without them.
- Docker images: rebuilt from the app dir.

The apps keep nothing in `/data` apart from the database and `backups/` (matplotlib's cache goes to
`/tmp`), so the two `_data` folders are expected to be empty. TODO: confirm at the first drill.

**`.env.deploy` and the source.** `deploy.sh` writes `DEPLOYED_COMMIT`, `DEPLOYED_BRANCH`,
`DEPLOYED_DIRTY` (1 means tracked files differed from the commit) and `DEPLOYED_AT` after every
successful deploy. Compose doesn't read the file; it's a record. The archived app dir is the source
that actually ran, so building straight from it is right even when `DEPLOYED_DIRTY=1`. To build from
git instead: check out `DEPLOYED_COMMIT` from thestrudl/finance_v2 and copy `app/household.json` in
from the archive. If `.env.deploy` is missing (deploys made before `deploy.sh` existed, or with plain
`docker compose up -d --build`), use the archived app dir as it is.
TODO: confirm the production app dir is a git checkout, and switch deploys to `./deploy.sh`.

## Values needed that are not in `.env`

| Variable | Where it comes from | Fallback if lost |
|---|---|---|
| `app/household.json` (file, not a variable) | the archived app dir | `app/household.example.json` is used automatically. Categories already in the database keep their groups; only new spellings and merges are missing. |
| Telegram webhook registration | stored at Telegram, points at production `FINANCE_HOST` | only for a real move: `flask set-webhook` with `RESTORE_MODE=0` re-registers it |
| IBKR Flex token IP restriction | IBKR Client Portal (the token may be limited to spaceshuttle's IP) | only for a real move: allow the new IP or make a new token, then update `IBKR_FLEX_TOKEN` |
| DNS for `FINANCE_HOST` and `RENOVATION_HOST` | the DNS provider | point the names (or drill names) at the new host |

Everything else, including the secrets, the Telegram and IBKR settings, `COMPOSE_PROFILES` and
`FINANCE_API_TOKEN`, is in `.env`.

## External side effects — switch OFF before starting a restored copy

A restored copy has production credentials. Unless these are disabled it will act on the
real world.

| Side effect | Code | Setting(s) | How to disable |
|---|---|---|---|
| Telegram messages: bot replies, `report` charts, error alerts to the admin, bill reminders on the 10th/15th/20th | `app/finance/telegram.py` `api()`; callers in `bot.py`, `reports.py`, `bills.py`, `scheduler.py`, `ibkr.py` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`, `TELEGRAM_ALLOWED_IDS`, `BILL_REMINDER_*` | `RESTORE_MODE=1` |
| Telegram webhook: `flask set-webhook` would point the **production bot** at the copy | `app/finance/cli.py` `set-webhook` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_*` | `RESTORE_MODE=1` (refused). Never run it on a copy without that. |
| Incoming Telegram updates | `app/finance/bot.py` | | nothing to do: Telegram delivers only to the registered (production) URL |
| Outgoing mail: login codes (both apps), test emails | `app/finance/mail.py`, `renovation/renovation/mail.py` `deliver()` | `SMTP_*`, `MAIL_FROM`, `PORTAL_USERS`, `RENOVATION_USERS` | `RESTORE_MODE=1`: to `RESTORE_MAIL_TO`, or only to the log |
| Renovation "ready to start" email, Mon 08:00 / Fri 18:00, to everyone who can log in | `renovation/renovation/digest.py`, run by `maintenance.py` | `RENOVATION_DIGEST` | `RESTORE_MODE=1` (skipped; the slot isn't marked sent) |
| IBKR Flex sync, every morning after 07:00 and from the Holdings page (read-only, but production token, rate-limited) | `app/finance/ibkr.py` `sync()`, `due()` | `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID` | `RESTORE_MODE=1` |
| Renovation → finance API: reads the forecast, logs and edits payments as finance transactions | `renovation/renovation/ledger.py`, `money.py` | `FINANCE_API_URL` (default `http://finance:8000`), `FINANCE_API_TOKEN` | not external: it reaches the **restored** finance over the project's `internal` network. Keep `FINANCE_API_URL` unset (the default) in a restored `.env`. |

Inside the copy and left on: the monthly repeat entries (the scheduler logs any that fell due since
the dump, dated on their due days), the apps' own backups into `/data/backups`, and pruning of
old login rows.

`RESTORE_MODE=1` in `.env` (read by all four containers through `env_file: .env`):
- every page of both apps shows a red **RESTORE COPY** banner, and tab titles start with "RESTORE COPY";
- no call goes to Telegram; each attempt is logged as `RESTORE_MODE: Telegram <method> to <chat> not sent: …`;
- no IBKR sync, neither scheduled nor from the Holdings button or `flask ibkr-sync`;
- the finance scheduler skips the bill reminder;
- renovation maintenance skips the digest;
- mail goes to `RESTORE_MAIL_TO` with `[RESTORE COPY]` before the subject and the real recipient in `X-Restore-Original-To`. Without `RESTORE_MAIL_TO` nothing is sent: the message, **login code and link included**, goes to the container log, so you can still log in (`docker compose logs finance | grep -A12 "not sent to"`). `RESTORE_MAIL_TO` still sends through the production mailbox in `SMTP_*`.

Each container logs `RESTORE_MODE is on: …` once at start. Tests: `app/tests/test_restore_mode.py`,
`renovation/tests/test_restore_mode.py`. Default off; nothing needs a manual switch besides the
setting.

## Hostnames, routers, Traefik

| Router | Production host | Variable / where to change |
|---|---|---|
| `finance` (HTTPS), `finance-http` (redirect to HTTPS); `/api` is not routed | `FINANCE_HOST` in the archived `.env` | `FINANCE_HOST` in `.env`. Login links use `https://$FINANCE_HOST` (`PUBLIC_URL` overrides it). |
| `renovation` (HTTPS), `renovation-http` (redirect) | `RENOVATION_HOST` in the archived `.env` | `RENOVATION_HOST` in `.env` (`RENOVATION_PUBLIC_URL` overrides the link base) |

Traefik network `nginx-proxy` and cert resolver `letsencrypt` (spaceshuttle: `nginx-proxy`,
`letsencrypt`; rokv-serv: `webwatchdog_edge`, `le`) — set `TRAEFIK_NETWORK` and
`TRAEFIK_CERTRESOLVER` in `.env`. The entrypoints are `TRAEFIK_ENTRYPOINT_HTTPS` (default
`websecure`) and `TRAEFIK_ENTRYPOINT_HTTP` (default `web`); TODO: rokv-serv's entrypoint names. With
none of these set, `docker compose config` renders exactly what production ran before they existed.

Other things fixed in `docker-compose.yml`:
- The container names (`finance`, `finance-scheduler`, `renovation`, `renovation-maintenance`) are fixed, so a drill copy can't run on spaceshuttle next to production. Use another host.
- The project name must stay `finance_v2` (restore into a directory with that name, or pass `-p finance_v2`), so the volume names match `finance_v2_finance-data` and `finance_v2_renovation-data`.

## Start order

From the restored app dir (named `finance_v2`):

1. Edit `.env`: `RESTORE_MODE=1`, optionally `RESTORE_MAIL_TO=you@…`, and for a drill set `FINANCE_HOST` and `RENOVATION_HOST` to the drill names, plus `TRAEFIK_*` on a host other than spaceshuttle. Check `FINANCE_API_URL` isn't set. **Do this before anything starts**: the scheduler runs its first round straight away.
2. Build and create without starting: `docker compose build && docker compose create`. This also creates the two empty volumes. The external Traefik network must exist.
3. Fill the volumes, with the dumps as the databases, owned by the image's user (uid 10001). `$R` is the Borg extract:
   ```sh
   for app in finance renovation; do
     docker run --rm -v finance_v2_${app}-data:/data \
       -v "$R/var/lib/docker/volumes/finance_v2_${app}-data/_data":/src:ro \
       -v "$R/data/backups/dumps/finance_v2":/dumps:ro \
       alpine sh -c "cp -a /src/. /data/ && cp /dumps/${app}-${app}.db /data/${app}.db && chown -R 10001:10001 /data"
   done
   ```
4. Start the rest: `docker compose up -d`. `finance-scheduler` waits for `finance` to be healthy, and `renovation-maintenance` for `renovation`.

To move production for real (not a drill): set `RESTORE_MODE=0`, point DNS at the new host, then
`docker compose up -d`, `docker compose exec finance flask set-webhook` and
`docker compose exec finance flask webhook-info`, and sort out the IBKR token's IP restriction.

## Verify

- [ ] `docker compose ps`: `finance` and `renovation` healthy, `finance-scheduler` and `renovation-maintenance` running.
- [ ] `docker compose logs | grep "RESTORE_MODE is on"`: one line from each of the four containers.
- [ ] Both sites show the RESTORE COPY banner.
- [ ] Log in to finance and to the planner with a real user from `PORTAL_USERS`: the code is in `docker compose logs finance` / `docker compose logs renovation` (or in the `RESTORE_MAIL_TO` inbox).
- [ ] Recent records are from the dump date. On the finance Transactions page, or:
      `docker compose exec finance python -c "import sqlite3; print(sqlite3.connect('/data/finance.db').execute('SELECT MAX(created_at) FROM transactions').fetchone())"`.
      Repeat entries the scheduler logged after the start can be newer.
- [ ] The planner's Gantt chart shows the plan, and `docker compose exec renovation flask check-finance` gets an answer from the restored finance.
- [ ] Scheduler jobs do nothing external: `docker compose logs finance-scheduler renovation-maintenance` has no "Sent the bill reminder", "IBKR holdings synced" or "Sent the digest", and `docker compose exec finance flask webhook-info` answers `RESTORE_MODE is on: nothing is sent to Telegram`.

## Teardown of a drill copy

`docker compose down -v`, `shred -u .env`, remove the restore directory, the drill DNS records and the
locally built images (`docker image rm finance-app:latest renovation-app:latest`).
