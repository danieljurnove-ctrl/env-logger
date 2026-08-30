#!/bin/bash
# Nightly snapshot of the envlog database.
#
# VACUUM INTO, never cp: a plain copy of a live WAL database can be corrupt,
# because the .db file alone is not a consistent view while the -wal file holds
# committed pages. VACUUM INTO asks SQLite for a consistent snapshot instead.
#
# The stated purpose of this project is multi-year trends, and the storage medium
# is an SD card in a Pi -- the most failure-prone component in the system.
# Accepting gaps in the record is not the same as accepting the loss of all of it.
#
# Configure by editing /etc/envlog/backup.conf, which is sourced if it exists.

set -euo pipefail

DB="${ENVLOG_DB:-/var/lib/envlog/envlog.db}"
SNAPSHOT_DIR="${ENVLOG_SNAPSHOT_DIR:-/var/backups/envlog}"
WEEKLY_DIR="$SNAPSHOT_DIR/weekly"
KEEP_DAILY="${ENVLOG_KEEP_DAILY:-7}"
KEEP_WEEKLY="${ENVLOG_KEEP_WEEKLY:-4}"

# Where snapshots are copied off this box. UNSET BY DEFAULT, deliberately: a
# backup that never leaves the SD card is not a backup. Set this to an
# rsync/scp-style destination, e.g.
#   ENVLOG_BACKUP_DEST="user@host.tailnet-name.ts.net:/srv/backups/envlog/"
ENVLOG_BACKUP_DEST="${ENVLOG_BACKUP_DEST:-}"

# shellcheck source=/dev/null
[[ -f /etc/envlog/backup.conf ]] && source /etc/envlog/backup.conf

log() { printf '%s envlog-backup: %s\n' "$(date -Is)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ -f "$DB" ]] || die "no database at $DB"
command -v sqlite3 >/dev/null || die "sqlite3 is not installed"

mkdir -p "$SNAPSHOT_DIR" "$WEEKLY_DIR"

today="$(date +%F)"
snapshot="$SNAPSHOT_DIR/envlog-$today.db"

# VACUUM INTO refuses to write to a path that already exists, so snapshot to a
# temporary name and rename into place. That also means a run interrupted
# halfway never leaves a truncated file where a good snapshot used to be, and a
# second run on the same day -- a manual one after the timer has already fired,
# or a Persistent=true catch-up -- simply replaces the earlier snapshot instead
# of failing.
tmp="$SNAPSHOT_DIR/.envlog-$today.$$.tmp"
trap 'rm -f -- "$tmp"' EXIT
rm -f -- "$tmp"

# Quote the path for SQL. Single quotes are the only character that matters here
# and a date never contains one, but the snapshot dir is configurable.
sql_path="${tmp//\'/\'\'}"
sqlite3 "$DB" "VACUUM INTO '$sql_path'" || die "VACUUM INTO failed"

# Verify the snapshot opens and has rows BEFORE it replaces the previous one or
# anything is pruned on its behalf.
count="$(sqlite3 "$tmp" "SELECT count(*) FROM readings" 2>/dev/null)" \
  || die "snapshot did not open cleanly -- keeping older backups"

mv -f -- "$tmp" "$snapshot"
log "wrote $snapshot ($(du -h "$snapshot" | cut -f1)), $count readings"

# Sunday's snapshot is promoted to the weekly set. A hard link, so it costs
# nothing until the daily copy is pruned.
if [[ "$(date +%u)" == "7" ]]; then
  ln -f "$snapshot" "$WEEKLY_DIR/envlog-$today.db"
  log "promoted to weekly"
fi

prune() {
  local dir="$1" keep="$2"
  # shellcheck disable=SC2012  # names are ISO dates, so ls sorts chronologically
  ls -1 "$dir"/envlog-*.db 2>/dev/null | sort -r | tail -n "+$((keep + 1))" |
    while read -r old; do
      log "pruning $old"
      rm -f -- "$old"
    done
}
prune "$SNAPSHOT_DIR" "$KEEP_DAILY"
prune "$WEEKLY_DIR" "$KEEP_WEEKLY"

if [[ -z "$ENVLOG_BACKUP_DEST" ]]; then
  log "WARNING: ENVLOG_BACKUP_DEST is unset -- snapshot stayed on this SD card."
  log "         Set it in /etc/envlog/backup.conf. See docs/design.md#backups."
  exit 0
fi

if command -v rsync >/dev/null; then
  rsync -a --delete "$SNAPSHOT_DIR/" "$ENVLOG_BACKUP_DEST" \
    || die "rsync to $ENVLOG_BACKUP_DEST failed"
else
  # Windows' built-in OpenSSH has scp but not rsync, so this path matters if the
  # off-box target is the dev laptop.
  scp -q "$snapshot" "$ENVLOG_BACKUP_DEST" \
    || die "scp to $ENVLOG_BACKUP_DEST failed"
fi
log "copied off-box to $ENVLOG_BACKUP_DEST"
