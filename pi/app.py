"""envlog ingest service.

Flask behind waitress: a synchronous, single-writer service handling single-digit
requests per minute. Reasoning for every decision here is in docs/design.md; the
comments below note only what is easy to get wrong.

Run with::

    ENVLOG_TOKEN=... /opt/envlog/.venv/bin/python -m pi.app

or, in production, via the waitress entry point in systemd/envlog.service.
"""

from __future__ import annotations

import atexit
import csv
import hmac
import io
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask, Response, current_app, g, jsonify, redirect, request,
    send_from_directory,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
STATIC_DIR = os.path.join(HERE, "static")

# A browser navigating to / cannot set an X-Auth-Token header, so the token is
# also accepted once as ?token=... and then remembered in this cookie. Same
# secret, same constant-time comparison -- only the transport differs.
COOKIE_NAME = "envlog_token"

# Column -> plausible range. Anything outside is a sensor fault or a wiring
# mistake, not a reading; see _validate for why it NULLs the column rather than
# rejecting the whole row.
SENSOR_RANGES = {
    "bme_temp_c": (-50.0, 100.0),
    "bme_rh_pct": (0.0, 100.0),
    "pressure_hpa": (300.0, 1100.0),
    "scd_temp_c": (-50.0, 100.0),
    "scd_rh_pct": (0.0, 100.0),
    "co2_ppm": (0.0, 40000.0),
    "pm1_0_atm": (0.0, 1000.0),
    "pm2_5_atm": (0.0, 1000.0),
    "pm10_atm": (0.0, 1000.0),
    # Counts per 0.1 L. The sensor sends these as unsigned 16-bit, so 65535 is
    # the ceiling it can express rather than a plausibility judgement.
    "pm0_3_count": (0.0, 65535.0),
    "pm0_5_count": (0.0, 65535.0),
    "pm1_0_count": (0.0, 65535.0),
    "pm2_5_count": (0.0, 65535.0),
    "pm5_0_count": (0.0, 65535.0),
    "pm10_count": (0.0, 65535.0),
}
SENSOR_COLUMNS = tuple(SENSOR_RANGES)
READING_COLUMNS = ("node_id", "ts") + SENSOR_COLUMNS + ("boot_count",)

# Indoor column -> its counterpart in the `outdoor` table, for the reference
# line on the charts. Only pairs that mean the same quantity appear here: the
# point is "is this number mine or the whole region's", and that question is
# only well posed when both sides measure the same thing.
#
# Both temperature sensors map to the one outdoor temperature -- the BME280 and
# the SCD-41 disagree with each other indoors, which is the whole reason they
# are stored separately, but outside there is only one air.
#
# co2_ppm is deliberately absent. The upstream air-quality model carries carbon
# *monoxide*, not dioxide, and quietly pairing the two would be a units-grade
# error dressed up as a feature. The decay fit's 420 ppm stays an assumption.
# Particle counts are absent for the same reason: the model reports mass
# concentration, not counts per 0.1 L.
OUTDOOR_FOR = {
    "bme_temp_c": "temp_c",
    "scd_temp_c": "temp_c",
    "bme_rh_pct": "rh_pct",
    "scd_rh_pct": "rh_pct",
    "pressure_hpa": "pressure_hpa",
    "pm2_5_atm": "pm2_5_atm",
    "pm10_atm": "pm10_atm",
}
OUTDOOR_COLUMNS = ("temp_c", "rh_pct", "pressure_hpa", "pm2_5_atm",
                   "pm10_atm", "us_aqi")


class ValidationError(ValueError):
    """A request body the node should not have sent."""


# --------------------------------------------------------------------------- db


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas that do NOT persist in the file.

    journal_mode lives in schema.sql because it is a property of the database.
    These three reset every time, and foreign_keys defaults to OFF, so a
    connection opened without them silently runs with none of the intended
    settings.
    """
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        schema = fh.read()
    conn = connect(db_path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


def _get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["ENVLOG_DB"])
    return g.db


def _get_or_create(conn: sqlite3.Connection, table: str, name: str) -> int:
    """Resolve a nodes/rooms name to an id, inserting it if new.

    table is never user-supplied -- it is a literal at both call sites.
    """
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    return int(cur.lastrowid)


# ------------------------------------------------------------------------ auth


def _presented_token() -> str:
    """The token as offered by whichever transport the caller has.

    The node sends a header. A browser cannot, when it is following a link, so
    it may hand the token over once in the query string and thereafter carry the
    cookie the server sets. All three are the same secret.
    """
    header = request.headers.get("X-Auth-Token")
    if header:
        return header
    query = request.args.get("token")
    if query:
        return query
    return request.cookies.get(COOKIE_NAME, "")


def _token_ok() -> bool:
    # compare_digest rather than == so the comparison is not timing-variable.
    return hmac.compare_digest(
        _presented_token(), current_app.config["ENVLOG_TOKEN"]
    )


def _remember_token(response):
    """Persist the token so the dashboard keeps working across page loads.

    HttpOnly because no script needs to read it. Not Secure: docs/design.md rules
    out TLS here deliberately, and a Secure cookie over plain HTTP is simply
    never stored, which would break the dashboard rather than protect it.
    """
    response.set_cookie(
        COOKIE_NAME,
        current_app.config["ENVLOG_TOKEN"],
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="Lax",
    )
    return response


def _require_token(view):
    """Auth on every endpoint, not just /ingest.

    A tailnet is a flat network: every device and user on it can otherwise read
    the dashboard.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _token_ok():
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)

    return wrapper


# ------------------------------------------------------------------ validation


