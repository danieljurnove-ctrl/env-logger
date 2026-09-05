# Deployment

The box this actually runs on — which is not the box the rest of the docs were written for.

Recorded because the difference is not cosmetic: it changed which Python the service runs on,
which SQLite features are available, and which port it listens on. Verified on the machine
2026-09-01.

---

## The machine

| | As documented | As deployed |
| --- | --- | --- |
| Board | Raspberry Pi 2B | **Pi 4 Model B Rev 1.1** (`c03111`) |
| RAM | 1 GB | **4 GB** |
| OS | Raspberry Pi OS Bookworm | **Raspbian 10 (buster)**, EOL |
| System Python | 3.11 | **3.7.3** |
| SQLite | 3.40 | **3.27.2** |
| Architecture | armv7l | armv7l — unchanged |
| Also running | Pi-hole | Pi-hole **and RetroPie** |
| Hostname | — | `retropie` |

Pi-hole does live here, so [bring-up step 3](bring-up.md#step-3--pi-hole-naming) happens on this
machine as written. Everything else in that table has consequences.

### What the RAM figure changes

Nothing operationally — but the **"no Grafana" argument in [design.md](design.md#why-not-grafana)
rests on a premise that is false here.** 4 GB has ample room for another 150–280 MB of RSS.

The decision stands regardless, on the surviving reasons: 32-bit ARM packaging that will rot, one
less service to keep alive, and a dashboard built around placement-aware segmentation that a
generic tool would need coaxing into. But do not re-derive it from RAM on this box, and do not
use "the Pi is too small" as a reason to reject anything else here.

---

## Python: 3.11 built alongside, system 3.7 untouched

The service does not run on 3.7 — not as a preference, as three hard failures:

- `app.py` imports `zoneinfo`, standard library only from **3.9**.
- `flask==3.1.0` requires 3.9+.
- `waitress==3.0.2` requires 3.9+.

The install otherwise succeeds and the service then dies at first start, which reads as a broken
install rather than a wrong interpreter. `install.sh` now checks the version up front and says so.

**Python 3.11.16 was built from source and installed with `make altinstall`.** That is the
load-bearing detail: `altinstall` creates `/usr/local/bin/python3.11` and deliberately does *not*
create or overwrite `python3`. Since `/usr/local/bin` precedes `/usr/bin` in `PATH`, a plain
`make install` would silently shadow the system interpreter for every script on the box —
including Pi-hole's. `python3` is still 3.7.3 and must stay that way.

```sh
./configure --prefix=/usr/local --with-ensurepip=install
make -j4
sudo make altinstall
sudo ENVLOG_PYTHON=python3.11 bash pi/install.sh
```

`ENVLOG_PYTHON` is needed on that first install only. Afterwards the installer reuses the
interpreter the venv was built on, so updates are `git pull && sudo bash pi/install.sh`.

No `--enable-optimizations`: PGO turns a ~25-minute build into a multi-hour one by running the
test suite for profiling, and buys perhaps 10–20% interpreter speed — meaningless for a service
handling one request every 45 seconds.

The build reports `_dbm`, `_gdbm` and `_tkinter` as missing. None are used. **`_ssl` and
`_sqlite3` both built**, which are the two that would have mattered.

Rebuilding is only needed for a Python security update, and the box does not otherwise depend on
this interpreter — nothing but envlog uses it.

---

## SQLite is 3.27.2, and that is the real ceiling

Buster ships 3.27.2. `VACUUM INTO`, which the nightly backup depends on, landed in **3.27.0** —
so backups work with exactly one patch release to spare.

The rest of the docs say "target 3.40, the version Bookworm ships". **On this box the ceiling is
3.27.2**, which rules out considerably more:

| Feature | Needs |
| --- | --- |
| `STRICT` tables | 3.37 |
| `RETURNING` | 3.35 |
| `->>` operator | 3.38 |
| `unixepoch()` | 3.38 |
| Generated columns | 3.31 |
| `iif()` | 3.32 |

Checked 2026-09-04: **the current code uses none of them.** `ON CONFLICT DO NOTHING` is 3.24, and
there are no window functions anywhere. Nothing is broken — but that is the line not to cross,
and it is lower than `schema.sql` used to claim.

---

## apt: the Buster archive moved

`raspbian.raspberrypi.org` no longer serves Buster — `apt update` fails with *"no longer has a
Release file"*. `/etc/apt/sources.list` is repointed at `http://legacy.raspbian.org/raspbian/`;
the original is at `/etc/apt/sources.list.bak`.

The Raspberry Pi Foundation repo (`archive.raspberrypi.org`) still works and was left alone.

**Buster is end-of-life for security updates.** That is noted, not solved. The service is
LAN-only behind a shared token, so the exposure is bounded, but a box serving DNS for the house
on an unsupported OS is a real thing to eventually deal with — see the re-imaging note at the
bottom.

---

## Port 8000 was already taken

**RetroPie Manager** — a Django `runserver`, launched from `/etc/rc.local` — held `0.0.0.0:8000`,
so `envlog.service` failed at startup with `OSError: [Errno 98] Address already in use`.

envlog keeps port 8000 (every URL in these docs assumes it) and the RetroPie Manager line in
`/etc/rc.local` is commented out with a note saying why. Backup at `/etc/rc.local.bak`; to
restore it, delete the leading `#` and give envlog a different `ENVLOG_PORT`.

This stops **only the web admin UI**. EmulationStation, the emulators and the games are
untouched.

---

## Two networks — decide before step 3

The Pi is dual-homed:

| Interface | Address | |
| --- | --- | --- |
| `eth0` | `10.0.0.254/8` | static, in `/etc/dhcpcd.conf` |
| `wlan0` | `192.168.50.254/24` | static, in `/etc/dhcpcd.conf` |

The router's DHCP pool on the WiFi side is **`192.168.50.100`–`.250`**, so `.254` sits outside it
and cannot be handed to anything else. That is what makes the hand-written `envlog.home` record
safe: the address it points at is pinned at both ends.

Three things in `dhcpcd.conf` are wrong but currently harmless, and will bite whoever edits it
next:

- The `wlan0` block has `static router=` and `static domain_name_server=` — both singular, both
  not real dhcpcd options, both silently ignored. The address itself is correct, which is why WiFi
  works; the route and DNS come from `eth0`.
- That ignored DNS value is `192.168.1.254`, an address on a subnet this network does not have.
- There are **two** `interface eth0` blocks with conflicting settings. The `/8` one wins.

The ingest service binds `0.0.0.0`, so it answers on both. But
[step 3](bring-up.md#step-3--pi-hole-naming) points `envlog.home` at **one** address, and it has
to be the one reachable from whatever network the ESP32's WiFi joins.

**Decided: the node goes on `192.168.50.0/24`.** So `envlog.home` resolves to **`192.168.50.254`**
— the Pi's `wlan0` address, not `eth0`. Pointing it at `10.0.0.254` would resolve fine and then
time out on every POST.

**The open question this leaves is DNS, not routing.** `envlog.home` only resolves if whatever
serves DHCP on `192.168.50.0/24` hands out the Pi as the resolver. If that subnet belongs to a
separate router — a guest or IoT network, say — it will hand out its own DNS instead, Pi-hole
will never see the query, and the name will not resolve no matter what is configured in Pi-hole.

Step 3's verify catches this: `dig +short envlog.home` **from a machine on the 192 network**, not
from the Pi. If it comes back empty, the fix is one line rather than an investigation — set
`ingest_host: 192.168.50.254` in [`esphome/env-node.yaml`](../esphome/env-node.yaml) and skip DNS
altogether. That costs nothing except having to edit the config if the Pi's address ever changes.

**The Pi's address is the one worth pinning** — by reservation or static config. `envlog.home` is
a hand-written record pointing at `192.168.50.254`, so the name breaks silently if that moves. The
node's own address does not matter: it initiates every POST, and OTA reaches it by mDNS name.

(The `/8` on `eth0` is unusual — the Pi treats all of `10.x.x.x` as local — but nothing here
depends on it.)

---

## If this box is ever re-imaged

A fresh Bookworm install would delete every workaround above: system Python becomes 3.11, SQLite
becomes 3.40, apt works normally, and `install.sh` needs no `ENVLOG_PYTHON`. The cost is
RetroPie's setup and Pi-hole's configuration, both of which need backing up first — Pi-hole
exports cleanly via Teleporter, RetroPie does not.

Worth doing eventually for the security updates alone. Not worth doing to make this project
simpler; it already works.
