#!/bin/sh
# GHI-TIME nightly backup (host cron; see ops/crontab.example).
#   1. WAL checkpoint (keeps the -wal file from growing unbounded)
#   2. sqlite3 .backup  -> consistent snapshot even while the app runs
#   3. restic           -> encrypted offsite copy (target configurable)
#   4. record the outcome in ops_event so the dashboard shows it
set -eu

DB="${GHITIME_DB:-/opt/ghitime/data/ghitime.db}"
BACKUP_DIR="${GHITIME_BACKUP_DIR:-/opt/ghitime/backups}"
# restic target is configurable: set RESTIC_REPOSITORY + RESTIC_PASSWORD_FILE
# in /etc/ghitime-backup.env (sourced by the cron line).
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SNAP="$BACKUP_DIR/ghitime-$STAMP.db"

mkdir -p "$BACKUP_DIR"

ok=1
detail=""
{
  sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
  sqlite3 "$DB" ".backup '$SNAP'"
  sqlite3 "$SNAP" "PRAGMA integrity_check;" | grep -q '^ok$'
  if [ -n "${RESTIC_REPOSITORY:-}" ]; then
    restic backup "$SNAP" --tag ghitime
    restic forget --tag ghitime --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
  else
    detail="restic not configured — snapshot is LOCAL ONLY"
  fi
  # keep the last 14 local snapshots
  ls -1t "$BACKUP_DIR"/ghitime-*.db | tail -n +15 | xargs -r rm --
} || { ok=0; detail="backup failed at: $?"; }

sqlite3 "$DB" "PRAGMA recursive_triggers=ON; INSERT INTO ops_event (kind, at, ok, detail) \
  VALUES ('backup', strftime('%Y-%m-%dT%H:%M:%fZ','now'), $ok, '$detail');"

[ "$ok" -eq 1 ] || exit 1