def _validate(payload: object) -> tuple[str, dict]:
    """Turn a POST body into (node_name, column values).

    A failed sensor NULLs its own columns and never drops the whole row: if the
    SCD-41 times out on I2C while the BME280 responds, that reading is still
    worth keeping. So an out-of-range value discards that column only.

    Structural problems -- not an object, missing node, unknown key, wrong type --
    are the node's bug and raise, because silently accepting them would hide a
    firmware typo behind a column of NULLs.
    """
    if not isinstance(payload, dict):
        raise ValidationError("body must be a JSON object")

    node = payload.get("node")
    if not isinstance(node, str) or not node.strip():
        raise ValidationError("'node' must be a non-empty string")

    known = set(SENSOR_COLUMNS) | {"node", "boot_count"}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ValidationError(f"unknown field(s): {', '.join(unknown)}")

    values: dict[str, float | int | None] = {c: None for c in SENSOR_COLUMNS}
    values["boot_count"] = None

    for column in SENSOR_COLUMNS:
        if column not in payload:
            continue  # omitted -> NULL -> a gap, which is the contract
        raw = payload[column]
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValidationError(f"'{column}' must be a number or null")
        low, high = SENSOR_RANGES[column]
        if not (low <= float(raw) <= high):
            continue  # implausible: NULL this column, keep the rest of the row
        values[column] = float(raw)

    boot = payload.get("boot_count")
    if boot is not None:
        if isinstance(boot, bool) or not isinstance(boot, int) or boot < 0:
            raise ValidationError("'boot_count' must be a non-negative integer")
        values["boot_count"] = boot

    # pm1_0 <= pm2_5 <= pm10 is physical: each size band includes the smaller
    # ones. A violation means the sensor is confused, so drop all three rather
    # than record an ordering that cannot happen.
    pm = [values["pm1_0_atm"], values["pm2_5_atm"], values["pm10_atm"]]
    if all(v is not None for v in pm) and not (pm[0] <= pm[1] <= pm[2]):
        values["pm1_0_atm"] = values["pm2_5_atm"] = values["pm10_atm"] = None

    # Counts run the other way: they are cumulative "at least this size", so
    # every particle counted at 0.5um was already counted at 0.3um and the
    # series must be non-increasing. Same reasoning as above -- an ordering that
    # cannot physically happen means the frame is not trustworthy, so drop the
    # set rather than record it.
    count_columns = (
        "pm0_3_count", "pm0_5_count", "pm1_0_count",
        "pm2_5_count", "pm5_0_count", "pm10_count",
    )
    counts = [values[c] for c in count_columns]
    if all(v is not None for v in counts) and any(a < b for a, b in zip(counts, counts[1:])):
        for c in count_columns:
            values[c] = None

    return node.strip(), values


# ----------------------------------------------------------------- write buffer


class ReadingBuffer:
    """Holds readings in memory and flushes them in one transaction per minute.

    One fsync a minute rather than one per reading is the largest single lever on
    SD-card wear. The cost is up to a minute of readings lost on an unclean
    shutdown, which docs/design.md accepts explicitly.
    """

    def __init__(self, db_path: str, interval: float) -> None:
        self._db_path = db_path
        self._interval = interval
        self._lock = threading.Lock()
        self._pending: list[tuple[str, int, dict]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, node: str, ts: int, values: dict) -> None:
        with self._lock:
            self._pending.append((node, ts, values))

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def flush(self) -> int:
        """Write everything buffered. Returns the number of rows accepted."""
        with self._lock:
            batch, self._pending = self._pending, []
        if not batch:
            return 0

        conn = None
        try:
            # connect() inside the try: if opening the database fails (locked,
            # disk full), the batch must go back on the queue, not vanish.
            conn = connect(self._db_path)
            with conn:  # one transaction, one fsync
                node_ids: dict[str, int] = {}
                rows = []
                for node, ts, values in batch:
                    if node not in node_ids:
                        node_ids[node] = _get_or_create(conn, "nodes", node)
                    rows.append(
                        (node_ids[node], ts) + tuple(values[c] for c in SENSOR_COLUMNS)
                        + (values["boot_count"],)
                    )
                placeholders = ", ".join("?" * len(READING_COLUMNS))
                # A client retry, or the Pi's clock stepping backwards after an
                # NTP sync, would otherwise collide on PRIMARY KEY (node_id, ts).
                conn.executemany(
                    f"INSERT INTO readings ({', '.join(READING_COLUMNS)}) "
                    f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    rows,
                )
        except sqlite3.Error:
            # Put them back rather than lose them; the next tick tries again.
            with self._lock:
                self._pending = batch + self._pending
            raise
        finally:
            if conn is not None:
                conn.close()
        return len(batch)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="envlog-flush", daemon=True
        )
        self._thread.start()
        atexit.register(self.stop)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.flush()
            except sqlite3.Error:  # pragma: no cover - transient lock contention
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        try:
            self.flush()  # last write on the way down
        except sqlite3.Error:  # pragma: no cover
            pass


# ---------------------------------------------------------------- series query

# All real-world IANA UTC offsets are whole multiples of 15 minutes, so folding
# 900-second buckets into local calendar days is exact -- and it lets a year-long
# day view aggregate ~35k buckets instead of ~700k raw rows on a Pi 2B.
DAY_SUBBUCKET = 900


def _resolve_node(conn: sqlite3.Connection, name: str | None) -> tuple[int, str]:
    if name:
        row = conn.execute("SELECT id, name FROM nodes WHERE name = ?", (name,)).fetchone()
    else:
        # Single-node is the normal case; only guess when it is unambiguous.
        rows = conn.execute("SELECT id, name FROM nodes ORDER BY id LIMIT 2").fetchall()
        if len(rows) != 1:
            raise ValidationError("'node' is required when several nodes exist")
        row = rows[0]
    if row is None:
        raise ValidationError(f"unknown node: {name}")
    return int(row["id"]), str(row["name"])


