# env-logger

Home air quality and environmental logging. A portable WiFi sensor node carried from room to
room posts readings to an always-on Raspberry Pi, which stores them in SQLite and serves trend
graphs viewable from anywhere over Tailscale.

**The goal is short-term comparison between rooms** — is the bedroom stuffier than the office by
bedtime, does the kitchen recover after cooking — not a long-term archive. That is why there are
no backups configured, why absolute CO₂ accuracy matters less than the relative trend, and why
correct room attribution matters more than either. See
[docs/design.md](docs/design.md#purpose).

> **Status: hardware in hand, bring-up in progress.** The Pi service and the node firmware are
> both written and pass their checks off-hardware; nothing has been verified against real sensors
> yet. [docs/bring-up.md](docs/bring-up.md) is the ordered checklist for doing that.

---

## Architecture

```
[ ESP32 Feather V2 + BME280 + SCD-41 + PMS5003 ]   portable, USB-C powered
     │  HTTP POST JSON, X-Auth-Token header
     │  only sensors with a fresh reading; the rest are omitted → NULL
     │  target: http://envlog.home:8000/ingest
     ▼
[ Raspberry Pi — fixed, always on, running Pi-hole ]
     ├─ Pi-hole            :53 + its own web UI     (pre-existing, untouched)
     ├─ envlog ingest      :8000  Flask + waitress
     │     ├─ POST  /ingest          auth → validate → buffer → batched write
     │     ├─ POST  /placements      "I moved it to the bedroom"
     │     ├─ PATCH /placements/:id  retroactive correction
     │     ├─ GET   /markers         annotations: "opened windows"
     │     ├─ POST  /markers         placed by clicking a chart
     │     ├─ GET   /api/series      JSON for the dashboard
     │     └─ GET   /                self-contained HTML dashboard
     ├─ SQLite  /var/lib/envlog/envlog.db   (WAL, batched ~1 write/min)
     ├─ nightly VACUUM INTO + rsync off-box
     └─ Tailscale          remote access via the Pi's tailnet IP
```

No Grafana. The ingest service serves its own dashboard page instead — one less service, no
plugin architecture to rot, and a dashboard that can be built around placement-aware
segmentation. (The original reason was RAM on a 1 GB Pi 2B. The box this actually runs on has
4 GB, so that particular argument no longer applies; the others still do. See
[docs/deployment.md](docs/deployment.md#what-the-ram-figure-changes).)

**Firmware is built on a laptop, not on the Pi.** ESPHome requires Python ≥3.12, publishes no
armv7 container image, and wants more RAM than is comfortable here. The Pi is a data sink only.

**The deployed box is not the one described above.** It is a Pi 4 on Raspbian Buster that also
runs RetroPie, with Python 3.7 as its system interpreter and SQLite 3.27.2. What that changes —
and what had to be done to it — is in **[docs/deployment.md](docs/deployment.md)**.

Development is on **Windows**, with files reaching the ESP32 over USB and the Pi over SSH. See
[the development machine notes](docs/bring-up.md#development-machine) for the line-ending and
USB-driver consequences of that.

---

## Hardware

| Part | Adafruit PID | Interface |
| --- | --- | --- |
| ESP32 Feather V2, pre-soldered headers, 8 MB flash + 2 MB PSRAM | 5900 | — |
| BME280 temperature / humidity / pressure | 2652 | I²C @ 0x77 |
| SCD-41 true NDIR CO₂ (+ temp/RH) | 5190 | I²C @ 0x62 |
| PMS5003 particulate sensor + breadboard adapter | 3686 | UART, 9600 baud |
| STEMMA QT cable, 100 mm (×2) | 4210 | — |
| Female/female jumper wires | 1950 | — |

No soldering required. Full wiring, pinouts and per-sensor gotchas: [docs/hardware.md](docs/hardware.md).

Two things that are easy to get wrong and expensive to debug — both covered in detail in the
hardware doc:

- **The PMS5003 needs 5 V on VCC**, from the Feather's `USB` pin, not the `3V` pin. Its logic is
  3.3 V, so no level shifter is needed.
- **GPIO2 must be driven HIGH** or no I²C sensor will be detected. The STEMMA QT port runs off
  its own regulator gated by that pin.

---

## Repo layout

| Path | Contents |
| --- | --- |
| `docs/hardware.md` | Bill of materials, pinouts, wiring, sensor gotchas |
| `docs/design.md` | Schema, storage decisions, API contract, and why |
| `docs/bring-up.md` | Ordered checklist for the day the parts arrive |
| `docs/deployment.md` | The box this actually runs on, and how it differs from the docs |
| `esphome/` | Node firmware config — `env-node.yaml`, plus a scan-only config for step 4 |
| `pi/` | Ingest service, schema, dashboard, systemd units |

---

## What it records

Temperature, relative humidity, and barometric pressure (BME280); CO₂, plus a second
independent temperature and humidity (SCD-41); PM1.0, PM2.5 and PM10 mass, plus particle counts
at 0.3, 0.5, 1.0, 2.5, 5.0 and 10 µm (PMS5003).

The counts matter more than they look. Mass is derived from them and reported as an integer, so a
clean room reads 0 µg/m³ on every size for hours while the counts move over hundreds.

Because the node is portable, **location is tracked as a time interval, not as a property of the
node**. The node identifies the device (`feather-01`); a `placements` table records which room
it was in and when. Moving it is two writes and no firmware change, and mislabelled history is
corrected by editing one row rather than rewriting thousands. See
[docs/design.md](docs/design.md#location-tracking).

**Markers** record what you did. Click any chart at the moment something
happened — "opened windows", "left house", "cooked with fan on" — and the
annotation appears on all six charts at once. A CO₂ decay curve is only a
ventilation measurement once you know a window opened, and a PM spike is only a
cooking test once you know the hood was on.

---

## What you can learn from it

The point isn't the graphs, it's the questions they settle — and per
[Purpose](docs/design.md#purpose), those are comparisons: this room versus that one, this week
versus last, before versus after you changed something.

**CO₂ is a ventilation meter.** Humans are the only significant indoor source, so ppm tracks how
much of your own exhaled air you're rebreathing. Outdoor is ~420; under 800 is well ventilated,
1000–1500 is stuffy, and a closed bedroom with two people in it will often reach 2000–3000 by
morning. That's a testable hypothesis about why you wake up groggy: door shut, door open, two
nights, compare. Relative trend is all this needs, which is why the SCD-41's shaky absolute
calibration doesn't matter here.

The decay curve after everyone leaves a room gives you **air changes per hour** — a genuine
quantitative ventilation figure for that room, from the exponential fit back toward outdoor
baseline.

**Particulates are mostly a kitchen story.** Cooking dominates indoor PM; searing or frying can
push PM2.5 into the hundreds of µg/m³ against a WHO 24-hour guideline of 15. What this settles:
whether your range hood actually works, whether opening a window beats it (usually yes), and
whether an air purifier earns its filter cost — measure the clearance rate with and without.

For the quieter question of "is this room dustier than that one", watch the **counts**, not the
mass. Mass is derived and rounded to an integer, so a clean room reads 0 µg/m³ for hours while
the 0.3 µm channel moves over hundreds.

**Temperature and humidity are where portability pays off.** One calibrated sensor moved between
rooms beats several uncalibrated ones, because the difference you measure is real rather than
instrument spread. That gives you a room-by-room thermal survey, including how fast each room
loses heat overnight — a crude but real insulation ranking.

Watch dew point rather than RH. It's computed on read from the same two values, and unlike RH
it's absolute: if a window or wall sits below it you get condensation, which is what actually
grows mould.

**Pressure** is mainly a weather trend line. Its real job here is feeding ambient pressure
compensation to the SCD-41 so the CO₂ figures are correct.

### What it does not measure

- **Carbon monoxide.** CO and CO₂ are different molecules and the SCD-41 sees only CO₂. **This is
  not a CO detector and will not warn you about a faulty furnace or a blocked flue.** Keep a
  certified CO alarm; nothing here substitutes for one.
- **VOCs** — paint, solvents, cleaning products, off-gassing. Needs a separate sensor; see
  [possible upgrades](docs/hardware.md#possible-upgrades).
- **Radon**, formaldehyde, and anything else chemically specific.
- Optical PM sensors also drift with age and can't tell cooking smoke from pollen.

---

## Roadmap

Ordered by risk, not by ease.

1. ~~Confirm ESPHome compiles for this board on a laptop~~ — **done 2026-08-30**: Windows 11,
   Python 3.13, ESPHome 2026.8.1, ESP-IDF 5.5.5. See
   [bring-up step 0](docs/bring-up.md#step-0--confirm-esphome-builds-for-this-board).
2. ~~Docs and skeleton~~
3. ~~Pi ingest service~~ — schema, placements endpoints, backup job, systemd units,
   fake-node simulator. See [pi/README.md](pi/README.md).
4. ~~Dashboard~~ — move control, liveness indicator, per-placement series segmentation
   *(built out of order: it needed no hardware either, and the simulator gave it real data)*
5. ~~ESPHome node config~~ — GPIO2 power switch, three sensors, freshness-gated POST. Validates
   and compiles; see [esphome/README.md](esphome/README.md). **Not yet verified against real
   sensors** — that is bring-up steps 4–7, and it is where you are now.
6. Tailscale — independent of everything above

---

## Limitations

Stated up front, because most of these are deliberate.

- **Gaps in the record are accepted.** Timestamps are assigned by the server on arrival, and
  there is no batch endpoint, so a WiFi or Pi-hole outage loses those readings rather than
  buffering them. This was chosen for simplicity.
- **Power-loss window.** Readings are buffered in memory for up to a minute, and SQLite runs
  `synchronous=NORMAL`, which under WAL can lose every transaction since the last checkpoint.
  Corruption-safe, but the exposure on an abrupt power cut is realistically hours, not seconds.
- **The PM sensor can't run on battery.** It needs 5 V from the USB pin. A LiPo would give you a
  temperature/humidity/pressure/CO₂ node only, unless you add a boost converter.
- **PM data is sparser than everything else.** The laser and fan are rated for roughly
  6000–8000 hours, so the sensor is duty-cycled to about 10%. PM arrives every ~5 minutes
  against ~45 seconds for the other metrics.
- **Absolute CO₂ may not be trustworthy on a portable node.** The SCD-41's automatic
  self-calibration needs days of continuous running, and every room move power-cycles it. See
  the hardware doc. Relative trends are unaffected, which is what the questions above actually
  need — so this is a caveat rather than a problem here.
- **No backups.** Deliberate: see [Purpose](docs/design.md#purpose). The nightly job ships and
  is one config line away if that ever changes.
- **Pi-hole is a dependency for ingest.** The node resolves `envlog.home` through it, so a
  Pi-hole outage stops data collection. Setting `ingest_host` to the Pi's IP in the node config
  removes the dependency entirely, at the cost of having to edit it if that address changes.
- **The Pi runs an unsupported OS.** Raspbian Buster stopped getting security updates in 2024,
  and its package archive has already moved once. Bounded — the service is LAN-only behind a
  shared token — but see [docs/deployment.md](docs/deployment.md#if-this-box-is-ever-re-imaged).
- **The outdoor reference is a model, not a sensor.** Open-Meteo serves a numerical forecast on a
  grid of several kilometres, so the dashed lines are the trend over your neighbourhood, not your
  street. When one disagrees with a thermometer outside your window, the thermometer is right.
  It is off until you set coordinates, and there is no outdoor CO₂ in it — the air-quality model
  carries carbon *monoxide*, a different gas.
- **Single node.** The schema supports several, but nothing has been tested with more than one.
