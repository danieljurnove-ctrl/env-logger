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
        ("get", "/markers"),
        ("post", "/markers"),
        ("patch", "/markers/1"),
        ("delete", "/markers/1"),
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


def test_counts_are_stored_alongside_mass(app, client):
    """The counts are the point: mass rounds to 0 indoors while these move."""
    client.post(
        "/ingest",
        headers=AUTH,
        json={
            "node": "feather-01",
            "pm1_0_atm": 0.0, "pm2_5_atm": 0.0, "pm10_atm": 0.0,
            "pm0_3_count": 126.0, "pm0_5_count": 112.0, "pm1_0_count": 24.0,
            "pm2_5_count": 2.0, "pm5_0_count": 0.0, "pm10_count": 0.0,
        },
    )
    app.buffer.flush()
    row = rows(app)[0]
    assert row["pm2_5_atm"] == 0.0
    assert row["pm0_3_count"] == 126.0
    assert row["pm2_5_count"] == 2.0


def test_count_ordering_violation_drops_all_six(app, client):
    """Counts are cumulative, so they can only decrease with size.

    0.5um reading higher than 0.3um is physically impossible -- every particle
    counted at 0.5um was already counted at 0.3um -- so the frame is untrusted
    and the whole set goes, exactly as the mass triple does.
    """
    client.post(
        "/ingest",
        headers=AUTH,
        json={
            "node": "feather-01",
            "pm0_3_count": 10.0, "pm0_5_count": 99.0, "pm1_0_count": 5.0,
            "pm2_5_count": 2.0, "pm5_0_count": 1.0, "pm10_count": 0.0,
            "co2_ppm": 600.0,
        },
    )
    app.buffer.flush()
    row = rows(app)[0]
    for column in ("pm0_3_count", "pm0_5_count", "pm1_0_count",
                   "pm2_5_count", "pm5_0_count", "pm10_count"):
        assert row[column] is None, column
    # The rest of the reading survives: one bad sensor never drops the row.
    assert row["co2_ppm"] == 600.0


def test_equal_counts_are_valid(app, client):
    """Non-increasing, not strictly decreasing -- clean air reads 0, 0, 0."""
    client.post(
        "/ingest",
        headers=AUTH,
        json={
            "node": "feather-01",
            "pm0_3_count": 5.0, "pm0_5_count": 5.0, "pm1_0_count": 0.0,
            "pm2_5_count": 0.0, "pm5_0_count": 0.0, "pm10_count": 0.0,
        },
    )
    app.buffer.flush()
    row = rows(app)[0]
    assert row["pm0_3_count"] == 5.0
    assert row["pm10_count"] == 0.0


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


# ------------------------------------------------------- browser auth + assets


def test_root_without_a_token_offers_the_login_form(client):
    resp = client.get("/")
    assert resp.status_code == 401
    assert b"<form" in resp.data and b"/login" in resp.data


def test_login_with_the_wrong_token_is_rejected(client):
    resp = client.post("/login", data={"token": "nope"})
    assert resp.status_code == 401
    assert b"not accepted" in resp.data


def test_login_sets_a_cookie_and_the_dashboard_then_loads(client):
    resp = client.post("/login", data={"token": TOKEN})
    assert resp.status_code == 302
    assert "envlog_token" in resp.headers.get("Set-Cookie", "")
    page = client.get("/")           # cookie is on the test client now
    assert page.status_code == 200
    assert b"uPlot.iife.min.js" in page.data


def test_token_in_the_query_string_is_exchanged_for_a_cookie(client):
    """It must not stay in the URL, where history and proxy logs would keep it."""
    resp = client.get(f"/?token={TOKEN}")
    assert resp.status_code == 302
    assert resp.headers["Location"] in ("/", "http://localhost/")
    assert "envlog_token" in resp.headers.get("Set-Cookie", "")


def test_static_assets_are_not_an_unauthenticated_back_door(client):
    assert client.get("/static/vendor/uPlot.min.css").status_code == 401
    client.post("/login", data={"token": TOKEN})
    assert client.get("/static/vendor/uPlot.min.css").status_code == 200


def test_rooms_lists_the_preset_names(client):
    client.post("/placements", headers=AUTH, json={"node": "feather-01", "room": "study"})
    body = client.get("/rooms", headers=AUTH).get_json()
    assert [r["name"] for r in body["rooms"]] == ["study"]


# -------------------------------------------------------------------- markers


def _mark(client, label, ts=None):
    body = {"label": label}
    if ts is not None:
        body["ts"] = ts
    res = client.post("/markers", json=body, headers=AUTH)
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def test_marker_round_trips(client):
    created = _mark(client, "opened windows", ts=1_700_000_000)
    assert created["ts"] == 1_700_000_000
    assert created["label"] == "opened windows"
    listed = client.get("/markers", headers=AUTH).get_json()["markers"]
    assert listed == [created]


def test_markers_come_back_newest_first(client):
    _mark(client, "left house", ts=1_700_000_000)
    _mark(client, "came back", ts=1_700_003_600)
    labels = [m["label"] for m in client.get("/markers", headers=AUTH).get_json()["markers"]]
    assert labels == ["came back", "left house"]


def test_ts_defaults_to_now(client):
    before = int(time.time())
    created = _mark(client, "cooked with fan on")
    assert before <= created["ts"] <= int(time.time())


def test_label_is_trimmed_and_required(client):
    assert _mark(client, "  opened windows  ")["label"] == "opened windows"
    for bad in ({"label": "   "}, {"label": ""}, {"label": 7}, {"ts": 1}):
        assert client.post("/markers", json=bad, headers=AUTH).status_code == 400


def test_marker_rejects_unknown_field(client):
    res = client.post(
        "/markers", json={"label": "x", "room": "kitchen"}, headers=AUTH
    )
    assert res.status_code == 400
    assert "room" in res.get_json()["error"]


def test_marker_rejects_boolean_ts(client):
    """bool is an int in Python; a marker at 'True' would be epoch 1970."""
    assert client.post(
        "/markers", json={"label": "x", "ts": True}, headers=AUTH
    ).status_code == 400


def test_marker_is_editable(client):
    created = _mark(client, "opend windows", ts=1_700_000_000)
    res = client.patch(
        f"/markers/{created['id']}",
        json={"label": "opened windows", "ts": 1_700_000_060},
        headers=AUTH,
    )
    assert res.status_code == 200
    assert res.get_json() == {
        "id": created["id"], "ts": 1_700_000_060, "label": "opened windows",
    }


def test_patch_one_field_leaves_the_other(client):
    created = _mark(client, "left house", ts=1_700_000_000)
    res = client.patch(f"/markers/{created['id']}", json={"ts": 1}, headers=AUTH)
    assert res.get_json() == {"id": created["id"], "ts": 1, "label": "left house"}


def test_empty_patch_is_rejected(client):
    created = _mark(client, "left house")
    assert client.patch(
        f"/markers/{created['id']}", json={}, headers=AUTH
    ).status_code == 400


def test_marker_is_deletable(client):
    created = _mark(client, "misclick", ts=1_700_000_000)
    assert client.delete(f"/markers/{created['id']}", headers=AUTH).status_code == 204
    assert client.get("/markers", headers=AUTH).get_json()["markers"] == []


def test_unknown_marker_is_404(client):
    assert client.patch("/markers/999", json={"label": "x"}, headers=AUTH).status_code == 404
    assert client.delete("/markers/999", headers=AUTH).status_code == 404
