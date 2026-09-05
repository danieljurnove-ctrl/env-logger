# Bring-up checklist

Ordered so that each step is verifiable on its own, and so the riskiest unknowns are settled
before they can waste a Saturday.

Every step has a **Verify** line. If it doesn't pass, stop there — debugging one new variable is
tractable, debugging four stacked ones is not.

---

## Development machine

Everything is built and edited on a **Windows laptop**; files reach the ESP32 over USB and the Pi
over SSH. Two consequences worth knowing before they bite:

- **Line endings.** Shell scripts and systemd units authored on Windows carry CRLF, and the Pi
  will not run them — a CRLF shebang fails with `bad interpreter: /bin/bash^M`, and a CRLF unit
  file fails to parse. The `.gitattributes` in this repo forces LF on checkout, so anything that
  travels through git is safe. Files copied by other means are not; check them.
- **USB serial driver.** The Feather V2's USB-to-serial chip is a **CH9102F** — not the CP2104 of
  the older Huzzah32, so the SiLabs driver is the wrong one. Windows 11 sometimes picks the chip
  up through Windows Update; when it doesn't, the board enumerates as an unknown device and no COM
  port appears. Install WCH's **CH343SER** package (it covers the CH9102) before arrival day.

Copy to the Pi with `scp`, which ships with Windows' built-in OpenSSH client. `rsync` does not.

---

## Step 0 — Confirm ESPHome builds for this board

**Do this before the parts arrive.** No hardware required, about twenty minutes — nearly all of
it spent downloading the ESP-IDF toolchain, which looks like a hang and is not.

**Confirmed passing:** Windows 11, Python 3.13, ESPHome 2026.8.1, ESP-IDF 5.5.5, board
`adafruit_feather_esp32_v2`.

### Install (Windows / PowerShell)

`python3` on Windows is a Microsoft Store alias stub rather than an interpreter. It reports
*"Python was not found"* even when Python is installed and visible in the Start menu. Use the `py`
launcher, which the python.org installer provides and which ignores the stub:

```powershell
py --list
py -3.13 -m venv $HOME\.venvs\esphome
& $HOME\.venvs\esphome\Scripts\pip.exe install esphome
& $HOME\.venvs\esphome\Scripts\esphome.exe version
```

Venv executables live in `Scripts\`, not `bin/`. Invoking the `.exe` paths directly sidesteps
`Activate.ps1` and PowerShell's execution policy.

### Install (macOS / Linux)

```sh
python3 -m venv ~/.venvs/esphome
~/.venvs/esphome/bin/pip install esphome
```

### Compile

[`esphome/minimal.yaml`](../esphome/minimal.yaml) is exactly this test: `esp32:`, `wifi:`,
`logger:`, `api:` and `ota:` on `board: adafruit_feather_esp32_v2`, with no sensors and fake
inline credentials so it compiles without a `secrets.yaml`.

```powershell
& $HOME\.venvs\esphome\Scripts\esphome.exe compile esphome\minimal.yaml
```

**Verify:** the run ends with `Successfully compiled program.` and leaves a `firmware.bin` under
`.esphome\build\minimal-compile-test\`.

### If CMake fails with "Needed a single revision"

A build that gets as far as `Component discovery failed`, with `fatal: Needed a single revision`
and `fatal: not a git repository: (NULL)` in
`.esphome\build\<name>\build\log\idf_py_std*_output_*`, is **not** a board or toolchain
problem. ESP-IDF stamps its own git revision into the build via `__build_get_idf_git_revision`.
The IDF is a downloaded tarball rather than a clone, so CMake walks *up* the directory tree
looking for a `.git` — and an empty repo anywhere above it (a stray `git init` in your home
directory, say) is found, then fails to resolve `HEAD`.

The IDF cache lives under `%LOCALAPPDATA%`, so moving your *project* does not help. Find the
offending repo:

```powershell
$p = "$env:LOCALAPPDATA\esphome\Cache\idf\frameworks\5.5.5"
while ($p) { if (Test-Path "$p\.git") { "FOUND: $p\.git" }; $p = Split-Path $p -Parent }
```

If what it finds has no commits and no remotes (`git -C <path> log --oneline -1` errors,
`git -C <path> remote -v` is empty), rename it away — or just give it one commit, which fixes it
equally well. Then delete the poisoned build tree, since CMake caches its own failure:

```powershell
Remove-Item -Recurse -Force .esphome
```

### Why this step exists

The Pi **cannot** do this: ESPHome requires Python ≥3.12 (Bookworm ships 3.11), ships no armv7
container image, and needs more RAM than a Pi 2B has. If you were counting on building on the Pi,
better to find out now than with a box of sensors on the desk.

---

## Step 1 — Pi: ingest service

Three things to check on the Pi before installing, each of which has already cost an evening once
(details in [deployment.md](deployment.md)):

- **`python3 --version` must be 3.9 or newer.** `app.py` imports `zoneinfo`, and the pinned Flask
  and waitress need 3.9+. If it is older, build a newer interpreter with `make altinstall` —
  never `make install`, which shadows the system `python3` that Pi-hole uses — and install with
  `sudo ENVLOG_PYTHON=python3.11 bash pi/install.sh`.
- **`sudo ss -tlnp | grep :8000` must come back empty.** If something already holds the port,
  `envlog.service` starts and dies with `Address already in use`. Either free the port or set
  `ENVLOG_PORT`.
- **`sqlite3 --version` must be 3.27.0 or newer**, the floor for the `VACUUM INTO` in step 2.

Then install per [pi/README.md](../pi/README.md) — the short version:

```sh
sudo mkdir -p /opt/envlog /var/lib/envlog /etc/envlog
sudo cp pi/app.py pi/schema.sql pi/backup.sh pi/requirements.txt /opt/envlog/
python3 -m venv /opt/envlog/.venv
/opt/envlog/.venv/bin/pip install -r /opt/envlog/requirements.txt
printf 'ENVLOG_TOKEN=%s\n' "$(openssl rand -hex 32)" | sudo tee /etc/envlog/envlog.env
sudo chmod 600 /etc/envlog/envlog.env
```

The schema is applied on first start, so there is no separate init step. Start the service, then
post a fake reading:

```sh
curl -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -H "X-Auth-Token: $ENVLOG_TOKEN" \
  -d '{"node":"feather-01","bme_temp_c":21.4,"co2_ppm":612}'
