# Letting Claude Code drive the Pi

Most of what is left on the roadmap happens *on* the Pi: installing the service, reading
`journalctl` when it won't start, poking the database, deploying a fix and watching it come back
up. Copy-pasting those commands between a chat window and a terminal is slow and error-prone.

This document is how to let an agent run them itself, and — the part worth reading carefully —
where the limits actually are, as opposed to where they look like they are.

---

## What does not work: cloud sessions

Claude Code sessions running in the cloud **cannot reach this Pi**, and no configuration changes
that. Two independent reasons:

- **No SSH egress.** The session container has no `ssh` binary, and raw TCP to port 22 does not
  leave the network. Outbound traffic is HTTPS through a policy-enforcing proxy; even git's SSH
  URLs get rewritten to HTTPS.
- **The Pi is behind home NAT.** It has no public address, so nothing on the internet can open a
  connection to it whatever the protocol.

A cloud session can still read the repo, write code, and push a branch. It just cannot deploy.
The pull-based option at the end of this document is the shape that works from there.

---

## What does work: run Claude Code locally

Run locally and the agent has a shell on the development laptop — the same laptop that already
has an OpenSSH client and already reaches the Pi. "Controlling the Pi" then needs no integration
at all. It is `ssh`.

### 1. A host alias

```
# ~/.ssh/config
Host envlog
    HostName envlog.home
    User daniel                     # a login account, NOT the envlog service user
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30

# Fallback for when Pi-hole is the thing that is broken. envlog.home resolves
# through it, so name resolution dies exactly when you most need to log in.
Host envlog-ip
    HostName 192.168.1.50           # set to the Pi's static lease
    User daniel
    IdentityFile ~/.ssh/id_ed25519
```

The second block matters more than it looks. The root README lists Pi-hole as a dependency for
ingest; it is equally a dependency for *reaching the box by name*. Keep an address-based route in.

**Do not try to log in as `envlog`.** `install.sh` deliberately creates it as
`--system --no-create-home --shell /usr/sbin/nologin`: it is the account the service drops into,
not an account for people. Deploy from an ordinary login user.

### 2. Key authentication, not passwords

An interactive password prompt will hang the agent's Bash tool — there is no terminal attached to
type into. `deploy.sh` connects with `BatchMode=yes` precisely so a missing key fails immediately
with a useful message rather than hanging forever.

```powershell
# On the Windows laptop
ssh-keygen -t ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh daniel@envlog.home `
  "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

`ssh-copy-id` does not ship with Windows OpenSSH, hence the pipe.

### 3. Pick one shell and stay in it

Claude Code's Bash tool on Windows runs through Git Bash, which ships its **own** `ssh` and reads
`~/.ssh/config` from `C:\Users\<you>\.ssh`. Running Claude Code inside WSL instead means a
different home directory, a different `~/.ssh`, and a different key.

Set up whichever one you actually launch Claude Code from. Splitting keys across both is a
reliable way to spend an evening on `Permission denied (publickey)`.

---

## The honest part: what the limits are worth

The obvious move is to allowlist SSH in `.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["Bash(ssh envlog:*)", "Bash(scp:*)"]
  }
}
```

Worth doing — it removes a prompt on every command. But be clear about what it buys: **nothing,
security-wise.** Permission rules match a command prefix, and with `ssh` everything interesting is
the arbitrary string after the prefix. `ssh envlog:*` approves every possible remote command at
once.

It is tempting to fix that with narrow `sudo`. For deployment specifically, that does not work
either, and it is better to know why than to build something that only looks safe:

- `install.sh` must run as root. It creates a system user, writes `/opt/envlog` and
  `/etc/systemd/system`, and enables units. There is no subset of that which is not root.
- Narrowing sudo to *that one script path* is illusory, because the deploy user is the one who
  just wrote that file. Anything that can `deploy.sh` can replace `install.sh` first.
- Granting `install`, `cp`, or `tee` through sudo is granting root outright. A narrowing that
  permits an arbitrary root-owned write is decorative.

So: **an agent that can deploy this service can root this Pi.** That is a real decision, and on a
box that also serves the household's DNS it deserves to be made deliberately rather than
discovered later. Pick a posture:

### Posture A — full autonomy

One NOPASSWD entry, understanding it is root-equivalent:

```sh
sudo visudo -f /etc/sudoers.d/envlog-deploy
```

```
daniel ALL=(root) NOPASSWD: /usr/bin/bash /home/daniel/envlog-src/install.sh, \
                            /usr/bin/systemctl restart envlog.service, \
                            /usr/bin/systemctl start envlog.service, \
                            /usr/bin/systemctl stop envlog.service
```

`pi/deploy.sh` then works end to end unattended. Choose this if you would have typed the same
commands yourself anyway and you keep backups — which the nightly `envlog-backup.timer` gives you,
provided `ENVLOG_BACKUP_DEST` is actually set.

### Posture B — human in the loop for the privileged step

Grant only the genuinely narrow verbs:

```
daniel ALL=(root) NOPASSWD: /usr/bin/systemctl restart envlog.service, \
                            /usr/bin/systemctl start envlog.service, \
                            /usr/bin/systemctl stop envlog.service
```

Day-to-day agent work — restart, read logs, query the API, run the simulator — stays fully
automatic via `pi/deploy.sh --restart-only`. Installs become two steps, the second of which you
run:

