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

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
command -v python3 >/dev/null || die "python3 is not installed"
python3 -c 'import venv' 2>/dev/null || die "python3-venv is missing: apt install python3-venv"
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
[[ -d "$PREFIX/.venv" ]] || python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

if [[ -f "$CONF_DIR/envlog.env" ]]; then
  log "keeping the existing token in $CONF_DIR/envlog.env"
else
  log "generating a token"
  token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
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
log "dashboard: http://$(hostname -I | awk '{print $1}'):$port/"
log "next: set ENVLOG_BACKUP_DEST in $CONF_DIR/backup.conf"