```

**Verify:** `sqlite3 /var/lib/envlog/envlog.db "SELECT * FROM readings;"` returns the row, with a
sensible `ts` and NULLs in the columns you didn't send.

Also confirm the token is actually enforced — the same request without the header should be
rejected, and so should `GET /` and `GET /api/series`. Auth covers every endpoint, not just
`/ingest`.

With that working, `pi/simulate_node.py` can drive the rest of the Pi-side bring-up without any
hardware: `--backfill-hours 48` writes two days of history for the dashboard to draw, and
`--fast` posts over HTTP exactly as the node will.

---

## Step 2 — Pi: backups

Install the nightly `VACUUM INTO` + rsync job (see [design.md](design.md#backups)) and run it once
by hand.

Set `ENVLOG_BACKUP_DEST` in `/etc/envlog/backup.conf` first. **It is unset by default, and
`backup.sh` warns and exits 0 in that state** — a snapshot that never leaves the SD card is not a
backup, and the SD card is the component most likely to fail. The script uses `rsync` when it is
present and falls back to `scp`, so a Windows target works: Windows' built-in OpenSSH has `scp`
but not `rsync`.

**Verify:** a snapshot file exists on the *other* machine, and
`sqlite3 <snapshot> "SELECT count(*) FROM readings;"` opens it cleanly.

Done now rather than later, because the window where you have data but no backup should be as
short as possible.

---

## Step 3 — Pi-hole: naming

Add a local DNS record pointing `envlog.home` at the Pi's LAN IP, and **make sure the Pi's own
address cannot change** — a reservation on whatever serves DHCP, or a static address on the Pi.
That record is written by hand, so if the Pi's address moves, the name silently points at nothing
and every POST fails.

**The node's address does not need pinning**, despite what this step used to say. Nothing connects
*to* the node: it initiates every POST, and OTA and `esphome logs` reach it by mDNS name
(`feather-01.local`), not by address. A changed lease costs nothing. Pin it only if mDNS is
unreliable on your machine, in which case `use_address:` in the node config is the more direct
fix. Both are in the Pi-hole admin UI; on v6 they live in `/etc/pihole/pihole.toml`
rather than the old `dnsmasq.d` snippets.

**If the Pi has more than one address, this is the step where that matters.** The service binds
`0.0.0.0` and answers on all of them, but the name must point at the address reachable from the
network the *node* joins — on this deployment, the `192.168.50.0/24` side. Pointing it at the
other interface resolves fine and then times out on every POST.

**Verify:** from a *different* machine **on the node's network**, `dig +short envlog.home`
returns the Pi's address on that network, and `curl http://envlog.home:8000/` responds.

An empty `dig` here usually means that subnet's DHCP hands out its own resolver rather than the
Pi, so Pi-hole never sees the query — common when the network belongs to a separate router. Don't
debug it: set `ingest_host` in [`../esphome/env-node.yaml`](../esphome/env-node.yaml) to the Pi's
IP and move on. The static reservation above is what keeps that address from changing.

