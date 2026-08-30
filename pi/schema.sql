-- envlog schema.
--
-- Target SQLite 3.40, the version Raspberry Pi OS Bookworm ships. A development
-- sandbox may have something much newer: do not reach for STRICT tables or recent
-- JSON functions that pass locally and fail on the Pi.
--
-- Pragmas are split two ways on purpose. journal_mode is a persistent property of
-- the database file and belongs here. synchronous, busy_timeout and foreign_keys
-- reset on every connection and are set by the connection factory in app.py --
-- foreign_keys in particular defaults to OFF. See docs/design.md#sqlite-pragmas.

PRAGMA journal_mode = WAL;

-- Device identity. Never a room name: the node keeps its name when it moves.
CREATE TABLE IF NOT EXISTS nodes (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

-- Preset list rather than free text, so 'bedroom' and 'Bedroom' cannot split a
-- chart in two. Renaming a room here relabels every chart in the archive.
CREATE TABLE IF NOT EXISTS rooms (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

-- Where a node was, and when. Location is a property of time, not of the device.
-- end_ts IS NULL means "it is there now".
--
-- Placements for one node must not overlap. SQLite cannot express that
-- declaratively, so app.py closes the open placement before opening the next and
-- test_app.py carries an overlap check.
CREATE TABLE IF NOT EXISTS placements (
  id       INTEGER PRIMARY KEY,
  node_id  INTEGER NOT NULL REFERENCES nodes(id),
  room_id  INTEGER NOT NULL REFERENCES rooms(id),
  start_ts INTEGER NOT NULL,
  end_ts   INTEGER,
  note     TEXT
);
CREATE INDEX IF NOT EXISTS idx_placements_lookup ON placements(node_id, start_ts);

-- Wide rather than narrow (ts, metric, value): the sensor set is small and fixed,
-- wide reads faster, and ALTER TABLE ... ADD COLUMN is O(1) metadata-only in
-- SQLite, so adding a metric later stays cheap.
--
-- Both the BME280 and the SCD-41 report temperature and humidity, so the columns
-- are named per sensor. A single temp_c would silently discard one of them, and
-- the SCD-41's own reading is what you need to tune its temperature_offset.
--
-- ts is unix epoch SECONDS, UTC, assigned by the server on arrival.
CREATE TABLE IF NOT EXISTS readings (
  node_id      INTEGER NOT NULL REFERENCES nodes(id),
  ts           INTEGER NOT NULL,
  bme_temp_c   REAL,
  bme_rh_pct   REAL,
  pressure_hpa REAL,
  scd_temp_c   REAL,
  scd_rh_pct   REAL,
  co2_ppm      REAL,
  pm1_0_atm    REAL,
  pm2_5_atm    REAL,
  pm10_atm     REAL,
  boot_count   INTEGER,
  PRIMARY KEY (node_id, ts)
);
