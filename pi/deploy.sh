#!/usr/bin/env bash
#
# Push this checkout to the Pi and install it, from the development machine.
#
# install.sh does the actual installation, but it assumes a checkout is already
# on the Pi and must run as root there. This script is the missing half: it
# ships pi/ over SSH and then invokes install.sh remotely. Deploying is one
# reviewable command rather than a scp followed by three ssh calls.
#
#   pi/deploy.sh [--host NAME] [--ship-only] [--restart-only] [--logs] [--test]
#
#     --host NAME     ssh target; default $ENVLOG_HOST, else 'envlog'
#     --ship-only     copy the files across; do not run install.sh
#     --restart-only  restart and health-check; ship and install nothing
#     --logs          tail the journal after a successful deploy
#     --test          run the test suite locally before shipping
#
# Requires passwordless sudo on the Pi for install.sh, which is root-equivalent.
# See docs/remote-control.md for the setup and for what that does and does not
# get you, security-wise.
#
# Uses tar over ssh rather than rsync: Windows' built-in OpenSSH ships ssh and
# scp but no rsync (docs/bring-up.md#development-machine).

set -euo pipefail

HOST="${ENVLOG_HOST:-envlog}"
RESTART_ONLY=0
SHIP_ONLY=0
SHOW_LOGS=0
RUN_TESTS=0

SRC_DIR="${ENVLOG_SRC_DIR:-envlog-src}"   # relative to the deploy user's home
SERVICE=envlog.service

usage() { sed -n '3,21p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --host)         HOST="${2:?--host needs a value}"; shift 2 ;;
        --host=*)       HOST="${1#*=}"; shift ;;
        --ship-only)    SHIP_ONLY=1; shift ;;
        --restart-only) RESTART_ONLY=1; shift ;;
        --logs)         SHOW_LOGS=1; shift ;;
        --test)         RUN_TESTS=1; shift ;;
        -h|--help)      usage 0 ;;
        *)              echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mxx %s\033[0m\n' "$*" >&2; exit 1; }

REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null) \
    || REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

[ -f pi/install.sh ] || die "pi/install.sh not found — run this from the repo"

# SRC_DIR is interpolated into a remote `rm -rf ~/$SRC_DIR`. An empty value
# there would mean `rm -rf ~/`, so refuse anything that is not a plain name.
case "$SRC_DIR" in
    ''|*/*|*' '*|.|..) die "invalid ENVLOG_SRC_DIR '$SRC_DIR' — must be a plain directory name" ;;
esac

# BatchMode turns a missing key into an immediate error rather than a password
# prompt that hangs forever with no terminal attached to answer it.
say "Checking $HOST"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true 2>/dev/null \
    || die "cannot reach '$HOST' over SSH with key auth.
   Check the Host block in ~/.ssh/config and that your public key is in
   ~/.ssh/authorized_keys on the Pi. See docs/remote-control.md."

if [ "$RUN_TESTS" -eq 1 ] && [ "$RESTART_ONLY" -eq 0 ]; then
    say "Running tests locally"
    # pytest is not in requirements.txt — CI installs it separately — so it is
    # not in the Pi's venv either. The laptop is the right place for this: a
    # Pi 2B would take minutes, and a red suite should stop the deploy here.
    # Probe by running, not by `command -v`: on Windows `python3` is a Store
    # alias stub that exists on PATH and fails when invoked
    # (docs/bring-up.md#install-windows--powershell).
    PY=""
    for c in python3 python; do
        if "$c" -c 'import sys' >/dev/null 2>&1; then PY="$c"; break; fi
    done
    [ -n "$PY" ] || die "no working python on PATH"
    "$PY" -c 'import pytest' 2>/dev/null \
        || die "pytest is not installed for $PY — 'pip install pytest', or drop --test"
    (cd pi && "$PY" -m pytest test_app.py -q) \
        || die "tests failed — nothing shipped"
fi

if [ "$RESTART_ONLY" -eq 0 ]; then
    say "Shipping pi/ to $HOST:~/$SRC_DIR"
    # .gitattributes forces LF on checkout, so scripts and units authored on
    # Windows arrive with endings the Pi will actually accept.
    # shellcheck disable=SC2029
    tar -cz --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' \
        --exclude='*.db' --exclude='.venv' --exclude='.pytest_cache' \
        --exclude='./deploy.sh' -C pi . \
        | ssh "$HOST" "rm -rf ~/$SRC_DIR && mkdir -p ~/$SRC_DIR && tar -xz -C ~/$SRC_DIR"
    # SC2029: $SRC_DIR is meant to expand here, on the client. `~` is inside
    # double quotes, so it stays literal and expands on the Pi. Validated above.
fi

if [ "$SHIP_ONLY" -eq 1 ]; then
    say "Shipped to $HOST:~/$SRC_DIR — not installed"
    echo "   Install it yourself, with a password prompt you can answer:"
    echo "     ssh -t $HOST 'sudo bash ~/$SRC_DIR/install.sh'"
    exit 0
fi

ssh "$HOST" bash -s -- "$SRC_DIR" "$SERVICE" "$RESTART_ONLY" <<'REMOTE_EOF'
set -euo pipefail
SRC_DIR="$1"; SERVICE="$2"; RESTART_ONLY="$3"
SRC="$HOME/$SRC_DIR"

say() { printf '\n\033[1m--> %s\033[0m\n' "$*"; }

if [ "$RESTART_ONLY" -eq 1 ]; then
    say "Restarting $SERVICE"
    sudo -n /usr/bin/systemctl restart "$SERVICE"
else
    say "Running install.sh"
    # install.sh is idempotent, keeps the existing token, never touches the
    # database, installs the units, and health-checks the service itself.
    # -n so a password-required sudo fails immediately instead of hanging on
    # a prompt with no terminal attached. --ship-only covers that setup.
    sudo -n bash "$SRC/install.sh" || {
        echo "passwordless sudo for install.sh is not configured on this host." >&2
        echo "Use: pi/deploy.sh --ship-only, then run install.sh yourself." >&2
        exit 1
    }
fi

# install.sh already verifies, but --restart-only skips it, and a restart can
# fail after a successful install. systemctl restart returns before a service
# has had the chance to crash, so wait and judge on evidence.
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
    echo >&2
    echo "SERVICE IS NOT RUNNING — last 30 journal lines:" >&2
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    exit 1
fi

port=$(grep -oP '^ENVLOG_PORT=\K.*' /etc/envlog/envlog.env 2>/dev/null || echo 8000)
# Any HTTP status means it is listening and serving. 401 is a pass: every
# endpoint is authenticated and this check deliberately holds no token.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/" || echo 000)
if [ "$code" = "000" ]; then
    echo >&2
    echo "Service is running but nothing answered on port $port." >&2
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    exit 1
fi

say "Healthy — HTTP $code on port $port"
REMOTE_EOF

if [ "$SHOW_LOGS" -eq 1 ]; then
    say "Tailing $SERVICE on $HOST (Ctrl-C to stop)"
    ssh -t "$HOST" "journalctl -u $SERVICE -f -n 50"
else
    say "Deployed to $HOST"
fi
