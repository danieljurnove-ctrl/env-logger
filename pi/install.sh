#!/bin/bash
# Install or update envlog on the Pi. Safe to re-run: it never overwrites an
# existing token, and never touches the database.
#
#   sudo bash pi/install.sh
#
# Everything it does is in pi/README.md; this only saves typing it out.

set -euo pipefail

PREFIX="${ENVLOG_PREFIX:-/opt/envlog}"
DATA_DIR="${ENVLOG_DATA_DIR:-/var/lib/envlog}"
CONF_DIR="${ENVLOG_CONF_DIR:-/etc/envlog}"
BACKUP_DIR="${ENVLOG_SNAPSHOT_DIR:-/var/backups/envlog}"
SERVICE_USER="${ENVLOG_USER:-envlog}"

# The interpreter the venv is built on. Overridable because "python3" is not
# always a Python the service can run: app.py imports zoneinfo (3.9+), and the
# pinned flask/waitress require 3.9+ too, while an older Raspberry Pi OS may
# still have 3.7 as its system python3. Point this at a newer interpreter --
# e.g. a 3.11 built alongside with 'make altinstall' -- rather than replacing
# the system one, which other services on the box (Pi-hole, for instance) use.
#
#   sudo ENVLOG_PYTHON=python3.11 bash pi/install.sh
#
# Only needed the first time. After that the venv already exists and we reuse
# the interpreter it was built on, so updates are a plain re-run -- otherwise
# every future `git pull && install.sh` fails on the system python again and the
# fix is a variable you have to remember from months ago.
PYTHON="${ENVLOG_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$PREFIX/.venv/bin/python" ]]; then
    PYTHON="$PREFIX/.venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
command -v "$PYTHON" >/dev/null || die "$PYTHON is not installed (set ENVLOG_PYTHON)"

# Checked up front rather than left to a traceback at first start: on 3.8 or
# older the service imports fine right up to 'from zoneinfo import ZoneInfo'
# and then dies, which reads as a broken install rather than a wrong Python.
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || \
  die "$PYTHON is $("$PYTHON" -c 'import platform; print(platform.python_version())'), but envlog needs 3.9+ (zoneinfo). Set ENVLOG_PYTHON to a newer interpreter."

"$PYTHON" -c 'import venv' 2>/dev/null || die "venv is missing for $PYTHON: apt install python3-venv"
"$PYTHON" -c 'import sqlite3' 2>/dev/null || die "$PYTHON was built without the sqlite3 module"
command -v sqlite3 >/dev/null || die "sqlite3 is missing: apt install sqlite3"

log "creating service user '$SERVICE_USER'"
id -u "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

log "creating directories"
install -d -m 0755 "$PREFIX" "$CONF_DIR"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR" "$BACKUP_DIR"

log "copying application files"
install -m 0644 "$SRC/app.py" "$SRC/schema.sql" "$SRC/requirements.txt" "$PREFIX/"
install -m 0755 "$SRC/backup.sh" "$PREFIX/"
rm -rf "${PREFIX:?}/static"
cp -r "$SRC/static" "$PREFIX/static"
chmod -R a+rX "$PREFIX/static"

log "building the virtualenv"
# Bookworm marks the system Python externally managed (PEP 668). A venv is the
# supported path; --break-system-packages is not, on a service meant to last.
[[ -d "$PREFIX/.venv" ]] || "$PYTHON" -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

if [[ -f "$CONF_DIR/envlog.env" ]]; then
  log "keeping the existing token in $CONF_DIR/envlog.env"
else
  log "generating a token"
  token="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')"
  printf 'ENVLOG_TOKEN=%s\n' "$token" > "$CONF_DIR/envlog.env"
fi
chmod 600 "$CONF_DIR/envlog.env"
chown root:root "$CONF_DIR/envlog.env"

if [[ ! -f "$CONF_DIR/backup.conf" ]]; then
  cat > "$CONF_DIR/backup.conf" <<'CONF'
# Where nightly snapshots are copied. UNSET means backups never leave the SD
# card, which is the component most likely to fail. Set this.
#ENVLOG_BACKUP_DEST="user@host:/srv/backups/envlog/"
CONF
  chmod 600 "$CONF_DIR/backup.conf"
fi

log "installing systemd units"
install -m 0644 "$SRC/systemd/envlog.service" \
                "$SRC/systemd/envlog-backup.service" \
                "$SRC/systemd/envlog-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now envlog.service envlog-backup.timer

sleep 2
if systemctl is-active --quiet envlog.service; then
  log "envlog.service is running"
else
  systemctl status envlog.service --no-pager || true
  die "envlog.service did not start"
fi

port="$(grep -oP '^ENVLOG_PORT=\K.*' "$CONF_DIR/envlog.env" 2>/dev/null || echo 8000)"
echo
log "done. Your token (needed to open the dashboard):"
grep -oP '^ENVLOG_TOKEN=\K.*' "$CONF_DIR/envlog.env"
echo
# Every address, not just the first: a box with both Ethernet and WiFi answers
# on all of them, and `hostname -I` puts them in an order that has nothing to do
# with which one you can actually reach it on.
for ip in $(hostname -I); do
  log "dashboard: http://$ip:$port/"
done
log "next: set ENVLOG_BACKUP_DEST in $CONF_DIR/backup.conf"