---

## Step 4 — Node: I²C only

Copy `esphome/secrets.yaml.example` to `esphome/secrets.yaml` and fill it in first — the ingest
token is the one the installer printed in step 1.

Flash [`esphome/i2c-scan.yaml`](../esphome/i2c-scan.yaml) over USB-C with a **data** cable. It is
the GPIO2 power switch and the `i2c:` bus with `scan` on, and **no sensor components at all.**

```powershell
& $HOME\.venvs\esphome\Scripts\esphome.exe run esphome\i2c-scan.yaml
```

**Verify:** the boot log lists **both** `0x62` (SCD-41) and `0x77` (BME280):

```
[C][i2c.idf:113]: Results from bus scan:
[C][i2c.idf:119]: Found device at address 0x62
[C][i2c.idf:119]: Found device at address 0x77
```

Those are `[C]` — **CONFIG** level. ESPHome's levels run `ERROR < WARN < INFO < CONFIG < DEBUG`,
so a `logger: level: INFO` suppresses them entirely and the scan looks like it never ran. Sensor
readings in step 5 are `[D]`, one level further out again. Keep the logger at `DEBUG` until step 7
passes.

This is the step that catches the GPIO2 trap, and it's why nothing else is configured yet. If the
scan finds nothing, the problem is power to the STEMMA QT port, not your sensor configuration —
see [hardware.md](hardware.md#the-i2c-power-trap).

---

## Step 5 — Node: sensors one at a time

Switch to [`esphome/env-node.yaml`](../esphome/env-node.yaml), **cut at the `--- STEP 7` marker**
— delete the `interval:` block at the end and flash the rest. The PM sensor's components stay in
the image and stay silent, because nothing is wired to the UART yet; PM absent from the row is
the designed behaviour, not a fault.

(There is no cut that removes the PM sensor alone: `uart:` is a top-level key and `pmsx003` is a
list item under `sensor:`, so deleting from the STEP 6 marker down would take the two I²C sensors
with it.)

**Verify:** after each addition, plausible values in the log — not zeros, not NaN, and a
temperature within a couple of degrees of what the room actually feels like.

OTA works from here on; no more cable.

---

## Step 6 — Node: PM sensor

Last, because it needs the four jumpers and because it's the one that can be mis-wired
destructively. Double-check **VCC goes to `USB`, not `3V`** before powering on.

**No reflash needed** — the PM components are already in the step 5 image, waiting for something
to appear on the UART. Wire the four jumpers, reboot the node, and it starts publishing.
`update_interval` is already `5min`: strictly greater than 30 s, or the duty cycling silently
doesn't happen.

**Verify:** the fan is audible for about 30 seconds, then stops, then restarts about five minutes
later. Values appear once per cycle, not continuously.

---

## Step 7 — Node: enable posting

Flash `env-node.yaml` entire. The `interval:` block posts to
`http://envlog.home:8000/ingest` with the auth header, carrying only the sensor groups that
produced a reading since the last POST — see
[esphome/README.md](../esphome/README.md#freshness-and-why-has_state-is-not-enough) for why that
is tracked with globals rather than `has_state()`.

**Verify:** rows accumulate in the database with the expected cadence — temperature and CO₂ every
~45 s, PM roughly every 5 minutes with NULLs in between.

Then pull the node's power for a minute and plug it back in. **Verify:** it reconnects on its own
and resumes posting, with `boot_count` incremented.

---

## Step 8 — Pi: make it survive a reboot

Install and enable the systemd units for the ingest service and the backup timer.

**Verify:** `sudo reboot`, then confirm without touching anything that the service is running,
Pi-hole is unaffected, and new rows are still arriving.

---

## Step 9 — Tailscale

Install via the static-binary path (the apt repo has had gaps for 32-bit ARM), then:

```sh
sudo tailscale up --accept-dns=false
```

`--accept-dns=false` matters: MagicDNS otherwise installs itself as the system resolver on this
host and fights Pi-hole for control of the Pi's own DNS. This is a well-documented friction point
between the two.

No subnet router is needed — the service runs on the Tailscale host itself, so its tailnet IP
reaches it directly.

**Verify:** from a phone on cellular data, with WiFi off, `http://<tailnet-ip>:8000/` loads the
dashboard.

---

## Then

Record the first placement so data stops accumulating as "Unknown":

```sh
curl -X POST http://envlog.home:8000/placements \
  -H "X-Auth-Token: $ENVLOG_TOKEN" \
  -d '{"node":"feather-01","room":"living room"}'
```

Leave it running for a few days before trusting absolute CO₂ — and see the ASC caveat in
[hardware.md](hardware.md#scd-41) regarding what room moves do to that calibration.
