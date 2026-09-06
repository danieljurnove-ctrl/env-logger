#!/usr/bin/env python3
"""Pull outdoor conditions into the `outdoor` table.

Run hourly by envlog-outdoor.timer. Source is Open-Meteo, which needs no API
key and no account -- the reason it was chosen over AirNow, PurpleAir or
OpenWeatherMap, all of which do. Two endpoints: weather for temperature,
humidity and pressure, air quality for PM and the US AQI.

WHAT THIS IS NOT
----------------
It is not a sensor on your porch. Open-Meteo serves a numerical model on a grid
of several kilometres, so this is the trend over your neighbourhood, not your
street. When it disagrees with a thermometer outside your window, the
thermometer is right. Its value here is comparative: whether the indoor number
is yours or the whole region's.

There is no outdoor CO2 in it either -- the air-quality model carries carbon
*monoxide*, a different gas. The 420 ppm baseline the decay fit uses is still
an assumption about the global background, not something measured here.

Standard library only, deliberately: this runs on a Raspberry Pi on an
unsupported OS, and adding `requests` to that box's venv buys nothing that
urllib does not already do.

Config comes from the environment, normally /etc/envlog/envlog.env:

    ENVLOG_LAT, ENVLOG_LON     required -- without them this exits quietly
    ENVLOG_DB                  database path
    ENVLOG_OUTDOOR_PAST_DAYS   how many days back to re-fetch (default 2)

Coordinates are your home address, so they live in that root-owned 0600 file
and never in the repository. They are also rounded to two decimals (~1 km)
before being sent: the model's own grid is coarser than that, so the rounding
costs no accuracy and hands a third party less than it asked for.

    python3 fetch_outdoor.py --dry-run    # print what would be written
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Our column <- their hourly variable name.
WEATHER_VARS = {
    "temp_c": "temperature_2m",
    "rh_pct": "relative_humidity_2m",
    "pressure_hpa": "surface_pressure",
}
AIR_QUALITY_VARS = {
    "pm2_5_atm": "pm2_5",
    "pm10_atm": "pm10",
    "us_aqi": "us_aqi",
}

COLUMNS = tuple(WEATHER_VARS) + tuple(AIR_QUALITY_VARS)

TIMEOUT_SECONDS = 20


class FetchError(RuntimeError):
    pass


def _parse_hour(value: str) -> int:
    """'2026-09-06T22:00' -> unix seconds.

    Times come back in UTC because no `timezone` parameter is sent and the
    upstream default is GMT. A naive string is therefore UTC; one carrying an
    offset is honoured on its own terms, so a future default change shifts
    nothing silently. The Z suffix is spelled out because
    datetime.fromisoformat only learned it in Python 3.11 and this may run on
    3.9.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _get_json(url: str, params: dict) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full, headers={"User-Agent": "envlog/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{url}: {exc}") from exc
    except ValueError as exc:
        raise FetchError(f"{url}: response was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FetchError(f"{url}: expected a JSON object")
    # Open-Meteo reports its own errors in a 400 body, which urllib raises on,
    # but check anyway rather than trusting the status alone.
    if payload.get("error"):
        raise FetchError(f"{url}: {payload.get('reason', 'upstream error')}")
    return payload


def _hourly_series(payload: dict, wanted: dict, url: str) -> dict[int, dict]:
    """Turn {hourly: {time: [...], var: [...]}} into {ts: {column: value}}.

    A variable the upstream did not return becomes None rather than an
    exception, so a name that goes wrong shows up as a column that stays
    empty -- visible, harmless, fixable -- and never as an hourly job that
    dies. All six names were confirmed against the live API on 2026-09-06
    (72 hours, every column populated), but they are upstream's to rename and
    this file cannot be recompiled when they do.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise FetchError(f"{url}: no 'hourly' object in the response")
    times = hourly.get("time")
    if not isinstance(times, list):
        raise FetchError(f"{url}: no 'hourly.time' array in the response")

    out: dict[int, dict] = {}
    for index, raw_time in enumerate(times):
        try:
            ts = _parse_hour(str(raw_time))
        except ValueError:
            continue
        row = {}
        for column, variable in wanted.items():
            values = hourly.get(variable)
            value = None
            if isinstance(values, list) and index < len(values):
                value = values[index]
            row[column] = float(value) if isinstance(value, (int, float)) else None
        out[ts] = row
    return out


def fetch(lat: float, lon: float, past_days: int) -> dict[int, dict]:
    """Both endpoints, merged by hour. Partial results are kept."""
    common = {
        # Two decimals is ~1.1km. The model grid is coarser, so this loses no
        # accuracy and shares no more of a home address than it has to.
        "latitude": f"{lat:.2f}",
        "longitude": f"{lon:.2f}",
        "past_days": str(past_days),
        # 1, not 0: the current hour lives at the near edge of the forecast,
        # and hours past now are dropped below rather than stored.
        "forecast_days": "1",
    }

    merged: dict[int, dict] = {}
    failures = []
    sources = (
        (WEATHER_URL, WEATHER_VARS),
        (AIR_QUALITY_URL, AIR_QUALITY_VARS),
    )
    for url, wanted in sources:
        params = dict(common, hourly=",".join(wanted.values()))
        try:
            series = _hourly_series(_get_json(url, params), wanted, url)
        except FetchError as exc:
            failures.append(str(exc))
            continue
        for ts, row in series.items():
            merged.setdefault(ts, {}).update(row)

    if not merged:
        raise FetchError("; ".join(failures) or "no data returned")
    for message in failures:
        print(f"warning: {message}", file=sys.stderr)

    # Never store an hour that has not happened. Both endpoints return forecast
    # hours past now, and writing those would put predictions in the same
    # column as observations with nothing to tell them apart afterwards.
    now = int(time.time())
    # Every column on every row, even when an endpoint failed: a caller that
    # has to know which of the two responses contributed a key is a caller
    # waiting to raise KeyError the first time one of them is down.
    return {
        ts: {c: row.get(c) for c in COLUMNS}
        for ts, row in merged.items()
        if ts <= now
    }


def write(db_path: str, rows: dict[int, dict], fetched_at: int) -> int:
    assignments = ", ".join(
        # COALESCE, so a variable missing from one response never erases a
        # value already stored from an earlier run that did have it. The
        # upstream revises hours; it does not un-know them.
        f"{c} = COALESCE(excluded.{c}, outdoor.{c})" for c in COLUMNS
    )
    sql = (
        f"INSERT INTO outdoor (ts, {', '.join(COLUMNS)}, fetched_at) "
        f"VALUES (?, {', '.join('?' * len(COLUMNS))}, ?) "
        f"ON CONFLICT(ts) DO UPDATE SET {assignments}, fetched_at = excluded.fetched_at"
    )
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        with conn:
            for ts in sorted(rows):
                row = rows[ts]
                conn.execute(
                    sql,
                    [ts] + [row.get(c) for c in COLUMNS] + [fetched_at],
                )
    finally:
        conn.close()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and print, write nothing -- how to check the upstream "
             "contract by hand before trusting the timer",
    )
    parser.add_argument("--db", default=os.environ.get("ENVLOG_DB",
                                                       "/var/lib/envlog/envlog.db"))
    args = parser.parse_args(argv)

    lat = os.environ.get("ENVLOG_LAT", "").strip()
    lon = os.environ.get("ENVLOG_LON", "").strip()
    if not lat or not lon:
        # Not an error. The timer is installed and enabled unconditionally so
        # that setting coordinates later needs no reinstall, which means the
        # unconfigured state is the normal one and must be silent.
        print("ENVLOG_LAT/ENVLOG_LON unset -- outdoor reference disabled")
        return 0
    try:
        latitude, longitude = float(lat), float(lon)
    except ValueError:
        print("ENVLOG_LAT/ENVLOG_LON must be decimal degrees", file=sys.stderr)
        return 2
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        print("ENVLOG_LAT/ENVLOG_LON out of range", file=sys.stderr)
        return 2

    try:
        past_days = int(os.environ.get("ENVLOG_OUTDOOR_PAST_DAYS", "2"))
    except ValueError:
        past_days = 2
    past_days = max(0, min(past_days, 7))

    try:
        rows = fetch(latitude, longitude, past_days)
    except FetchError as exc:
        # Exit non-zero so systemd records the failure, but say why in one
        # line: a gap in this table is tolerable, the same as a gap in
        # readings, and does not warrant a retry storm.
        print(f"outdoor fetch failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        for ts in sorted(rows):
            stamp = datetime.fromtimestamp(ts, timezone.utc).isoformat()
            values = ", ".join(f"{c}={rows[ts].get(c)}" for c in COLUMNS)
            print(f"{stamp}  {values}")
        print(f"{len(rows)} hour(s); nothing written (--dry-run)")
        return 0

    written = write(args.db, rows, int(time.time()))
    print(f"outdoor: {written} hour(s) up to date")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
