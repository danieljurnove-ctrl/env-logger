-- envlog schema.
--
-- Target SQLite 3.27.2, the version the deployed box (Raspbian Buster) ships -- not
-- the 3.40 of Bookworm this was first written against. A development sandbox will
-- have something much newer, so the ceiling is easy to breach by accident: no
-- STRICT tables (3.37), no RETURNING (3.35), no ->> (3.38), no unixepoch() (3.38),
-- no generated columns (3.31). VACUUM INTO, which the backup needs, is 3.27.0 --
-- one patch release below what is installed. See docs/deployment.md.
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
  -- Particle counts per 0.1 L, cumulative: every particle counted at 0.5um is
  -- also counted at 0.3um. Stored alongside mass because mass is derived from
  -- these and reported as an integer, so a clean room reads 0.0 ug/m3 on all
  -- three sizes for hours while the counts move over hundreds. The counts are
  -- what answer "is this room dustier than that one" indoors.
  pm0_3_count  REAL,
  pm0_5_count  REAL,
  pm1_0_count  REAL,
  pm2_5_count  REAL,
  pm5_0_count  REAL,
  pm10_count   REAL,
  boot_count   INTEGER,
  PRIMARY KEY (node_id, ts)
);

-- Arbitrary annotations: "opened windows", "left house", "cooked with fan on".
-- The charts exist to answer before-and-after questions, and without these
-- nothing records what the before and the after actually were. A CO2 decay
-- curve is a ventilation measurement only once you know a window opened.
--
-- Points, not intervals. An event with a duration is two markers, which keeps
-- both the schema and the click-to-place interaction trivial; ALTER TABLE ADD
-- COLUMN is O(1) metadata-only in SQLite if that ever needs revisiting.
--
-- Deliberately not scoped to a node. Every marker shows on every chart, which
-- is what "left house" wants, and which room the node was in at that instant is
-- already answered by placements.
CREATE TABLE IF NOT EXISTS markers (
  id    INTEGER PRIMARY KEY,
  ts    INTEGER NOT NULL,
  label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markers_ts ON markers(ts);

-- Outdoor conditions, from a public weather/air-quality model. The reference
-- that turns "my PM2.5 is 40" into "my PM2.5 is 40 and outside is 8, so this is
-- mine" -- or into "outside is 55, so shut the window". Without it every indoor
-- number is unanchored, and a wildfire two states away reads as a kitchen
-- problem.
--
-- NOT a measurement of your street. It is a regional model on a grid of several
-- kilometres, so treat it as the trend outside your neighbourhood, not as a
-- second sensor. It will disagree with a thermometer on your porch, and the
-- thermometer is right.
--
-- One row per hour, keyed on ts alone: outdoor is outdoor, so unlike readings
-- this is not per-node. Hours are the top of the hour, unix seconds, UTC.
--
-- Written with ON CONFLICT DO UPDATE rather than DO NOTHING, which is the
-- opposite of readings and deliberate: the upstream model revises recent hours
-- as observations replace forecasts, so a re-fetch of an hour we already hold
-- is a correction to take, not a duplicate to drop.
CREATE TABLE IF NOT EXISTS outdoor (
  ts           INTEGER PRIMARY KEY,
  temp_c       REAL,
  rh_pct       REAL,
  pressure_hpa REAL,
  pm2_5_atm    REAL,
  pm10_atm     REAL,
  us_aqi       REAL,
  -- When this row was last written. The gap between fetched_at and ts is how
  -- you tell a settled observation from a value still being revised, and a
  -- stale fetched_at across every recent row is how you notice the timer died.
  fetched_at   INTEGER NOT NULL
);