```sh
pi/deploy.sh --ship-only
ssh -t envlog 'sudo bash ~/envlog-src/install.sh'
```

This is the better default until the deploy loop has earned some trust.

Either way, the sudoers file must be mode `0440` and must not contain CRLF — a sudoers file with
Windows line endings is a syntax error, and a broken one locks you out of `sudo` entirely. Always
edit it with `visudo`, which refuses to save a file that does not parse.

For logs, add the account to the journal group rather than handing it more sudo:

```sh
sudo usermod -aG systemd-journal daniel
```

`systemctl status` and `is-active` need no privilege at all.

### What is already protecting you

The service itself is confined regardless of any of the above.
[`envlog.service`](../pi/systemd/envlog.service) runs as the `envlog` system user under
`ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges=yes`, with `ReadWritePaths` limited to
`/var/lib/envlog`. A bug in `app.py` cannot reach Pi-hole's configuration. That is a separate
question from what a deploy can do, but it is the layer that holds while the service is running.

---

## Deploying

[`pi/install.sh`](../pi/install.sh) already does the installation, and does it well — idempotent,
keeps the existing token, never touches the database, health-checks the service. What it assumes
is a checkout already on the Pi.

`pi/deploy.sh` is the missing half. It runs on the laptop, ships `pi/` over SSH into
`~/envlog-src`, and invokes `install.sh` there:

```sh
pi/deploy.sh                    # ship, install, restart, verify
pi/deploy.sh --test             # run the test suite locally first; a red suite ships nothing
pi/deploy.sh --logs             # ... then tail the journal
pi/deploy.sh --restart-only     # restart and health-check only
pi/deploy.sh --ship-only        # copy files, leave install.sh to you (posture B)
pi/deploy.sh --host envlog-ip   # when Pi-hole is down and DNS with it
```

Notes on why it is built the way it is:

- **tar over SSH, not rsync.** Windows OpenSSH ships `ssh` and `scp` but no `rsync`, as
  [bring-up.md](bring-up.md#development-machine) notes. `tar -cz … | ssh …` needs nothing on the
  laptop that is not already there. `.gitattributes` forces LF on checkout, so units and scripts
  authored on Windows arrive with endings the Pi accepts.
- **Tests run on the laptop, not the Pi.** `pytest` is not in `requirements.txt` — CI installs it
  separately — so it is not in the Pi's venv, and a Pi 2B would take minutes to run a suite the
  laptop finishes in under a second.
- **It judges success on evidence.** `systemctl restart` returns before a service has had the
  chance to crash, so the script waits, checks `is-active`, then makes an HTTP request. Any status
  code counts as healthy — every endpoint is authenticated and the script deliberately holds no
  token, so a `401` still proves it is up and serving. On failure it dumps the last 30 journal
  lines and exits non-zero.

---

## Making it work from outside the house

Tailscale is already on the roadmap for dashboard access, and it upgrades this too: with the Pi on
the tailnet, the laptop reaches it from anywhere. Add a third `Host` block pointed at the tailnet
IP. Nothing else changes — still SSH, still key auth, still the same account and posture.

---

## Two things not to do

**Don't run Claude Code on the Pi.** A 2B is 1 GB of RAM and 32-bit ARMv7, already shared with
Pi-hole. Claude Code is a Node application that wants substantially more headroom, and 32-bit ARM
is the thinnest part of Node's support matrix. Expect swapping and thrashing if it starts at all.
The reasoning that keeps Grafana and ESPHome off this box
([design.md](design.md#why-not-grafana)) applies here too: the Pi is a data sink.

**Don't expose a command endpoint to the internet.** Tailscale Funnel or a Cloudflare Tunnel could
make an HTTPS service on the Pi reachable from a cloud session, and an MCP server behind it could
accept commands. That is a bad trade: an internet-reachable remote-execution endpoint on the
household DNS server, in exchange for not having to open a laptop.

---

## If you want cloud sessions to reach it anyway

There is a safe shape, and it is pull-based — nothing inbound is exposed, and the Pi decides when
to act. A cloud session pushes to a branch; a timer on the Pi pulls it and deploys:

```sh
cd ~/envlog-src && git pull --ff-only && sudo bash install.sh
```

Point it at a dedicated branch, never `main`, so that landing a commit and deploying it stay
separate decisions. There is no interactive debugging, but for "apply this change and restart" it
works, and the Pi never listens for anything.

---

## One-time setup checklist

On the Pi:

- [ ] An ordinary login account for deploys, separate from the `envlog` service user
- [ ] Laptop's public key in that account's `~/.ssh/authorized_keys`
- [ ] `/etc/sudoers.d/envlog-deploy` written with `visudo`, mode `0440`, LF endings, matching
      posture A or B above
- [ ] Deploy account added to the `systemd-journal` group
- [ ] Static DHCP lease, so the `envlog-ip` fallback keeps working
- [ ] `ENVLOG_BACKUP_DEST` set in `/etc/envlog/backup.conf` — backups matter more once something
      else can deploy

On the laptop:

- [ ] `Host envlog` and `Host envlog-ip` blocks in `~/.ssh/config`
- [ ] `ssh envlog true` succeeds with no password prompt
- [ ] That config lives in the home directory of whichever shell runs Claude Code
- [ ] `.claude/settings.json` allowlist added, understanding it is convenience and not a boundary
