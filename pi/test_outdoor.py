"""Tests for the outdoor reference fetcher.

Run from the repo root::

    python -m pytest pi -q

Every test here runs against a local server returning Open-Meteo's documented
shape, because egress to open-meteo.com is blocked from the environment this
was written in. That covers the parsing, the hour filtering and the upsert --
but a stand-in cannot tell you the variable names are the ones the real service
serves. That was checked separately, by running `fetch_outdoor.py --dry-run` on
the Pi on 2026-09-06: 72 hours, all six columns populated.

`test_a_variable_the_upstream_omits_becomes_null` stays, and matters more now
than it did then: the names are upstream's to change, and when one does the
failure has to be a blank line on a chart rather than an hourly job that dies.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import fetch_outdoor
from app import init_db

HOUR = 3600


def hours_around(now: int, past: int, future: int) -> list[int]:
    """Whole hours, `past` behind the current hour and `future` ahead."""
    top = now - (now % HOUR)
    return [top + i * HOUR for i in range(-past, future + 1)]


def iso(ts: int) -> str:
    """The naive-UTC spelling Open-Meteo uses when no timezone is requested."""
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))


class FakeUpstream:
    """A stand-in for the two Open-Meteo endpoints.

    Serves the documented shape: an `hourly` object holding a `time` array and
    one parallel array per requested variable.
    """

    def __init__(self, weather: dict, air: dict, times: list[int]):
        self.weather, self.air, self.times = weather, air, times
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - name fixed by the stdlib
                outer.requests.append(self.path)
                if self.path.startswith("/v1/forecast"):
                    body = {"hourly": dict(time=[iso(t) for t in outer.times],
                                           **outer.weather)}
                elif self.path.startswith("/v1/air-quality"):
                    body = {"hourly": dict(time=[iso(t) for t in outer.times],
                                           **outer.air)}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # keep pytest output readable
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


@pytest.fixture()
def upstream(monkeypatch):
    """A server whose values are simply the index, so a misalignment shows."""
    now = int(time.time())
    times = hours_around(now, past=3, future=2)
    n = len(times)
    weather = {
        "temperature_2m": [10.0 + i for i in range(n)],
        "relative_humidity_2m": [50.0 + i for i in range(n)],
        "surface_pressure": [1000.0 + i for i in range(n)],
    }
    air = {
        "pm2_5": [5.0 + i for i in range(n)],
        "pm10": [8.0 + i for i in range(n)],
        "us_aqi": [20.0 + i for i in range(n)],
    }
    with FakeUpstream(weather, air, times) as server:
        monkeypatch.setattr(fetch_outdoor, "WEATHER_URL", server.base + "/v1/forecast")
        monkeypatch.setattr(
            fetch_outdoor, "AIR_QUALITY_URL", server.base + "/v1/air-quality"
        )
        # The sandbox routes everything through a proxy that would swallow a
        # request to a loopback port.
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        server.times = times
        yield server


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "envlog.db")
    init_db(path)
    return path


def outdoor_rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM outdoor ORDER BY ts").fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------- parsing


def test_parses_the_documented_shape(upstream):
    rows = fetch_outdoor.fetch(40.71, -74.01, past_days=1)
    assert rows, "nothing parsed out of a well-formed response"
    ts = min(rows)
    # Index 0 of both responses, so every column comes from the same hour --
    # this is what catches the two endpoints being merged off by one.
    assert rows[ts]["temp_c"] == 10.0
    assert rows[ts]["rh_pct"] == 50.0
    assert rows[ts]["pressure_hpa"] == 1000.0
    assert rows[ts]["pm2_5_atm"] == 5.0
    assert rows[ts]["pm10_atm"] == 8.0
    assert rows[ts]["us_aqi"] == 20.0


def test_future_hours_are_dropped(upstream):
    """Both endpoints return forecast hours. Storing them would put predictions
    in the same column as observations with nothing to tell them apart."""
    now = int(time.time())
    rows = fetch_outdoor.fetch(40.71, -74.01, past_days=1)
    assert rows
    assert max(rows) <= now
    # The fixture offers two future hours; they must not be among them.
    assert len(rows) < len(upstream.times)


def test_coordinates_are_rounded_before_they_leave_the_house(upstream):
    """Two decimals is ~1km, and the model grid is coarser than that. A home
    address is not something to hand a third party more precisely than the
    answer needs."""
    fetch_outdoor.fetch(40.7127753, -74.0059728, past_days=1)
    assert upstream.requests
    for path in upstream.requests:
        assert "latitude=40.71" in path
        assert "longitude=-74.01" in path
        assert "40.7127" not in path


def test_a_variable_the_upstream_omits_becomes_null(monkeypatch, db):
    """The one failure mode that cannot be checked from the build sandbox: a
    variable name that is wrong. It has to land as an empty column -- visible,
    harmless, fixable -- and never as an hourly job that dies."""
    now = int(time.time())
    times = hours_around(now, past=2, future=0)
    n = len(times)
    weather = {"temperature_2m": [10.0] * n}          # the other two missing
    air = {"pm2_5": [5.0] * n}
    with FakeUpstream(weather, air, times) as server:
        monkeypatch.setattr(fetch_outdoor, "WEATHER_URL", server.base + "/v1/forecast")
        monkeypatch.setattr(
            fetch_outdoor, "AIR_QUALITY_URL", server.base + "/v1/air-quality"
        )
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        rows = fetch_outdoor.fetch(40.71, -74.01, past_days=1)
    ts = max(rows)
    assert rows[ts]["temp_c"] == 10.0
    assert rows[ts]["rh_pct"] is None
    assert rows[ts]["us_aqi"] is None


def test_one_endpoint_down_still_yields_the_other(monkeypatch, db):
    """Partial data beats no data, and the failure is reported on stderr rather
    than raised: half a reference line is still a reference line."""
    now = int(time.time())
    times = hours_around(now, past=2, future=0)
    weather = {"temperature_2m": [10.0] * len(times)}
    with FakeUpstream(weather, {}, times) as server:
        monkeypatch.setattr(fetch_outdoor, "WEATHER_URL", server.base + "/v1/forecast")
        # Nothing listening on this port.
        monkeypatch.setattr(fetch_outdoor, "AIR_QUALITY_URL", "http://127.0.0.1:1/x")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        rows = fetch_outdoor.fetch(40.71, -74.01, past_days=1)
    assert rows
    assert all(r["temp_c"] == 10.0 for r in rows.values())
    assert all(r["pm2_5_atm"] is None for r in rows.values())


def test_both_endpoints_down_is_an_error(monkeypatch):
    monkeypatch.setattr(fetch_outdoor, "WEATHER_URL", "http://127.0.0.1:1/a")
    monkeypatch.setattr(fetch_outdoor, "AIR_QUALITY_URL", "http://127.0.0.1:1/b")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with pytest.raises(fetch_outdoor.FetchError):
        fetch_outdoor.fetch(40.71, -74.01, past_days=1)


# Derived, not hand-computed: an epoch constant typed from memory is its own
# bug, and all four spellings below name the same instant.
SAME_INSTANT = calendar.timegm((2026, 9, 6, 22, 0, 0, 0, 0, 0))


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-06T22:00",        # naive: UTC, the upstream default
        "2026-09-06T22:00Z",       # Z, which fromisoformat rejects before 3.11
        "2026-09-06T22:00+00:00",
        "2026-09-06T18:00-04:00",  # an offset is honoured, not ignored
    ],
)
def test_hour_parsing(text):
    assert fetch_outdoor._parse_hour(text) == SAME_INSTANT


# --------------------------------------------------------------------- writing


def test_writes_and_then_revises(upstream, db):
    """The upstream revises recent hours as observations replace forecasts, so
    a re-fetch of an hour already held is a correction to take -- the opposite
    of readings, which drop duplicates."""
    rows = fetch_outdoor.fetch(40.71, -74.01, past_days=1)
    fetch_outdoor.write(db, rows, fetched_at=1000)
    first = outdoor_rows(db)
    assert first

    ts = first[0]["ts"]
    fetch_outdoor.write(db, {ts: dict(rows[ts], temp_c=99.0)}, fetched_at=2000)
    after = {r["ts"]: r for r in outdoor_rows(db)}
    assert after[ts]["temp_c"] == 99.0
    assert after[ts]["fetched_at"] == 2000
    assert len(after) == len(first), "a revision must not add a row"


def test_a_missing_value_never_erases_one_already_stored(upstream, db):
    """COALESCE in the upsert. If the air-quality endpoint is down on the hour
    that revises a row, the PM figures already held must survive it."""
    rows = fetch_outdoor.fetch(40.71, -74.01, past_days=1)
    fetch_outdoor.write(db, rows, fetched_at=1000)
    ts = min(rows)
    kept = {r["ts"]: r for r in outdoor_rows(db)}[ts]["pm2_5_atm"]
    assert kept is not None

    partial = dict.fromkeys(fetch_outdoor.COLUMNS, None)
    partial["temp_c"] = 42.0
    fetch_outdoor.write(db, {ts: partial}, fetched_at=2000)

    row = {r["ts"]: r for r in outdoor_rows(db)}[ts]
    assert row["temp_c"] == 42.0
    assert row["pm2_5_atm"] == kept


# ------------------------------------------------------------------ entry point


def test_unset_coordinates_is_success_not_failure(monkeypatch, capsys, db):
    """The timer is installed and enabled whether or not coordinates are set,
    so the unconfigured state is the normal one. A non-zero exit here would put
    a red unit on the box for a feature nobody turned on."""
    monkeypatch.delenv("ENVLOG_LAT", raising=False)
    monkeypatch.delenv("ENVLOG_LON", raising=False)
    assert fetch_outdoor.main(["--db", db]) == 0
    assert "disabled" in capsys.readouterr().out


def test_nonsense_coordinates_are_rejected(monkeypatch, db):
    monkeypatch.setenv("ENVLOG_LAT", "not-a-number")
    monkeypatch.setenv("ENVLOG_LON", "-74.01")
    assert fetch_outdoor.main(["--db", db]) == 2

    monkeypatch.setenv("ENVLOG_LAT", "140.0")
    assert fetch_outdoor.main(["--db", db]) == 2


def test_dry_run_writes_nothing(upstream, monkeypatch, capsys, db):
    """The command that checked the upstream contract on the Pi, and the one to
    re-run if a column ever goes empty."""
    monkeypatch.setenv("ENVLOG_LAT", "40.71")
    monkeypatch.setenv("ENVLOG_LON", "-74.01")
    assert fetch_outdoor.main(["--dry-run", "--db", db]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert outdoor_rows(db) == []


def test_main_writes(upstream, monkeypatch, db):
    monkeypatch.setenv("ENVLOG_LAT", "40.71")
    monkeypatch.setenv("ENVLOG_LON", "-74.01")
    assert fetch_outdoor.main(["--db", db]) == 0
    assert outdoor_rows(db)


def test_an_upstream_failure_exits_non_zero(monkeypatch, db):
    monkeypatch.setenv("ENVLOG_LAT", "40.71")
    monkeypatch.setenv("ENVLOG_LON", "-74.01")
    monkeypatch.setattr(fetch_outdoor, "WEATHER_URL", "http://127.0.0.1:1/a")
    monkeypatch.setattr(fetch_outdoor, "AIR_QUALITY_URL", "http://127.0.0.1:1/b")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    assert fetch_outdoor.main(["--db", db]) == 1
