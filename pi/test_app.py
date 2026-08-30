"""Tests for the ingest service.

Run from the repo root::

    python -m pytest pi/test_app.py -q

The placement overlap check is the important one: docs/design.md states that
non-overlap is an invariant SQLite cannot enforce, so it is enforced here.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from app import ValidationError, connect, create_app

TOKEN = "test-token"
AUTH = {"X-Auth-Token": TOKEN}


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        db_path=str(tmp_path / "envlog.db"),
        token=TOKEN,
        flush_interval=3600,
        start_flusher=False,  # tests flush explicitly, so timing is not a variable
    )
    yield application
    application.buffer.stop()


@pytest.fixture()
def client(app):
    return app.test_client()


def rows(app):
    conn = connect(app.config["ENVLOG_DB"])
    try:
        return conn.execute("SELECT * FROM readings ORDER BY ts").fetchall()
    finally:
        conn.close()


# ----------------------------------------------------------------------- auth


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/"),
        ("get", "/api/status"),
        ("get", "/api/series"),
        ("get", "/placements"),
        ("post", "/ingest"),
        ("post", "/placements"),
        ("patch", "/placements/1"),
    ],
)
def test_every_endpoint_requires_the_token(client, method, path):
    """A tailnet is flat: the dashboard and read API need auth as much as /ingest."""
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_empty_token_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError, match="ENVLOG_TOKEN"):
        create_app(db_path=str(tmp_path / "x.db"), token="", start_flusher=False)


def test_oversized_body_is_rejected(client):
    big = {"node": "feather-01", "co2_ppm": 500, "boot_count": 1}
    payload = ("x" * 32_000)
    resp = client.post(
        "/ingest", headers=AUTH, data='{"node": "' + payload + '"}',
        content_type="application/json",
    )
    assert resp.status_code == 413
    assert big  # payload shape documented above


# --------------------------------------------------------------------- ingest


def test_reading_is_buffered_then_written(app, client):
    resp = client.post("/ingest", headers=AUTH, json={"node": "feather-01", "co2_ppm": 612})
    assert resp.status_code == 202
    assert rows(app) == []          # nothing written yet: batching is the point
    assert app.buffer.flush() == 1
    written = rows(app)
    assert len(written) == 1
    assert written[0]["co2_ppm"] == 612


def test_omitted_sensors_become_null(app, client):
    client.post("/ingest", headers=AUTH, json={"node": "feather-01", "co2_ppm": 612})
    app.buffer.flush()
    row = rows(app)[0]
    assert row["co2_ppm"] == 612
    assert row["pm2_5_atm"] is None
    assert row["bme_temp_c"] is None


def test_failed_sensor_nulls_only_its_own_column(app, client):
    """An implausible value must not cost the readings that came with it."""
    client.post(
        "/ingest",
        headers=AUTH,
        json={"node": "feather-01", "bme_temp_c": 21.4, "co2_ppm": 99999},
    )
    app.buffer.flush()
    row = rows(app)[0]
    assert row["co2_ppm"] is None
    assert row["bme_temp_c"] == 21.4


def test_pm_ordering_violation_drops_all_three(app, client):
    client.post(
        "/ingest",
        headers=AUTH,
        json={
            "node": "feather-01",
            "pm1_0_atm": 9.0,
            "pm2_5_atm": 4.0,
            "pm10_atm": 12.0,
            "co2_ppm": 600,
        },
    )
    app.buffer.flush()
    row = rows(app)[0]
    assert row["pm1_0_atm"] is None
    assert row["pm2_5_atm"] is None
    assert row["pm10_atm"] is None
    assert row["co2_ppm"] == 600


def test_unknown_field_is_rejected(client):
    resp = client.post(
        "/ingest", headers=AUTH, json={"node": "feather-01", "co2_ppmm": 600}
    )
    assert resp.status_code == 400
    assert "co2_ppmm" in resp.get_json()["error"]


def test_missing_node_is_rejected(client):
    assert client.post("/ingest", headers=AUTH, json={"co2_ppm": 600}).status_code == 400


def test_boolean_is_not_a_number(client):
    resp = client.post("/ingest", headers=AUTH, json={"node": "n", "co2_ppm": True})
    assert resp.status_code == 400


def test_duplicate_timestamp_does_not_raise(app):
    """A retry, or an NTP step backwards, must not take the whole batch down."""
    app.buffer.add("feather-01", 1000, {c: None for c in _all_columns()})
    app.buffer.add("feather-01", 1000, {c: None for c in _all_columns()})
    assert app.buffer.flush() == 2
    assert len(rows(app)) == 1


def _all_columns():
    from app import SENSOR_COLUMNS

    return SENSOR_COLUMNS + ("boot_count",)


def test_flush_failure_keeps_the_batch(app, monkeypatch):
    app.buffer.add("feather-01", 1000, {c: None for c in _all_columns()})
    monkeypatch.setattr(
        "app.connect", lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("nope"))
    )
    with pytest.raises(sqlite3.OperationalError):
        app.buffer.flush()
    assert app.buffer.pending_count() == 1


# ----------------------------------------------------------------- placements


def test_move_closes_the_previous_placement(app, client):
    first = client.post(
        "/placements", headers=AUTH,
        json={"node": "feather-01", "room": "living room", "start_ts": 1000},
    ).get_json()
    client.post(
        "/placements", headers=AUTH,
        json={"node": "feather-01", "room": "bedroom", "start_ts": 2000},
    )
    conn = connect(app.config["ENVLOG_DB"])
    try:
        row = conn.execute(
            "SELECT end_ts FROM placements WHERE id = ?", (first["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["end_ts"] == 2000


def test_placements_never_overlap(app, client):
    """The invariant SQLite cannot express. Enforced by the move endpoint."""
    for i, room in enumerate(["living room", "bedroom", "kitchen", "study"]):
        client.post(
            "/placements", headers=AUTH,
            json={"node": "feather-01", "room": room, "start_ts": 1000 + i * 1000},
        )
    conn = connect(app.config["ENVLOG_DB"])
    try:
        placements = conn.execute(
            "SELECT start_ts, end_ts FROM placements ORDER BY start_ts"
        ).fetchall()
    finally:
        conn.close()
    assert len(placements) == 4
    for earlier, later in zip(placements, placements[1:]):
        assert earlier["end_ts"] is not None, "only the newest placement stays open"
        assert earlier["end_ts"] <= later["start_ts"]
    assert placements[-1]["end_ts"] is None


def test_retroactive_correction(client):
    created = client.post(
        "/placements", headers=AUTH,
        json={"node": "feather-01", "room": "bedroom", "start_ts": 1000},
    ).get_json()
    resp = client.patch(
        f"/placements/{created['id']}", headers=AUTH, json={"room": "study"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["room"] == "study"


def test_patch_rejects_inverted_interval(client):
    created = client.post(
        "/placements", headers=AUTH,
        json={"node": "feather-01", "room": "bedroom", "start_ts": 1000},
    ).get_json()
    resp = client.patch(
        f"/placements/{created['id']}", headers=AUTH, json={"end_ts": 500}
    )
    assert resp.status_code == 400


def test_patch_unknown_placement_is_404(client):
    assert client.patch("/placements/999", headers=AUTH, json={"room": "x"}).status_code == 404


# --------------------------------------------------------------------- series


def _seed(app, client, samples, placements=()):
    for room, start in placements:
        client.post(
            "/placements", headers=AUTH,
            json={"node": "feather-01", "room": room, "start_ts": start},
        )
    for ts, co2 in samples:
        values = {c: None for c in _all_columns()}
        values["co2_ppm"] = co2
        app.buffer.add("feather-01", ts, values)
    app.buffer.flush()


def test_series_segments_at_a_move(app, client):
    """A move breaks the line: one segment per placement, never a slope across."""
    _seed(
        app, client,
        samples=[(1100, 500), (1200, 510), (2100, 800), (2200, 810)],
        placements=[("living room", 1000), ("bedroom", 2000)],
    )
    data = client.get(
        "/api/series?from=0&to=9999&metrics=co2_ppm", headers=AUTH
    ).get_json()
    assert [s["location"] for s in data["segments"]] == ["living room", "bedroom"]
    assert [len(s["rows"]) for s in data["segments"]] == [2, 2]


def test_unlabelled_readings_are_unknown_not_dropped(app, client):
    _seed(app, client, samples=[(100, 400), (200, 410)])
    data = client.get("/api/series?from=0&to=9999", headers=AUTH).get_json()
    assert data["segments"][0]["location"] == "Unknown"
    assert len(data["segments"][0]["rows"]) == 2


def test_bucketing_averages(app, client):
    _seed(app, client, samples=[(1000, 400), (1010, 500), (1100, 600)])
    data = client.get(
        "/api/series?from=0&to=9999&bucket=100", headers=AUTH
    ).get_json()
    rows_ = data["segments"][0]["rows"]
    assert rows_ == [[1000, 450.0], [1100, 600.0]]


def test_day_bucket_uses_the_requested_timezone(app, client):
    """07:00 UTC is the previous local day in Los Angeles; the fold must respect it."""
    # 2026-01-02T07:00:00Z == 2026-01-01T23:00 America/Los_Angeles
    _seed(app, client, samples=[(1767337200, 500), (1767366000, 700)])
    data = client.get(
        "/api/series?from=1767200000&to=1767500000&bucket=day&tz=America/Los_Angeles",
        headers=AUTH,
    ).get_json()
    day_starts = [r[0] for r in data["segments"][0]["rows"]]
    assert len(day_starts) == 2, "the two samples fall on different local days"
    assert day_starts == sorted(day_starts)


def test_unknown_metric_is_rejected(client):
    client.post("/ingest", headers=AUTH, json={"node": "feather-01", "co2_ppm": 1})
    resp = client.get("/api/series?metrics=co2_ppm;DROP", headers=AUTH)
    assert resp.status_code == 400


def test_unknown_timezone_is_rejected(app, client):
    _seed(app, client, samples=[(1000, 400)])
    resp = client.get("/api/series?tz=Mars/Olympus", headers=AUTH)
    assert resp.status_code == 400


# --------------------------------------------------------------------- status


def test_status_reports_liveness_and_placement(app, client):
    _seed(app, client, samples=[(int(time.time()) - 30, 500)],
          placements=[("kitchen", 1000)])
    data = client.get("/api/status", headers=AUTH).get_json()
    assert data["node"] == "feather-01"
    assert 0 <= data["seconds_since_last"] < 120
    assert data["placement"]["room"] == "kitchen"


def test_status_on_a_fresh_install_is_not_an_error(client):
    """The endpoint you curl to ask 'has anything arrived yet?' must answer."""
    resp = client.get("/api/status", headers=AUTH)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["node"] is None
    assert body["last_ts"] is None
