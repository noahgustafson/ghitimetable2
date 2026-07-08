# GHI-TIME

Payroll-**prep** time tracking for Gustafson Home Improvements (GHI). Crew
captures hours on their own phones — offline in the field; admin reviews and
approves; the system exports CSVs to the bookkeeper. It **prepares payroll
inputs. It never executes payroll** — no withholding, general ledger,
invoicing, tax, legal, or insurance function anywhere. Final numbers are the
bookkeeper's.

> This build is AI-assisted (Claude Code) and operator-reviewed before use.

Stack: Python 3.12+, Flask, SQLite (WAL), Jinja2, htmx (vendored). Server-
rendered; the only client JavaScript is the PWA service worker and the
offline capture module. One Docker Compose service.

For day-to-day operation (add a worker, approve a week, run the export,
restore a backup…) see **[RUNBOOK.md](RUNBOOK.md)** — it is written for a
non-technical operator. This file covers setup and maintenance.

---

## Setup

```sh
git clone <this repo> /opt/ghitime && cd /opt/ghitime
docker compose up -d --build          # migrates automatically, then serves
docker compose exec ghitime flask create-admin gus "Gus Halvorsen"
#   prints a temp password; you must change it at first login
```

The app listens on `127.0.0.1:8080` **only**. Publish it to the tailnet with
TLS (service workers — and therefore offline capture — **require HTTPS**,
which `tailscale serve` provides on the MagicDNS hostname):

```sh
tailscale serve --bg https / http://127.0.0.1:8080
```

Crew phones must be on the tailnet (RUNBOOK: "Add a phone to the tailnet").
There is no public exposure; app login is still mandatory (lost-phone case).

Time zone: the container runs `TZ=America/Chicago`; clock times are stored
exactly as entered (single-timezone operation). Server/audit timestamps are
UTC.

Demo data (fake names, every state incl. flags and a post-approval
correction): `docker compose exec ghitime flask seed-demo` — dev only;
refuses to run on a database that already has people.

## Backups — nightly, encrypted offsite, restore-verified weekly

The database is a single SQLite file (`./data/ghitime.db`). Backup is host
cron (`ops/crontab.example`), **no maintenance task on a shorter cycle than
daily**:

- **Nightly** `ops/backup.sh`: WAL checkpoint → `sqlite3 .backup` snapshot
  (safe while the app runs) → integrity check → **encrypted offsite copy via
  restic** (target configurable via `RESTIC_REPOSITORY` +
  `RESTIC_PASSWORD_FILE` in `/etc/ghitime-backup.env`) → prune old copies →
  outcome recorded in `ops_event` (the admin dashboard shows the last
  successful backup and warns when it goes stale).
- **Weekly** `ops/restore-verify.sh`: restores the latest backup to a temp
  directory, runs `PRAGMA integrity_check` plus sanity counts, and records
  the outcome. A backup that has never been restored is a hope, not a backup.

Host needs `sqlite3` and `restic` installed. Install the cron lines from
`ops/crontab.example`.

**Restore procedure** (also in the RUNBOOK in plain words):

```sh
docker compose down
restic restore latest --tag ghitime --target /tmp/ghitime-restore
cp /tmp/ghitime-restore/.../ghitime-*.db data/ghitime.db
rm -f data/ghitime.db-wal data/ghitime.db-shm
docker compose up -d
```

## Update procedure

```sh
cd /opt/ghitime
ops/backup.sh                  # never update without a fresh backup
git pull
docker compose up -d --build   # pending forward-only migrations apply on start
```

Schema changes ship **only** as new numbered `migrations/NNN_*.sql` files —
`001_init.sql` is never edited. Roll forward, never back; if an update goes
wrong, restore the pre-update backup.

## Unattended for a month

- `restart: always` in compose brings the app back after crashes/reboots
  (enable Docker at boot: `systemctl enable docker`).
- WAL checkpoint runs inside the nightly backup.
- App logs rotate via Docker (`max-size: 10m`, 3 files); backup logs via the
  cron redirection.
- Backups and restore-verification are cron, not memory.

## Rules for future development

- **No report or export title may ever contain "margin" or "profit."** Only
  labor cost is captured here; revenue and margin live with the bookkeeper.
  This is a deliberate scope rule — do not "improve" it away.
- Every money or quantity value carries a tag (`SOURCE` / `CALCULATED` /
  `ALLOCATED` / `ESTIMATED` / `EXTERNAL`); missing values render blank and
  visibly flagged — never defaulted, never invented.
- Append-only tables stay append-only; every writing connection sets
  `PRAGMA recursive_triggers=ON` (see `migrations/001_init.sql` engine notes).
- `rate_bill` is admin-only and must never render on a worker-facing page or
  worker-facing export (tested in `tests/test_auth.py`).
- Changing `workweek_start_dow` mid-history is **out of scope for V1**: it is
  set once at go-live (Monday, per operator decision) and left alone.

## Development

```sh
pip install -r requirements.txt
python validate_schema.py      # schema invariant proof (also run by CI)
pytest -q                      # full suite
flask --app ghitime migrate && flask --app ghitime run --debug
```

CI (`.github/workflows/ci.yml`) runs `validate_schema.py` and the full pytest
suite on every PR to `main`.
