#!/bin/sh
# GHI-TIME weekly automated restore-verification of the LATEST backup.
# A backup that has never been restored is a hope, not a backup.
#   1. restore the newest snapshot (restic if configured, else newest local)
#   2. integrity_check + sanity queries against the restored copy
#   3. record the outcome in ops_event (dashboard shows it)
set -eu

DB="${GHITIME_DB:-/opt/ghitime/data/ghitime.db}"
BACKUP_DIR="${GHITIME_BACKUP_DIR:-/opt/ghitime/backups}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ok=1
detail=""
{
  if [ -n "${RESTIC_REPOSITORY:-}" ]; then
    restic restore latest --tag ghitime --target "$WORK"
    RESTORED="$(find "$WORK" -name 'ghitime-*.db' | sort | tail -1)"
  else
    RESTORED="$WORK/copy.db"
    cp "$(ls -1t "$BACKUP_DIR"/ghitime-*.db | head -1)" "$RESTORED"
    detail="verified LOCAL snapshot (restic not configured)"
  fi
  sqlite3 "$RESTORED" "PRAGMA integrity_check;" | grep -q '^ok$'
  people=$(sqlite3 "$RESTORED" "SELECT COUNT(*) FROM person;")
  versions=$(sqlite3 "$RESTORED" "SELECT COUNT(*) FROM time_entry_version;")
  detail="$detail restored: $people people, $versions entry versions"
} || { ok=0; detail="restore-verification FAILED"; }

sqlite3 "$DB" "PRAGMA recursive_triggers=ON; INSERT INTO ops_event (kind, at, ok, detail) \
  VALUES ('restore_verify', strftime('%Y-%m-%dT%H:%M:%fZ','now'), $ok, '$detail');"

[ "$ok" -eq 1 ] || exit 1
