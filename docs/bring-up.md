# Bring-up checklist

Ordered so that each step is verifiable on its own, and so the riskiest unknowns are settled
before they can waste a Saturday.

Every step has a **Verify** line. If it doesn't pass, stop there — debugging one new variable is
tractable, debugging four stacked ones is not.

---

## Step 0 — Confirm ESPHome builds for this board

**Do this before the parts arrive.** No hardware required, about twenty minutes.

On a laptop — x86-64 or Apple Silicon, **Python ≥3.12**:

```sh
python3 -m venv ~/.venvs/esphome
~/.venvs/esphome/bin/pip install esphome
```

Write a minimal config with no sensors — just `esp32:`, `wifi:`, `logger:`, `api:`/`ota:` — using
`board: adafruit_feather_esp32_v2`, and compile it:

```sh
~/.venvs/esphome/bin/esphome compile minimal.yaml
```

**Verify:** the compile finishes and produces a firmware binary.

This step exists because the Pi **cannot** do it: ESPHome requires Python ≥3.12 (Bookworm ships
3.11), ships no armv7 container image, and needs more RAM than a Pi 2B has. If you were counting
on building on the Pi, better to find out now than with a box of sensors on the desk.

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
