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
- **USB serial driver.** Flashing the Feather V2 from Windows needs the SiLabs **CP2104** driver.
  Install it before arrival day — it is a reboot you do not want to discover mid-bring-up.

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

```sh
sudo mkdir -p /var/lib/envlog
python3 -m venv /opt/envlog/.venv
/opt/envlog/.venv/bin/pip install flask waitress
```

Initialise the database from `schema.sql`, then run the service and post a fake reading:

```sh
curl -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -H "X-Auth-Token: $ENVLOG_TOKEN" \
  -d '{"node":"feather-01","bme_temp_c":21.4,"co2_ppm":612}'
```

**Verify:** `sqlite3 /var/lib/envlog/envlog.db "SELECT * FROM readings;"` returns the row, with a
sensible `ts` and NULLs in the columns you didn't send.

Also confirm the token is actually enforced — the same request without the header should be
rejected.

---

## Step 2 — Pi: backups

Install the nightly `VACUUM INTO` + rsync job (see [design.md](design.md#backups)) and run it once
by hand.

Note that the snapshot has to land somewhere that speaks rsync. The Windows dev laptop does not
without extra tooling, so the off-box target needs deciding — another Linux host, a NAS, or
switching this job to `scp`/`sftp`, which Windows does support.

**Verify:** a snapshot file exists on the *other* machine, and
`sqlite3 <snapshot> "SELECT count(*) FROM readings;"` opens it cleanly.

Done now rather than later, because the window where you have data but no backup should be as
short as possible.

---

## Step 3 — Pi-hole: naming

Add a static DHCP reservation for the node's MAC, and a local DNS record pointing `envlog.home`
at the Pi's LAN IP. Both are in the Pi-hole admin UI; on v6 they live in `/etc/pihole/pihole.toml`
rather than the old `dnsmasq.d` snippets.

**Verify:** from a *different* machine on the LAN, `dig +short envlog.home` returns the Pi's
address, and `curl http://envlog.home:8000/` responds.

---

## Step 4 — Node: I²C only

Flash over USB-C with a **data** cable. Config at this point should contain the GPIO2 power switch
and the `i2c:` bus with `scan` on — **and no sensor components at all.**

**Verify:** the boot log lists **both** `0x62` (SCD-41) and `0x77` (BME280).

This is the step that catches the GPIO2 trap, and it's why nothing else is configured yet. If the
scan finds nothing, the problem is power to the STEMMA QT port, not your sensor configuration —
see [hardware.md](hardware.md#the-i2c-power-trap).

---

## Step 5 — Node: sensors one at a time

Add `bme280_i2c`, check the log. Then add `scd4x`, check the log. Wire the SCD-41's pressure
compensation to the BME280's pressure sensor.

**Verify:** after each addition, plausible values in the log — not zeros, not NaN, and a
temperature within a couple of degrees of what the room actually feels like.

OTA works from here on; no more cable.

---

## Step 6 — Node: PM sensor

Last, because it needs the four jumpers and because it's the one that can be mis-wired
destructively. Double-check **VCC goes to `USB`, not `3V`** before powering on.

Set `update_interval: 5min` — strictly greater than 30 s, or the duty cycling silently doesn't
happen.

**Verify:** the fan is audible for about 30 seconds, then stops, then restarts about five minutes
later. Values appear once per cycle, not continuously.

---

## Step 7 — Node: enable posting

Point `http_request` at `http://envlog.home:8000/ingest` with the auth header, posting only fresh
sensor values.

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