def _describe(values: list[float]) -> dict:
    """Summary stats over raw readings.

    Median and p95 by index rather than interpolation: with a few hundred
    readings the difference is noise, and an actual observed value is easier to
    sanity-check against the chart than one that sits between two of them.
    """
    n = len(values)
    if n == 0:
        return {"n": 0}
    ordered = sorted(values)

    def at(q):
        return ordered[min(n - 1, max(0, math.ceil(q * n) - 1))]

    return {
        "n": n,
        "mean": round(sum(ordered) / n, 2),
        "median": round(at(0.5), 2),
        "p95": round(at(0.95), 2),
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
    }


def _query_buckets(conn, node_id, metrics, t_from, t_to, bucket):
    """Aggregate readings into buckets, carrying the placement each falls in.

    The LEFT JOINs are the ones from docs/design.md#location-tracking: location
    resolves at query time, and unlabelled periods survive as 'Unknown' rather
    than silently vanishing.

    Column names are interpolated, so metrics MUST already be whitelisted.
    """
    selects = ["(r.ts / ?) * ? AS bucket_ts"]
    params: list[object] = [bucket, bucket]
    for m in metrics:
        selects.append(f"AVG(r.{m}) AS {m}")
        selects.append(f"COUNT(r.{m}) AS n_{m}")
    sql = f"""
        SELECT p.id AS placement_id,
               COALESCE(m.name, 'Unknown') AS location,
               p.note AS note,
               {', '.join(selects)}
        FROM readings r
        LEFT JOIN placements p
          ON  p.node_id = r.node_id
          AND r.ts >= p.start_ts
          AND (p.end_ts IS NULL OR r.ts < p.end_ts)
        LEFT JOIN rooms m ON m.id = p.room_id
        WHERE r.node_id = ? AND r.ts >= ? AND r.ts < ?
        GROUP BY placement_id, location, note, bucket_ts
        ORDER BY bucket_ts
    """
    params += [node_id, t_from, t_to]
    return conn.execute(sql, params).fetchall()


def _query_outdoor(conn, columns, t_from, t_to):
    """Outdoor rows covering the window, plus the last one before it.

    Deliberately NOT resampled onto the chart's bucket grid. The source is
    hourly and the charts are bucketed in seconds; interpolating between two
    hourly values would invent readings that were never modelled and hide how
    coarse the reference is. The dashboard step-holds each value until the next
    hour instead, which is what an hourly average actually claims.

    The row at or before t_from is included so the held line reaches the left
    edge of the chart rather than starting up to an hour in.

    Column names are interpolated, so they MUST already be whitelisted.
    """
    select = ", ".join(columns)
    sql = f"""
        SELECT ts, {select} FROM outdoor
        WHERE ts >= COALESCE((SELECT MAX(ts) FROM outdoor WHERE ts <= ?), ?)
          AND ts < ?
        ORDER BY ts
    """
    rows = conn.execute(sql, (t_from, t_from, t_to)).fetchall()
    return [[r["ts"]] + [r[c] for c in columns] for r in rows]


def _outdoor_columns_for(metrics):
    """The outdoor columns worth fetching for these indoor metrics, deduped.

    Both temperature sensors map to the single outdoor temperature, so asking
    for bme_temp_c and scd_temp_c together must not select temp_c twice.
    """
    columns, seen = [], set()
    for metric in metrics:
        counterpart = OUTDOOR_FOR.get(metric)
        if counterpart and counterpart not in seen:
            seen.add(counterpart)
            columns.append(counterpart)
    return columns


