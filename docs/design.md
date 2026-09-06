# Design

Decisions and the reasoning behind them, so the code phases are execution rather than debate.

---

## Purpose

**Short-term comparison between rooms in one house.** Is the bedroom stuffier than the office by
bedtime; does the kitchen recover after cooking; is this room worse than that one this week.

It is not a long-term archive. That distinction is load-bearing in more places than it looks:

- **Backups are not configured**, and that is a decision rather than an omission. Losing a few
  weeks of stale room comparisons costs nothing worth defending against.
- **Retention needs no thought.** Full resolution forever is simplest and the data is small; it
  just isn't a feature anyone is relying on.
- **Absolute CO₂ accuracy barely matters.** The SCD-41's self-calibration may never converge on a
  node that gets unplugged for every room move (see
  [hardware.md](hardware.md#scd-41)) — and for every question above, the relative trend is the
  answer. This turns the project's most uncertain hardware question into a non-issue.

What *does* matter, given that purpose: correct room attribution (a trend drawn across a move is
a lie), honest gaps rather than fabricated continuity, and a dashboard that segments by placement.
All three are what the rest of this document is about.

---

## Why not Grafana

The original sketch had Grafana on port 3000 with the SQLite datasource plugin. It's out, for
one disqualifying reason and one supporting one:

- **RAM.** Grafana's RSS runs 150–280 MB. The Pi 2B has 1 GB total and already runs Pi-hole,
  whose own footprint scales with blocklist size. There isn't room, and the failure mode is the
  whole box thrashing rather than one service degrading.

  **This premise turned out to be wrong.** The box this was deployed on is a 4 GB Pi 4, where
  Grafana would fit comfortably. The decision stands on the packaging argument below and on the
  placement-aware dashboard being worth owning — but this bullet is no longer a reason, and
  should not be recycled as one for rejecting anything else. See
  [deployment.md](deployment.md#what-the-ram-figure-changes).
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

**The venv also has to be built on a Python new enough to run this.** `app.py` imports `zoneinfo`
and the pinned Flask and waitress all require **3.9+**; an older Raspberry Pi OS ships 3.7, where
the install succeeds and the service then dies at import. `install.sh` checks this up front and
takes `ENVLOG_PYTHON` to point at a newer interpreter — which is how the deployed box runs, on a
3.11 built alongside its system 3.7. See [deployment.md](deployment.md#python-311-built-alongside-system-37-untouched).

**Target SQLite 3.27.2**, the version the deployed box ships — not the 3.40 of Bookworm this was
originally written against. A development sandbox will have something much newer, so the ceiling
is easy to breach by accident: no `STRICT` tables (3.37), no `RETURNING` (3.35), no `->>` (3.38),
no `unixepoch()` (3.38), no generated columns (3.31).

`VACUUM INTO`, which the nightly backup depends on, arrived in 3.27.0 — one patch release below
what is installed. Everything else in use is comfortably older: `ON CONFLICT DO NOTHING` is 3.24,
and the one window function (`LAG`, in candidate-move detection) is 3.25. See
[deployment.md](deployment.md#sqlite-is-3272-and-that-is-the-real-ceiling).

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
  pm0_3_count  REAL,                 -- particles per 0.1 L, cumulative by size
  pm0_5_count  REAL,
  pm1_0_count  REAL,
  pm2_5_count  REAL,
  pm5_0_count  REAL,
  pm10_count   REAL,
  boot_count   INTEGER,              -- move detection only
  PRIMARY KEY (node_id, ts)
);
```

**Particle counts are stored as well as mass**, all six channels the sensor reports. Mass is
derived from these and rounded to an integer, so indoors it reads 0 µg/m³ for hours while the
counts move over hundreds — the counts are what distinguish one room from another at household
concentrations. The CF=1 "standard particle" mass set is deliberately not stored: it is the same
measurement under a different calibration curve, identical to the atmospheric set except at
concentrations this project will never see indoors.

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

About 100 bytes per row. At a 45-second cadence that's roughly **70 MB/year** for one node. Even
left running for years unattended it stays small enough that keeping everything at full
resolution is the simplest correct answer — and against the actual usage in
[Purpose](#purpose), the question never comes up at all.

### What we deliberately don't do

An earlier draft specified a 90-day retention window, an hourly rollup table, a scheduler to
populate it, incremental auto-vacuum, and dashboard logic to switch between raw and rolled-up
data by time range. All of it is gone.

The sizing above is why: a few hundred MB over several years doesn't need managing. The rollup
existed to make
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

**Not configured on this deployment, deliberately** — see [Purpose](#purpose). Backing up a
rolling few weeks of room comparisons is not worth a scheduled job and somewhere to put it, and
`ENVLOG_BACKUP_DEST` being unset means `backup.sh` warns and exits 0 rather than pretending.

The machinery below ships anyway, because the moment the archive *is* worth something the SD card
is the most failure-prone component in the system, and the batching above exists to slow down a
wear-out mode that still ends in the card dying. Setting one line in `/etc/envlog/backup.conf`
turns it on; nothing needs reinstalling.

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
- **An outage breaks the line too.** Readings stop whenever WiFi or Pi-hole does, and those gaps
  are accepted — but drawing a straight line across one asserts the room did nothing in between,
  which is the same lie as drawing a slope across a move.
- **A slow sensor is not an outage.** A bucket exists if *any* metric landed in it, so PM — five
  minutes against a 45-second cadence — is NULL in most rows. Treating those as gaps isolates
  every PM sample between two breaks and the series can only ever draw dots. So each chart is
  first compacted to the rows where its own metrics have data, and only then judged for outages,
  against the coarser of the bucket and that metric's own observed spacing. The spacing estimate
  is a lower quartile rather than a mean, because outages only add large deltas and would
  otherwise inflate the very threshold meant to catch them.

---

## Node → server contract

**Each POST carries only the sensors that produced a fresh reading.** Omitted fields become NULL.

This matters because the sensors run at different cadences: with PM duty-cycled to 5 minutes and
POSTs going out every 45 seconds, carrying the last PM value forward would make roughly 85% of
the PM series fabricated repeats. The schema already renders NULL as a gap, so honesty here costs
nothing.

Auth is a shared secret in an `X-Auth-Token` header.

### Data hygiene

- Enforce `pm1_0 ≤ pm2_5 ≤ pm10` on mass, and the reverse on counts: they are cumulative
  "at least this size", so `pm0_3 ≥ pm0_5 ≥ … ≥ pm10`. Either ordering violated means the frame
  is untrustworthy, so that whole set is dropped — not the row.
- Reject implausible values.
- A failed sensor NULLs its own columns — never drop the whole row. If the SCD-41 times out on
  I²C while the BME280 responds, that reading is still worth keeping.
- Render NULL as a gap, not as a line connected across it.

---

## Liveness, timezones, auth

**Liveness.** The dashboard header shows `last seen: N minutes ago`, red past a threshold.
Roughly ten lines of code, and without it a dead node is discovered whenever you next happen to
open the page — which for a portable sensor that's often unplugged is a real risk.

**Units.** Store Celsius, display Fahrenheit. The sensors report Celsius, the API returns it, and
the archive holds it; the dashboard converts when it draws. Keeping the conversion at the display
edge means changing your mind is a refresh rather than a migration of every historical row — and
the same argument as the BME280 offset: don't bake a presentation decision into stored data.

**Dew point** is derived on read too, for the same reason, and needs no column: it is a pure
function of temperature and RH, both already stored. Computed from BME280 temperature and
humidity via the Magnus approximation (valid roughly −45 to 60 °C):

```
γ  = ln(RH/100) + (17.62 · T) / (243.12 + T)
Td = (243.12 · γ) / (17.62 − γ)
```

Deriving it in Python rather than SQL is not optional here: `ln()` is a SQLite math function,
those arrived in **3.35**, and the deployed box has **3.27.2**. Apply the BME280 offset first,
then compute — otherwise a retuned offset silently stops matching the dew point beside it.

Worth surfacing because dew point answers the humidity question RH can't. RH is relative to
temperature, so 60% in a warm room and 60% in a cold one mean very different things; dew point is
absolute. If a window or wall surface sits below it you get condensation, and sustained
condensation is what grows mould.

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

**It has to work in portrait on a phone**, because that is where it gets read once Tailscale is
up — standing in the room you are asking about. The layout constraint that matters: the panels
live in a CSS grid whose track is `minmax(0, 1fr)`, not the implicit `auto`. An `auto` track takes
the min-content width of its widest item, so one unshrinkable child (the placement table, with its
date inputs) widens every panel and pushes the page past the viewport — and the charts, which size
themselves from their container, then grow to match and overflow visibly.

Vendor [uPlot](https://github.com/leeoniya/uPlot) (~40 KB, built for exactly this shape of data)
rather than hand-rolling canvas or pulling a CDN dependency onto a box that may have no route to
the internet.

### Markers

Placements record *where* the node was. Markers record *what you did*: "opened windows", "left
house", "cooked with fan on". Without them the charts show that something changed and never why,
and [Purpose](#purpose) is entirely about before-and-after — a CO₂ decay curve is a ventilation
measurement only once a window is known to have opened.

**Points, not intervals.** An event with a duration is two markers. This keeps the schema to three
columns and the interaction to a single click; `ALTER TABLE ... ADD COLUMN` is O(1) metadata-only
in SQLite if that ever needs revisiting.

**Not scoped to a node.** Every marker draws on every chart, which is what "left house" wants, and
which room the node was in at that instant is already answered by placements.

**Arming is deliberate.** uPlot already binds drag-on-a-chart to zoom, and both a zoom and a click
end in a `click` event — so placing a marker has to be an explicit mode, and a gesture that moved
more than three pixels between mousedown and mouseup is treated as a zoom and places nothing.
Without that guard every zoom would drop a marker where the drag ended.

**Rendering reuses the move-boundary plugin pattern**, and is registered in `makeChart` rather
than per chart, which is the whole of "one marker shows on all six". Solid magenta where a move is
dashed grey, and labelled where a move is not — a move is already named by its band in the
timeline, whereas an annotation has nowhere else to say what it was. The draw hook is handed every
marker and drops what falls outside the plot, so no range filtering is needed anywhere.

**Labels autocomplete from the full history** rather than a preset table like `rooms`. Markers are
too varied to enumerate up front, but repeat often enough that typing "opened windows" twice
should not produce two spellings. `GET /markers` is unbounded for the same reason — these are
hand-typed events, a few a day at most, so the whole history is a small payload.

### Candidate moves

`boot_count` arrives on every reading and, until now, was read by nothing.

A move means unplugging the node, carrying it and plugging it back in, so **every
move is a reboot with a gap**. The converse does not hold — an OTA update or a
power blip reboots without going anywhere — which is why these are offered as
prompts rather than turned into placements automatically. A candidate is a
`boot_count` increment with at least 120 s of silence before it, and one whose
gap already contains a placement start is dropped: nagging about a move you
already recorded is how a useful prompt becomes noise.

Accepting one opens the placement at *the reboot*, not at now, because the whole
point is that the readings since then are already mislabelled.

### Export

`GET /api/export` streams raw rows as CSV with the room resolved per reading via
the same range join the charts use — an export without it loses the one dimension
the project is organised around.

Streamed rather than assembled, and on **its own connection**: a Flask generator
is consumed after the request context has torn down, so reusing the
request-scoped connection raises `Cannot operate on a closed database` at the
first row. The tests caught exactly that.

It exists so that a question this dashboard does not answer can be answered in a
spreadsheet, instead of every such question becoming a feature request.

### Comparison view

The dashboard's other half. A time series shows that something *changed*; only a comparison
answers "did the hood help", which is what [Purpose](#purpose) says the project is for.

Two windows, picked from the placement list and then adjustable. Both are re-based to
**seconds elapsed since their own start** — that is what lets two different stretches of clock
time overlay on one axis at all.

Stats come from `/api/compare` over **raw rows**, not from the bucketed chart data. A p95 of
bucket averages is not a p95, and the tail is exactly where PM questions live. The response also
reports which rooms each window covers, because a window that straddles a move is comparing a
room to itself — with a 60-second minimum overlap, since the `datetime-local` inputs have minute
precision and a sub-minute spill into the neighbouring placement is not a straddle.

### Decay fits

`/api/decay` fits `ln(value − baseline)` against elapsed time by least squares. The slope is the
decay constant: on CO₂ after a room empties that is **air changes per hour**, a real ventilation
measurement; on PM after cooking it is how fast the room clears.

**The baseline is the trap, and r² will not save you.** CO₂ decays toward the outdoor floor
(~420 ppm), not toward zero. Fitting against zero understates the rate by about 20% — and r²
stays above 0.99 while it does, because r² measures whether the curve is exponential, *not*
whether the baseline you subtracted was the right one. So the default is 420 for CO₂ and 0 for
particulates, the answer always states which baseline it used, and
`test_a_wrong_baseline_is_wrong_but_still_fits_beautifully` pins the behaviour so nobody deletes
the warning.

The fit refuses rather than guessing: fewer than five readings above the baseline, or a window
that rises rather than decays, returns `fitted: false` with a reason. The rate assumes the room
is unoccupied and well mixed, which the dashboard says next to the number.

What it answers, in the two shapes that matter:

- **Room versus room.** One sensor and one calibration means the difference between two
  placements is real rather than instrument spread — the whole reason a single portable node
  beats several fixed ones.
- **Before versus after, within one placement.** Does the range hood work; did opening the door
  help the bedroom. The summary table reports mean, median, p95, min, max and a B − A row.

The two sides sit in a `minmax(0, 1fr)` grid that collapses to stacked panels below 720px, and
the stats table lives in a `.table-wrap` so it scrolls inside its panel rather than widening the
page. Both are the same phone-portrait constraint as the main grid.

**The compare chart is not in `charts`**, so the resize handler has to resize it explicitly —
missing that is what left a desktop-width canvas overflowing a 390px viewport the first time.

Still unbuilt: selecting a window by dragging on the main chart, so "compare last Tuesday's
dinner to tonight's" is a couple of taps rather than four datetime fields.
