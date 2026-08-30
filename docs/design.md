# Design

Decisions and the reasoning behind them, so the code phases are execution rather than debate.

---

## Why not Grafana

The original sketch had Grafana on port 3000 with the SQLite datasource plugin. It's out, for
one disqualifying reason and one supporting one:

- **RAM.** Grafana's RSS runs 150–280 MB. The Pi 2B has 1 GB total and already runs Pi-hole,
  whose own footprint scales with blocklist size. There isn't room, and the failure mode is the
  whole box thrashing rather than one service degrading.
- **ARMv7 packaging.** Grafana still builds 32-bit ARM packages but removed them from the
  download page in ~March 2024, so installing means constructing URLs by hand
  ([grafana#92385](https://github.com/grafana/grafana/issues/92385)). Workable, but it will rot.

The ingest service serves its own dashboard instead. One less service, no plugin architecture
risk, and the dashboard can be built around this data specifically — including the
placement-aware series segmentation that a generic tool would need coaxing to do.

---

## Ingest service stack

**Flask + waitress, in a venv.**

This is a synchronous, single-writer service handling single-digit requests per minute. FastAPI's
async machinery solves problems this workload does not have, and adds a dependency tree that has
to be built for ARMv7.

A secondary argument sometimes made — that `uvloop` and `httptools` publish no `armv7l` wheels —
is true on PyPI but shouldn't be leaned on: Raspberry Pi OS configures piwheels by default, which
may well serve prebuilt ARM wheels. The workload argument is the durable one.

Raspberry Pi OS Bookworm marks its system Python externally managed (PEP 668), so a venv is
required. Do not reach for `--break-system-packages` on a service meant to run for years.

**Target SQLite 3.40**, the version Bookworm ships. A development sandbox may have something much
newer — don't reach for `STRICT` tables or recent JSON functions that pass locally and fail on
the Pi.

---

## Schema

```sql
CREATE TABLE nodes (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE          -- device identity: 'feather-01', never a room name
);

CREATE TABLE rooms (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE          -- preset list, editable
);

CREATE TABLE placements (
  id       INTEGER PRIMARY KEY,
  node_id  INTEGER NOT NULL REFERENCES nodes(id),
  room_id  INTEGER NOT NULL REFERENCES rooms(id),
  start_ts INTEGER NOT NULL,
  end_ts   INTEGER,                  -- NULL = it is there now
  note     TEXT                      -- 'on the windowsill'
);
CREATE INDEX idx_placements_lookup ON placements(node_id, start_ts);

CREATE TABLE readings (
  node_id      INTEGER NOT NULL REFERENCES nodes(id),
  ts           INTEGER NOT NULL,     -- unix epoch SECONDS, UTC, server-assigned
  bme_temp_c   REAL,
  bme_rh_pct   REAL,
  pressure_hpa REAL,
  scd_temp_c   REAL,
  scd_rh_pct   REAL,
  co2_ppm      REAL,
  pm1_0_atm    REAL,
  pm2_5_atm    REAL,
  pm10_atm     REAL,
  boot_count   INTEGER,              -- move detection only
  PRIMARY KEY (node_id, ts)
);
```

**Both the BME280 and the SCD-41 report temperature and humidity**, so the columns are named per
sensor. A single `temp_c` column would silently discard one of them — and the SCD-41's own
temperature reading is exactly what you need in order to tune its `temperature_offset` later.

Wide rather than narrow (`ts, metric, value`): the sensor set is small and fixed, wide reads
faster, and `ALTER TABLE ... ADD COLUMN` is an O(1) metadata-only operation in SQLite, so adding
a metric later is cheap anyway.

Insert with **`ON CONFLICT DO NOTHING`** — a client retry, or the Pi's clock stepping backwards
after an NTP sync, would otherwise raise on the primary key.

No `WITHOUT ROWID`. It would save a redundant index, but at this size that's unmeasurable and it
forecloses rowid-based tooling for nothing.

### Size

About 100 bytes per row. At a 45-second cadence that's roughly **70 MB/year**, or ~350 MB over
five years for one node. Small enough that keeping everything at full resolution forever is the
simplest correct answer.

### What we deliberately don't do

An earlier draft specified a 90-day retention window, an hourly rollup table, a scheduler to
populate it, incremental auto-vacuum, and dashboard logic to switch between raw and rolled-up
data by time range. All of it is gone.

The sizing above is why: 350 MB over five years doesn't need managing. The rollup existed to make
queries fast that were never going to be slow, and it introduced a genuinely dangerous failure
mode — with raw data deleted at 90 days and the rollup as the only permanent record, a rollup job
that silently breaks destroys history irrecoverably.

Downsampling for long views is a `GROUP BY ts / bucket * bucket` at query time.

---

## Timestamps

**The server assigns `ts` on arrival. There is no batch endpoint.**

The ESP32 has no RTC, and LAN round-trip latency is milliseconds against a 45-second sample
interval, so arrival time is accurate enough.

This does mean a WiFi or Pi-hole outage loses those readings rather than buffering them for later
replay. That's an accepted trade — see Limitations in the README. It's worth stating plainly
because the two halves are easy to specify inconsistently: *if* the server stamps on arrival,
then a replayed backlog of 200 readings arrives with 200 identical timestamps, which against
`PRIMARY KEY (node_id, ts)` is one insert and 199 constraint violations. Server-assigned
timestamps and backlog replay cannot both exist. We chose the simpler one.

The node still reports `boot_count`, not for timing but because it drives move detection below.

---

## SQLite pragmas

Some pragmas are properties of the database file; others reset on every connection. Mixing them
up gives you a service that silently runs with none of its intended settings.

| Where | Pragma | Note |
| --- | --- | --- |
| `schema.sql`, once | `journal_mode=WAL` | Persistent property of the file |
| Connection factory, **every** connection | `synchronous=NORMAL` | Otherwise defaults to `FULL` |
| | `busy_timeout=5000` | Otherwise no timeout at all |
| | `foreign_keys=ON` | **Defaults to OFF** |

WAL is the right mode for one writer and an occasional reader — readers never block the writer.

**Durability, stated honestly:** `synchronous=NORMAL` under WAL is corruption-safe. What it costs
is that on power loss you lose every committed transaction since the last checkpoint — not merely
the last one. At roughly one write per minute, checkpoints can be a long way apart, so the real
exposure is closer to hours. Combined with the in-memory buffer below, an abrupt power cut can
cost a meaningful stretch of readings.

That's acceptable here, and the mitigation if it ever stops being acceptable is an explicit
`PRAGMA wal_checkpoint(PASSIVE)` after each flush, which is nearly free at this write rate.

### Write batching

Buffer readings in memory and flush once a minute inside a single transaction. One fsync a minute
rather than one per reading is the largest single lever on SD-card wear. The cost is up to a
minute of readings lost on an unclean shutdown.

---

## Backups

The stated purpose is multi-year trends. The storage medium is an SD card in a Pi — the most
failure-prone component in the system, with a wear-out mode that the batching above exists to
slow down. Accepting gaps in the record is not the same as accepting the loss of all of it.

Nightly:

```sh
sqlite3 /var/lib/envlog/envlog.db "VACUUM INTO '/tmp/envlog-$(date +%F).db'"
# then rsync/scp to another machine on the tailnet
```

`VACUUM INTO` produces a consistent snapshot of a live WAL database. A plain `cp` of the `.db`
file does not, and can yield a corrupt copy.

Rotate 7 daily and 4 weekly. At these sizes that's a few MB a night. This ships with the ingest
service, not "later".

---

## Location tracking

The sensor is portable, so location is a property of *time*, not of the device. The node
identifies the hardware (`feather-01`); `placements` records where it was and when.

Readings themselves stay location-free. Location resolves at query time:

```sql
SELECT r.ts, COALESCE(m.name, 'Unknown') AS location, r.co2_ppm
FROM readings r
LEFT JOIN placements p
  ON  p.node_id = r.node_id
  AND r.ts >= p.start_ts
  AND (p.end_ts IS NULL OR r.ts < p.end_ts)
LEFT JOIN rooms m ON m.id = p.room_id;
```

This is what makes retroactive labelling cheap. Correcting history is editing one `placements`
row; renaming a room is editing one `rooms` row, and every chart in the archive relabels itself.
Had location been stamped onto each reading, both would be mass `UPDATE`s over thousands of rows.

The `LEFT JOIN` is deliberate: unlabelled periods survive as "Unknown" rather than silently
vanishing from results.

**Invariant the application must enforce:** placements for a node must not overlap. SQLite can't
express this declaratively, so the move endpoint closes the open placement before opening the
next, and the test suite carries an overlap check.

### Interface

A "Move to …" control on the dashboard, picking from the `rooms` list with an option to add a new
one, plus an editable list of past placements for corrections. A preset list rather than free
text keeps `bedroom` and `Bedroom` from splitting a chart in two.

`POST /placements` and `PATCH /placements/:id` ship with the ingest service in phase 3, ahead of
the dashboard, so nothing is collected unlabelled while the UI is still being built. Until then a
single `curl` records a move.

### Move detection

Moving a USB-powered node means unplugging it, so **every move produces a reboot and a gap**. Not
every reboot is a move — power blips and OTA updates also reboot — but the implication runs one
way reliably, which is enough to be useful.

So the dashboard can surface "gap plus `boot_count` increment at 14:32 — was this a move?" and
offer to label it after the fact. Carrying the node on a power bank defeats this; nothing else
does.

### Charting consequences

- **A move breaks the line.** Series segment at placement boundaries. Drawing a trend across a
  move from the bedroom to the kitchen renders a slope that never happened.
- The same property enables the genuinely useful view: one sensor, one calibration, rooms
  compared against each other without cross-sensor error.
- **Flag the first few minutes of a placement as settling** — the BME280 needs to thermalise and
  the PM fan needs to spin up.

---

## Node → server contract

**Each POST carries only the sensors that produced a fresh reading.** Omitted fields become NULL.

This matters because the sensors run at different cadences: with PM duty-cycled to 5 minutes and
POSTs going out every 45 seconds, carrying the last PM value forward would make roughly 85% of
the PM series fabricated repeats. The schema already renders NULL as a gap, so honesty here costs
nothing.

Auth is a shared secret in an `X-Auth-Token` header.

### Data hygiene

- Enforce `pm1_0 ≤ pm2_5 ≤ pm10`; reject implausible values.
- A failed sensor NULLs its own columns — never drop the whole row. If the SCD-41 times out on
  I²C while the BME280 responds, that reading is still worth keeping.
- Render NULL as a gap, not as a line connected across it.

---

## Liveness, timezones, auth

**Liveness.** The dashboard header shows `last seen: N minutes ago`, red past a threshold.
Roughly ten lines of code, and without it a dead node is discovered whenever you next happen to
open the page — which for a portable sensor that's often unplugged is a real risk.

**Timezones.** Store UTC. The API takes an explicit IANA timezone and does local-day grouping at
query time. "Overnight CO₂" and "this week versus last" are local-time questions, and two days a
year the local day is 23 or 25 hours long.

**Auth covers every endpoint**, including `GET /` and `/api/series` — not just `/ingest`. A
tailnet is a flat network: every device and user on it can otherwise read the dashboard. Also set
`MAX_CONTENT_LENGTH` and compare tokens with `hmac.compare_digest`.

**Don't add TLS.** Anyone positioned to sniff the token on the LAN can already inject readings,
and the asset is household CO₂ numbers. The cleartext-token objection is theatre here; the body
size cap is not.

---

## Dashboard

One self-contained HTML page reading `GET /api/series`, in the same spirit as a single-file app:
no build step, no framework.

Vendor [uPlot](https://github.com/leeoniya/uPlot) (~40 KB, built for exactly this shape of data)
rather than hand-rolling canvas or pulling a CDN dependency onto a box that may have no route to
the internet.
