#!/usr/bin/env python3
"""A fake feather-01, so the Pi side can be finished before any hardware exists.

It reproduces the parts of the node -> server contract that the service has to
cope with, rather than posting tidy complete rows:

  * only sensors with a fresh reading are sent, so PM is absent from ~90% of
    posts and arrives as NULL -- carrying values forward would fabricate 85% of
    the PM series
  * boot_count increments across simulated reboots, which is what drives move
    detection
  * sensors drop out occasionally, NULLing their own columns only

Two modes, because the server assigns timestamps at one-second resolution:

  * **live** (default, and --fast) posts over HTTP like the real node. --fast
    still spaces posts one second apart -- any faster and every reading lands on
    the same ts, collides on PRIMARY KEY (node_id, ts), and ON CONFLICT DO
    NOTHING quietly collapses the lot into a single row.
  * **backfill** writes historical readings straight into the database, spaced at
    the real 45-second cadence. This is the one for building the dashboard
    against, since a day of history takes a moment instead of a day. It is a
    fixture generator, not a node: it does not go through /ingest, and nothing
    else in the system may write timestamps this way.

Usage::

    ENVLOG_TOKEN=... python pi/simulate_node.py --url http://localhost:8000
    ENVLOG_TOKEN=... python pi/simulate_node.py --fast --count 60
    python pi/simulate_node.py --backfill-hours 48 --db /var/lib/envlog/envlog.db
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request

PM_EVERY = 5 * 60           # duty-cycled to ~10% of runtime; see docs/hardware.md
SAMPLE_INTERVAL = 45.0
DROPOUT_CHANCE = 0.02       # an I2C timeout NULLs that sensor, not the row


def _drift(base: float, t: float, amplitude: float, period: float) -> float:
    return base + amplitude * math.sin(2 * math.pi * t / period) + random.gauss(0, 0.15)


def build_reading(t: float, boot_count: int, send_pm: bool) -> dict:
    """One POST body. Absent keys are the point, not an omission."""
    body: dict[str, object] = {"node": "feather-01", "boot_count": boot_count}

    if random.random() > DROPOUT_CHANCE:
        body["bme_temp_c"] = round(_drift(21.0, t, 2.0, 86400), 2)
        body["bme_rh_pct"] = round(_drift(44.0, t, 6.0, 86400), 2)
        body["pressure_hpa"] = round(_drift(1013.0, t, 4.0, 172800), 2)

    if random.random() > DROPOUT_CHANCE:
        body["scd_temp_c"] = round(_drift(21.6, t, 2.0, 86400), 2)
        body["scd_rh_pct"] = round(_drift(43.0, t, 6.0, 86400), 2)
        # CO2 rises overnight in a closed room and falls when it is aired.
        body["co2_ppm"] = round(max(410.0, _drift(750.0, t, 320.0, 86400)), 1)

    if send_pm and random.random() > DROPOUT_CHANCE:
        pm1 = max(0.0, _drift(4.0, t, 2.5, 43200))
        pm25 = pm1 + abs(random.gauss(1.8, 0.8))
        pm10 = pm25 + abs(random.gauss(2.4, 1.0))
        body["pm1_0_atm"] = round(pm1, 1)
        body["pm2_5_atm"] = round(pm25, 1)
        body["pm10_atm"] = round(pm10, 1)

        # Counts per 0.1 L, cumulative and therefore non-increasing with size.
        # Generated from the mass figures rather than independently, so the
        # simulator cannot emit a combination the real sensor never would --
        # and so the server's ordering check sees valid data here and only
        # fires on genuinely bad frames.
        c0_3 = 40 + pm25 * 55 + abs(random.gauss(0, 12))
        c0_5 = c0_3 * random.uniform(0.75, 0.92)
        c1_0 = c0_5 * random.uniform(0.15, 0.30)
        c2_5 = c1_0 * random.uniform(0.04, 0.14)
        c5_0 = c2_5 * random.uniform(0.0, 0.40)
        c10 = c5_0 * random.uniform(0.0, 0.50)
        body["pm0_3_count"] = float(round(c0_3))
        body["pm0_5_count"] = float(round(c0_5))
        body["pm1_0_count"] = float(round(c1_0))
        body["pm2_5_count"] = float(round(c2_5))
        body["pm5_0_count"] = float(round(c5_0))
        body["pm10_count"] = float(round(c10))

    return body


def post(url: str, token: str, body: dict, timeout: float = 5.0) -> int:
    request = urllib.request.Request(
        url.rstrip("/") + "/ingest",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Auth-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=0, help="0 = run forever")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="post once a second (the floor, given second-resolution timestamps) "
             "with PM every 7th post",
    )
    parser.add_argument(
        "--backfill-hours",
        type=float,
        default=0.0,
        help="write this many hours of history directly to --db and exit",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("ENVLOG_DB", "/var/lib/envlog/envlog.db"),
        help="database to backfill into (ignored unless --backfill-hours)",
    )
    parser.add_argument(
        "--reboot-every",
        type=int,
        default=0,
        help="simulate a reboot (boot_count increment) every N posts; 0 = never",
    )
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    if args.seed is not None:
        random.seed(args.seed)
    if args.backfill_hours > 0:
        return backfill(args.db, args.backfill_hours)

    token = os.environ.get("ENVLOG_TOKEN")
    if not token:
        print("ENVLOG_TOKEN is not set", file=sys.stderr)
        return 2
    boot_count = 1
    sent = 0
    last_pm = -PM_EVERY
    start = time.time()

    while args.count == 0 or sent < args.count:
        now = time.time()
        virtual = (sent * SAMPLE_INTERVAL) if args.fast else (now - start)
        send_pm = (sent % 7 == 0) if args.fast else (virtual - last_pm >= PM_EVERY)
        if send_pm:
            last_pm = virtual

        if args.reboot_every and sent and sent % args.reboot_every == 0:
            boot_count += 1

        body = build_reading(virtual, boot_count, send_pm)
        try:
            status = post(args.url, token, body)
        except urllib.error.HTTPError as exc:
            print(f"HTTP {exc.code}: {exc.read()[:200]!r}", file=sys.stderr)
            return 1
        except urllib.error.URLError as exc:
            # The real node loses readings during an outage rather than buffering
            # them, so the simulator does too.
            print(f"unreachable ({exc.reason}); dropping this reading", file=sys.stderr)
            status = 0

        sent += 1
        if sent % 10 == 0 or args.count:
            print(f"{sent} posted (last status {status}, boot_count {boot_count})")
        # 1.05s rather than 1.0s in fast mode: sleeping exactly one second can
        # still land two posts in the same integer second.
        time.sleep(1.05 if args.fast else SAMPLE_INTERVAL)

    return 0


def backfill(db_path: str, hours: float) -> int:
    """Write history straight to the database, bypassing /ingest.

    Deliberately not an HTTP path. docs/design.md rules out client-supplied
    timestamps for the node, and that stands -- this exists only so the dashboard
    has something to draw before the hardware exists.
    """
    import sqlite3

    if not os.path.exists(db_path):
        print(f"no database at {db_path}; start the service once first", file=sys.stderr)
        return 2

    now = int(time.time())
    start = now - int(hours * 3600)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            row = conn.execute(
                "SELECT id FROM nodes WHERE name = ?", ("feather-01",)
            ).fetchone()
            node_id = row[0] if row else conn.execute(
                "INSERT INTO nodes (name) VALUES (?)", ("feather-01",)
            ).lastrowid

            columns = (
                "node_id", "ts", "bme_temp_c", "bme_rh_pct", "pressure_hpa",
                "scd_temp_c", "scd_rh_pct", "co2_ppm",
                "pm1_0_atm", "pm2_5_atm", "pm10_atm",
                "pm0_3_count", "pm0_5_count", "pm1_0_count",
                "pm2_5_count", "pm5_0_count", "pm10_count", "boot_count",
            )
            rows, written, last_pm = [], 0, start - PM_EVERY
            for ts in range(start, now, int(SAMPLE_INTERVAL)):
                send_pm = ts - last_pm >= PM_EVERY
                if send_pm:
                    last_pm = ts
                body = build_reading(float(ts), 1, send_pm)
                rows.append(
                    (node_id, ts) + tuple(body.get(c) for c in columns[2:-1])
                    + (body.get("boot_count"),)
                )
                written += 1
            conn.executemany(
                f"INSERT INTO readings ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))}) ON CONFLICT DO NOTHING",
                rows,
            )
    finally:
        conn.close()
    print(f"backfilled {written} readings covering {hours}h into {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