def _fold_into_local_days(rows, metrics, tz):
    """Combine 900s buckets into local calendar days.

    Weighted by each bucket's sample count -- averaging pre-averaged buckets
    unweighted would over-weight a bucket that happened to hold one reading.
    """
    out: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
        local = datetime.fromtimestamp(row["bucket_ts"], tz)
        day_start = int(
            local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        key = (row["placement_id"], row["location"], row["note"], day_start)
        if key not in out:
            out[key] = {m: [0.0, 0] for m in metrics}
            order.append(key)
        for m in metrics:
            n = row[f"n_{m}"] or 0
            if n:
                out[key][m][0] += float(row[m]) * n
                out[key][m][1] += n
    folded = []
    for key in order:
        placement_id, location, note, day_start = key
        values = {}
        for m in metrics:
            total, n = out[key][m]
            values[m] = (total / n) if n else None
        folded.append((placement_id, location, note, day_start, values))
    folded.sort(key=lambda item: item[3])
    return folded


def _segment(entries, metrics):
    """Split a flat, time-ordered list into one segment per placement.

    A move breaks the line: drawing a trend across a move from the bedroom to the
    kitchen renders a slope that never happened.
    """
    segments = []
    for placement_id, location, note, ts, values in entries:
        if not segments or segments[-1]["placement_id"] != placement_id:
            segments.append(
                {
                    "placement_id": placement_id,
                    "location": location,
                    "note": note,
                    "rows": [],
                }
            )
        segments[-1]["rows"].append([ts] + [values[m] for m in metrics])
    return segments


# --------------------------------------------------------------------- the app


def create_app(db_path=None, token=None, flush_interval=None, start_flusher=True):
    # static_folder=None: Flask's built-in /static route bypasses view decorators,
    # which would leave the one set of unauthenticated endpoints in the service.
    app = Flask(__name__, static_folder=None)
    # `is None` rather than `or`: an explicitly passed empty token must NOT fall
    # through to the environment, or the refuse-to-start guard below can be
    # bypassed by whatever happens to be exported in the shell.
    app.config["ENVLOG_DB"] = (
        db_path if db_path is not None
        else os.environ.get("ENVLOG_DB", "/var/lib/envlog/envlog.db")
    )
    app.config["ENVLOG_TOKEN"] = (
        token if token is not None else os.environ.get("ENVLOG_TOKEN", "")
    )
    # A body cap costs nothing and is the one real protection here; docs/design.md
    # explains why TLS on the LAN would be theatre and this is not.
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    if not app.config["ENVLOG_TOKEN"]:
        raise RuntimeError(
            "ENVLOG_TOKEN is not set. Refusing to start: auth covers every "
            "endpoint, so an empty token would expose all of them."
        )

    init_db(app.config["ENVLOG_DB"])

    interval = (
        flush_interval
        if flush_interval is not None
        else float(os.environ.get("ENVLOG_FLUSH_INTERVAL", "60"))
    )
    app.buffer = ReadingBuffer(app.config["ENVLOG_DB"], interval)
    if start_flusher:
        app.buffer.start()

    @app.teardown_appcontext
    def _close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.errorhandler(ValidationError)
    def _bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(413)
    def _too_large(_exc):
        return jsonify(error="request body too large"), 413

    # ---------------------------------------------------------------- ingest

    @app.post("/ingest")
    @_require_token
    def ingest():
        node, values = _validate(request.get_json(silent=True))
        # The server assigns ts on arrival. The ESP32 has no RTC, and LAN latency
        # is milliseconds against a 45-second sample interval. This is also why
        # there is no batch endpoint: a replayed backlog would arrive with one
        # timestamp and collide with itself.
        ts = int(time.time())
        app.buffer.add(node, ts, values)
        return jsonify(status="buffered", ts=ts), 202

    # ------------------------------------------------------------ placements

    @app.get("/placements")
    @_require_token
    def list_placements():
        conn = _get_db()
        rows = conn.execute(
            """SELECT p.id, n.name AS node, m.name AS room,
                      p.start_ts, p.end_ts, p.note
               FROM placements p
               JOIN nodes n ON n.id = p.node_id
               JOIN rooms m ON m.id = p.room_id
               ORDER BY p.start_ts DESC"""
        ).fetchall()
        return jsonify(placements=[dict(r) for r in rows])

    @app.post("/placements")
    @_require_token
    def create_placement():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValidationError("body must be a JSON object")
        node = payload.get("node")
        room = payload.get("room")
        if not isinstance(node, str) or not node.strip():
            raise ValidationError("'node' must be a non-empty string")
        if not isinstance(room, str) or not room.strip():
            raise ValidationError("'room' must be a non-empty string")
        note = payload.get("note")
        if note is not None and not isinstance(note, str):
            raise ValidationError("'note' must be a string or null")
        start_ts = payload.get("start_ts", int(time.time()))
        if isinstance(start_ts, bool) or not isinstance(start_ts, int):
            raise ValidationError("'start_ts' must be an integer unix timestamp")

        conn = _get_db()
        with conn:
            node_id = _get_or_create(conn, "nodes", node.strip())
            room_id = _get_or_create(conn, "rooms", room.strip())
            # Close the open placement before opening the next. SQLite cannot
            # enforce non-overlap declaratively, so this is the invariant's only
            # guard -- test_app.py checks it holds.
            conn.execute(
                "UPDATE placements SET end_ts = ? "
                "WHERE node_id = ? AND end_ts IS NULL AND start_ts <= ?",
                (start_ts, node_id, start_ts),
            )
            cur = conn.execute(
                "INSERT INTO placements (node_id, room_id, start_ts, end_ts, note) "
                "VALUES (?, ?, ?, NULL, ?)",
                (node_id, room_id, start_ts, note),
            )
            placement_id = int(cur.lastrowid)
        return jsonify(id=placement_id, node=node.strip(), room=room.strip(),
                       start_ts=start_ts, note=note), 201

    @app.patch("/placements/<int:placement_id>")
    @_require_token
    def update_placement(placement_id: int):
        """Retroactive correction: mislabelled history is one row, not thousands."""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValidationError("body must be a JSON object")
        allowed = {"room", "start_ts", "end_ts", "note"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValidationError(f"unknown field(s): {', '.join(unknown)}")
        if not payload:
            raise ValidationError("nothing to update")

        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM placements WHERE id = ?", (placement_id,)
        ).fetchone()
        if row is None:
            return jsonify(error="no such placement"), 404

        sets, params = [], []
        with conn:
            if "room" in payload:
                room = payload["room"]
                if not isinstance(room, str) or not room.strip():
                    raise ValidationError("'room' must be a non-empty string")
                sets.append("room_id = ?")
                params.append(_get_or_create(conn, "rooms", room.strip()))
            for field in ("start_ts", "end_ts"):
                if field in payload:
                    value = payload[field]
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, int)
                    ):
                        raise ValidationError(f"'{field}' must be an integer or null")
                    sets.append(f"{field} = ?")
                    params.append(value)
            if "note" in payload:
                note = payload["note"]
                if note is not None and not isinstance(note, str):
                    raise ValidationError("'note' must be a string or null")
                sets.append("note = ?")
                params.append(note)

            start_ts = payload.get("start_ts", row["start_ts"])
            end_ts = payload.get("end_ts", row["end_ts"])
            if start_ts is None:
                raise ValidationError("'start_ts' may not be null")
            if end_ts is not None and end_ts <= start_ts:
                raise ValidationError("'end_ts' must be after 'start_ts'")

            params.append(placement_id)
            conn.execute(
                f"UPDATE placements SET {', '.join(sets)} WHERE id = ?", params
            )
            updated = conn.execute(
                """SELECT p.id, n.name AS node, m.name AS room,
                          p.start_ts, p.end_ts, p.note
                   FROM placements p
                   JOIN nodes n ON n.id = p.node_id
                   JOIN rooms m ON m.id = p.room_id
                   WHERE p.id = ?""",
                (placement_id,),
            ).fetchone()
        return jsonify(dict(updated))

    # --------------------------------------------------------------- markers

    def _marker(conn: sqlite3.Connection, marker_id: int):
        return conn.execute(
            "SELECT id, ts, label FROM markers WHERE id = ?", (marker_id,)
        ).fetchone()

    def _marker_fields(payload: object) -> dict:
        """Shared by POST and PATCH so the two cannot drift apart on what a
        valid marker is."""
        if not isinstance(payload, dict):
            raise ValidationError("body must be a JSON object")
        unknown = sorted(set(payload) - {"ts", "label"})
        if unknown:
            raise ValidationError(f"unknown field(s): {', '.join(unknown)}")
        fields = {}
        if "label" in payload:
            label = payload["label"]
            if not isinstance(label, str) or not label.strip():
                raise ValidationError("'label' must be a non-empty string")
            fields["label"] = label.strip()
        if "ts" in payload:
            ts = payload["ts"]
            if isinstance(ts, bool) or not isinstance(ts, int):
                raise ValidationError("'ts' must be an integer unix timestamp")
            fields["ts"] = ts
        return fields

    @app.get("/markers")
    @_require_token
    def list_markers():
        # Unbounded, like /placements. These are hand-typed events -- a few a day
        # at most -- so the whole history is a small payload, and having it all
        # client-side is what lets the label autocomplete cover more than the
        # window currently on screen.
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, ts, label FROM markers ORDER BY ts DESC"
        ).fetchall()
        return jsonify(markers=[dict(r) for r in rows])

    @app.post("/markers")
    @_require_token
    def create_marker():
        fields = _marker_fields(request.get_json(silent=True))
        if "label" not in fields:
            raise ValidationError("'label' is required")
        ts = fields.get("ts", int(time.time()))
        conn = _get_db()
        with conn:
            cur = conn.execute(
                "INSERT INTO markers (ts, label) VALUES (?, ?)",
                (ts, fields["label"]),
            )
            marker_id = int(cur.lastrowid)
        return jsonify(id=marker_id, ts=ts, label=fields["label"]), 201

    @app.patch("/markers/<int:marker_id>")
    @_require_token
    def update_marker(marker_id: int):
        fields = _marker_fields(request.get_json(silent=True))
        if not fields:
            raise ValidationError("nothing to update")
        conn = _get_db()
        if _marker(conn, marker_id) is None:
            return jsonify(error="no such marker"), 404
        sets = ", ".join(f"{name} = ?" for name in fields)
        with conn:
            conn.execute(
                f"UPDATE markers SET {sets} WHERE id = ?",
                [*fields.values(), marker_id],
            )
            updated = _marker(conn, marker_id)
        return jsonify(dict(updated))

    @app.delete("/markers/<int:marker_id>")
    @_require_token
    def delete_marker(marker_id: int):
        """Placing a marker is one click on a chart, so misplacing one is too.
        Removal has to be as cheap as creation or the charts fill with noise."""
        conn = _get_db()
        if _marker(conn, marker_id) is None:
            return jsonify(error="no such marker"), 404
        with conn:
            conn.execute("DELETE FROM markers WHERE id = ?", (marker_id,))
        return "", 204

    # ----------------------------------------------------------------- export

    @app.get("/api/export")
    @_require_token
    def export_csv():
        """Raw rows as CSV, so a question the dashboard does not answer can be
        answered in a spreadsheet instead of by growing the dashboard."""
        conn = _get_db()
        if not conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone():
            return Response("", mimetype="text/csv")
        node_id, node_name = _resolve_node(conn, request.args.get("node"))

        bounds, params = [], [node_id]
        for field, op in (("from", ">="), ("to", "<=")):
            raw = request.args.get(field)
            if raw is None:
                continue
            try:
                params.append(int(raw))
            except (TypeError, ValueError):
                raise ValidationError(f"'{field}' must be an integer unix timestamp")
            bounds.append(f"AND r.ts {op} ?")

        # Room comes from the placement covering each reading, the same range
        # join the charts use -- an export without it loses the one dimension
        # the whole project is organised around.
        sql = f"""
            SELECT r.ts,
                   COALESCE(m.name, 'Unknown') AS room,
                   {', '.join('r.' + c for c in SENSOR_COLUMNS)},
                   r.boot_count
            FROM readings r
            LEFT JOIN placements p
              ON  p.node_id = r.node_id
              AND r.ts >= p.start_ts
              AND (p.end_ts IS NULL OR r.ts < p.end_ts)
            LEFT JOIN rooms m ON m.id = p.room_id
            WHERE r.node_id = ? {' '.join(bounds)}
            ORDER BY r.ts
        """
        header = ("ts", "iso_utc", "room") + SENSOR_COLUMNS + ("boot_count",)

        db_path = current_app.config["ENVLOG_DB"]

        def rows():
            # Streamed: a year of readings is a few hundred thousand rows, and
            # building that in memory on a Pi to hand back one string is the
            # kind of thing that works in testing and dies in the field.
            #
            # Its own connection, deliberately. A generator is consumed *after*
            # the request context has torn down, and the request-scoped
            # connection is closed by then -- so reusing g.db raises
            # "Cannot operate on a closed database" at the first row.
            export_conn = connect(db_path)
            buf = io.StringIO()
            writer = csv.writer(buf)

            def drain():
                value = buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
                return value

            try:
                writer.writerow(header)
                yield drain()
                for row in export_conn.execute(sql, params):
                    ts = int(row["ts"])
                    writer.writerow(
                        [ts, datetime.fromtimestamp(ts, timezone.utc).isoformat()]
                        + [row[c] for c in ("room",) + SENSOR_COLUMNS + ("boot_count",)]
                    )
                    yield drain()
            finally:
                export_conn.close()

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return Response(
            rows(),
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="envlog-{node_name}-{stamp}.csv"'
            },
        )

    # ------------------------------------------------------- candidate moves

    # A move means unplugging the node, carrying it, and plugging it back in --
    # so every move is a reboot with a gap in the readings. The converse does not
    # hold (a power blip or an OTA reboots without going anywhere), which is why
    # these are candidates to confirm rather than placements to create.
    CANDIDATE_GAP_SECONDS = 120

    @app.get("/api/candidate-moves")
    @_require_token
    def candidate_moves():
        conn = _get_db()
        if not conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone():
            return jsonify(candidates=[])
        node_id, node_name = _resolve_node(conn, request.args.get("node"))
        # Window functions are 3.25, comfortably under the deployed 3.27.2.
        rows = conn.execute(
            """SELECT ts, prev_ts, boot_count, prev_boot FROM (
                   SELECT ts, boot_count,
                          LAG(ts)         OVER (ORDER BY ts) AS prev_ts,
                          LAG(boot_count) OVER (ORDER BY ts) AS prev_boot
                   FROM readings
                   WHERE node_id = ? AND boot_count IS NOT NULL
               )
               WHERE prev_boot IS NOT NULL
                 AND boot_count > prev_boot
                 AND ts - prev_ts >= ?
               ORDER BY ts DESC""",
            (node_id, CANDIDATE_GAP_SECONDS),
        ).fetchall()

        # Drop the ones already explained. A placement that starts inside the gap
        # is you having already recorded this move, and nagging about it again is
        # how a useful prompt becomes noise you learn to ignore.
        explained = [
            r["start_ts"]
            for r in conn.execute(
                "SELECT start_ts FROM placements WHERE node_id = ?", (node_id,)
            ).fetchall()
        ]
        candidates = [
            {
                "ts": int(r["ts"]),
                "prev_ts": int(r["prev_ts"]),
                "gap_seconds": int(r["ts"] - r["prev_ts"]),
                "boot_count": int(r["boot_count"]),
            }
            for r in rows
            if not any(r["prev_ts"] <= s <= r["ts"] for s in explained)
        ]
        return jsonify(node=node_name, candidates=candidates)
    # ---------------------------------------------------------- compare / fit

    # Minute-precision inputs make sub-minute overlaps meaningless.
    ROOM_OVERLAP_SECONDS = 60

    def _window(args, prefix):
        try:
            w_from = int(args[f"{prefix}_from"])
            w_to = int(args[f"{prefix}_to"])
        except KeyError:
            raise ValidationError(f"'{prefix}_from' and '{prefix}_to' are required")
        except ValueError:
            raise ValidationError(f"'{prefix}_*' must be integer unix timestamps")
        if w_from >= w_to:
            raise ValidationError(f"'{prefix}_from' must be before '{prefix}_to'")
        return w_from, w_to

    def _raw(conn, node_id, metric, w_from, w_to):
        """Raw values, not bucket averages.

        A p95 of averages is not a p95 -- bucketing is a drawing concern and
        would flatten exactly the tail that PM questions live in.
        """
        # metric is whitelisted against SENSOR_RANGES by every caller before it
        # reaches this interpolation.
        return [
            r[0]
            for r in conn.execute(
                f"SELECT {metric} FROM readings "
                f"WHERE node_id = ? AND ts >= ? AND ts < ? AND {metric} IS NOT NULL",
                (node_id, w_from, w_to),
            ).fetchall()
        ]

    @app.get("/api/compare")
    @_require_token
    def compare():
        """Two windows, side by side. The questions this project exists for are
        comparisons, and a time series alone cannot answer one."""
        conn = _get_db()
        args = request.args
        metrics = [m.strip() for m in args.get("metrics", "co2_ppm").split(",") if m.strip()]
        bad = [m for m in metrics if m not in SENSOR_RANGES]
        if bad:
            raise ValidationError(f"unknown metric(s): {', '.join(bad)}")
        if not metrics:
            raise ValidationError("'metrics' must name at least one column")

        if not conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone():
            return jsonify(a={}, b={}, node=None)
        node_id, node_name = _resolve_node(conn, args.get("node"))

        out = {}
        for side in ("a", "b"):
            w_from, w_to = _window(args, side)
            out[side] = {
                "from": w_from,
                "to": w_to,
                "rooms": _rooms_in(conn, node_id, w_from, w_to),
                "metrics": {
                    m: _describe(_raw(conn, node_id, m, w_from, w_to))
                    for m in metrics
                },
            }
        return jsonify(node=node_name, **out)

    def _rooms_in(conn, node_id, w_from, w_to):
        """Which rooms a window actually covers -- a window that straddles a
        move is comparing two rooms to itself, and the caller should be told."""
        # A *material* overlap, not any overlap. The dashboard's datetime-local
        # inputs have minute precision, so a window pinned to a placement
        # boundary spills up to 59s into the neighbour -- and reporting that as
        # "this window covers two rooms" is a false alarm about the one thing
        # this field exists to warn about.
        rows = conn.execute(
            """SELECT DISTINCT COALESCE(m.name, 'Unknown') AS room
               FROM placements p LEFT JOIN rooms m ON m.id = p.room_id
               WHERE p.node_id = ?
                 AND MIN(COALESCE(p.end_ts, ?), ?) - MAX(p.start_ts, ?) >= ?
               ORDER BY room""",
            (node_id, w_to, w_to, w_from, ROOM_OVERLAP_SECONDS),
        ).fetchall()
        return [r["room"] for r in rows] or ["Unknown"]

    @app.get("/api/decay")
    @_require_token
    def decay():
        """Fit an exponential decay and report its rate.

        On CO2 after a room empties that rate is air changes per hour; on PM
        after cooking it is how fast the room clears. Both are the quantitative
        payoff the charts only hint at.
        """
        conn = _get_db()
        args = request.args
        metric = args.get("metric", "co2_ppm")
        if metric not in SENSOR_RANGES:
            raise ValidationError(f"unknown metric: {metric}")
        if not conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone():
            return jsonify(fitted=False, reason="no readings yet")
        node_id, _ = _resolve_node(conn, args.get("node"))
        w_from, w_to = _window(args, "w")

        # Outdoor CO2 is the floor a room decays toward, not zero -- fitting
        # against zero bends the curve and inflates the rate. Particulates do
        # decay toward ~0 indoors.
        default_baseline = 420.0 if metric == "co2_ppm" else 0.0
        try:
            baseline = float(args.get("baseline", default_baseline))
        except ValueError:
            raise ValidationError("'baseline' must be a number")

        rows = conn.execute(
            f"SELECT ts, {metric} AS v FROM readings "
            f"WHERE node_id = ? AND ts >= ? AND ts < ? AND {metric} IS NOT NULL "
            "ORDER BY ts",
            (node_id, w_from, w_to),
        ).fetchall()
        points = [(int(r["ts"]), float(r["v"])) for r in rows if r["v"] > baseline]
        if len(points) < 5:
            return jsonify(
                fitted=False, metric=metric, baseline=baseline,
                n=len(points),
                reason="fewer than 5 readings above the baseline in this window",
            )

        # ln(v - baseline) against elapsed seconds is a straight line whose
        # slope is -lambda. Least squares on that, plus r^2 -- because a window
        # that is not actually a decay still yields a number, and reporting it
        # without saying how well it fits would be the dishonest part.
        t0 = points[0][0]
        xs = [t - t0 for t, _ in points]
        ys = [math.log(v - baseline) for _, v in points]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx == 0:
            return jsonify(fitted=False, metric=metric, baseline=baseline,
                           n=n, reason="all readings share one timestamp")
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        intercept = my - slope * mx
        ss_tot = sum((y - my) ** 2 for y in ys)
        ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        r2 = 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot

        if slope >= 0:
            return jsonify(
                fitted=False, metric=metric, baseline=baseline, n=n, r2=round(r2, 4),
                reason="this window rises rather than decays",
            )

        per_hour = -slope * 3600.0
        return jsonify(
            fitted=True,
            metric=metric,
            baseline=baseline,
            n=n,
            **{"from": w_from}, to=w_to,
            rate_per_hour=round(per_hour, 3),
            half_life_minutes=round(math.log(2) / -slope / 60.0, 1),
            start_value=round(points[0][1], 1),
            end_value=round(points[-1][1], 1),
            r2=round(r2, 4),
        )

    # -------------------------------------------------------------- read API

    @app.get("/api/status")
    @_require_token
    def status():
        """Liveness. Without it a dead node is discovered whenever you next
        happen to open the page -- a real risk for a node that is often
        unplugged."""
        conn = _get_db()
        requested = request.args.get("node")
        if requested is None and not conn.execute(
            "SELECT 1 FROM nodes LIMIT 1"
        ).fetchone():
            # A freshly installed Pi has no nodes yet. That is a legitimate
            # state to report, not a client error -- this endpoint is exactly
            # what you curl to find out whether anything has arrived.
            return jsonify(
                node=None, now=int(time.time()), last_ts=None,
                seconds_since_last=None, buffered=app.buffer.pending_count(),
                placement=None, outdoor_last_ts=None,
            )
        node_id, node_name = _resolve_node(conn, requested)
        row = conn.execute(
            "SELECT MAX(ts) AS last_ts FROM readings WHERE node_id = ?", (node_id,)
        ).fetchone()
        last_ts = row["last_ts"] if row else None
        where = conn.execute(
            """SELECT m.name AS room, p.note, p.start_ts
               FROM placements p JOIN rooms m ON m.id = p.room_id
               WHERE p.node_id = ? AND p.end_ts IS NULL
               ORDER BY p.start_ts DESC LIMIT 1""",
            (node_id,),
        ).fetchone()
        now = int(time.time())
        # The outdoor fetcher is a separate timer, so it can die without the
        # service noticing. Reporting its newest hour here is what turns that
        # from "the reference line just stopped one day" into something the
        # header can say out loud.
        outdoor_row = conn.execute("SELECT MAX(ts) AS last_ts FROM outdoor").fetchone()
        outdoor_last = outdoor_row["last_ts"] if outdoor_row else None
        return jsonify(
            node=node_name,
            now=now,
            last_ts=last_ts,
            seconds_since_last=(None if last_ts is None else now - int(last_ts)),
            buffered=app.buffer.pending_count(),
            placement=(dict(where) if where else None),
            outdoor_last_ts=outdoor_last,
        )

    @app.get("/api/series")
    @_require_token
    def series():
        conn = _get_db()
        args = request.args

        node_id, node_name = _resolve_node(conn, args.get("node"))

        requested = args.get("metrics", "co2_ppm")
        metrics = [m.strip() for m in requested.split(",") if m.strip()]
        # Whitelist: these names go straight into the SELECT list.
        bad = [m for m in metrics if m not in SENSOR_RANGES]
        if bad:
            raise ValidationError(f"unknown metric(s): {', '.join(bad)}")
        if not metrics:
            raise ValidationError("'metrics' must name at least one column")

        tz_name = args.get("tz", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError(f"unknown timezone: {tz_name}")

        now = int(time.time())
        try:
            t_to = int(args.get("to", now))
            t_from = int(args.get("from", t_to - 24 * 3600))
        except ValueError:
            raise ValidationError("'from' and 'to' must be integer unix timestamps")
        if t_from >= t_to:
            raise ValidationError("'from' must be before 'to'")

        bucket_arg = args.get("bucket", "1")
        if bucket_arg == "day":
            rows = _query_buckets(
                conn, node_id, metrics, t_from, t_to, DAY_SUBBUCKET
            )
            entries = _fold_into_local_days(rows, metrics, tz)
        else:
            try:
                bucket = int(bucket_arg)
            except ValueError:
                raise ValidationError("'bucket' must be an integer or 'day'")
            if bucket < 1:
                raise ValidationError("'bucket' must be at least 1 second")
            rows = _query_buckets(conn, node_id, metrics, t_from, t_to, bucket)
            entries = [
                (
                    r["placement_id"],
                    r["location"],
                    r["note"],
                    r["bucket_ts"],
                    {m: r[m] for m in metrics},
                )
                for r in rows
            ]

        # The outdoor reference rides along rather than living at its own
        # endpoint: every caller that wants indoor PM2.5 for a window wants
        # outdoor PM2.5 for the same window, and a second round trip could
        # return a different one.
        outdoor_columns = _outdoor_columns_for(metrics)
        outdoor = None
        if outdoor_columns:
            outdoor = {
                "columns": ["ts"] + outdoor_columns,
                # The indoor -> outdoor pairing travels with the data so the
                # dashboard does not carry a second copy of the map that then
                # drifts from this one.
                "pairs": {m: OUTDOOR_FOR[m] for m in metrics if m in OUTDOOR_FOR},
                "rows": _query_outdoor(conn, outdoor_columns, t_from, t_to),
            }

        return jsonify(
            node=node_name,
            tz=tz_name,
            bucket=bucket_arg,
            **{"from": t_from},
            to=t_to,
            columns=["ts"] + metrics,
            segments=_segment(entries, metrics),
            # null, not {}, when no requested metric has a counterpart -- the
            # dashboard distinguishes "nothing to compare against" from
            # "comparable, but the table is empty because the timer is off".
            outdoor=outdoor,
        )

    @app.get("/rooms")
    @_require_token
    def list_rooms():
        """The picker's preset list. Free text would split 'bedroom' and
        'Bedroom' into two rooms and two halves of every chart."""
        conn = _get_db()
        rows = conn.execute("SELECT id, name FROM rooms ORDER BY name").fetchall()
        return jsonify(rooms=[dict(r) for r in rows])

    @app.get("/static/<path:filename>")
    @_require_token
    def static_file(filename: str):
        return send_from_directory(STATIC_DIR, filename)

    @app.post("/login")
    def login():
        """Exchange the token for a cookie, so the dashboard survives a reload."""
        presented = request.form.get("token", "")
        if not hmac.compare_digest(presented, app.config["ENVLOG_TOKEN"]):
            return _login_page("That token was not accepted."), 401
        return _remember_token(redirect("/"))

    @app.get("/")
    def dashboard():
        if request.args.get("token") and _token_ok():
            # Strip the token back out of the URL so it stops living in browser
            # history, bookmarks and any proxy log between here and the browser.
            return _remember_token(redirect("/"))
        if not _token_ok():
            return _login_page(), 401
        return send_from_directory(STATIC_DIR, "index.html")

    return app


def _login_page(message: str = "") -> str:
    note = f'<p class="err">{message}</p>' if message else ""
    return f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>envlog</title>
<link rel="icon" href="data:,">
<style>
  body {{ font: 15px/1.5 system-ui, sans-serif; background:#11141a; color:#e6e9ef;
         display:grid; place-items:center; height:100vh; margin:0 }}
  form {{ background:#1a1f29; padding:24px; border-radius:10px; width:min(90vw,340px) }}
  h1 {{ font-size:16px; margin:0 0 12px }}
  input {{ width:100%; padding:8px; border-radius:6px; border:1px solid #333b4a;
           background:#0d1015; color:inherit; font:inherit; box-sizing:border-box }}
  button {{ margin-top:12px; width:100%; padding:8px; border:0; border-radius:6px;
            background:#4c8dff; color:#fff; font:inherit; cursor:pointer }}
  .err {{ color:#ff6b6b; margin:0 0 12px }}
</style>
<form method=post action=/login>
  <h1>envlog</h1>
  {note}
  <input name=token type=password placeholder="Ingest token" autofocus>
  <button type=submit>Open dashboard</button>
</form>"""


def main() -> None:  # pragma: no cover - process entry point
    from waitress import serve

    app = create_app()
    host = os.environ.get("ENVLOG_BIND", "0.0.0.0")
    port = int(os.environ.get("ENVLOG_PORT", "8000"))
    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":  # pragma: no cover
    main()
