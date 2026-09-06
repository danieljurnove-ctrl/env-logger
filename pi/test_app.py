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
        ("get", "/api/candidate-moves"),
        ("get", "/api/export"),
        ("get", "/api/compare"),
        ("get", "/api/decay"),
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


# ------------------------------------------------------------ candidate moves


def _readings(app, samples):
    """Write rows directly: the buffer stamps ts itself, and these tests need
    control over the gaps."""
    conn = connect(app.config["ENVLOG_DB"])
    try:
        conn.execute("INSERT OR IGNORE INTO nodes (id, name) VALUES (1, 'feather-01')")
        for ts, boot in samples:
            conn.execute(
                "INSERT INTO readings (node_id, ts, co2_ppm, boot_count) "
                "VALUES (1, ?, 600, ?)",
                (ts, boot),
            )
        conn.commit()
    finally:
        conn.close()


def test_reboot_after_a_gap_is_a_candidate_move(app, client):
    _readings(app, [(1000, 1), (1045, 1), (1600, 2), (1645, 2)])
    got = client.get("/api/candidate-moves", headers=AUTH).get_json()["candidates"]
    assert [c["ts"] for c in got] == [1600]
    assert got[0]["gap_seconds"] == 555


def test_reboot_without_a_gap_is_not_a_move(app, client):
    """An OTA update or a power blip reboots without going anywhere."""
    _readings(app, [(1000, 1), (1045, 2), (1090, 2)])
    assert client.get("/api/candidate-moves", headers=AUTH).get_json()["candidates"] == []


def test_gap_without_a_reboot_is_not_a_move(app, client):
    """WiFi outage: readings stop and resume, but the node never power-cycled."""
    _readings(app, [(1000, 1), (9000, 1)])
    assert client.get("/api/candidate-moves", headers=AUTH).get_json()["candidates"] == []


def test_already_recorded_move_is_not_offered_again(app, client):
    _readings(app, [(1000, 1), (1045, 1), (1600, 2), (1645, 2)])
    assert len(client.get("/api/candidate-moves", headers=AUTH).get_json()["candidates"]) == 1
    client.post(
        "/placements",
        json={"node": "feather-01", "room": "bedroom", "start_ts": 1500},
        headers=AUTH,
    )
    assert client.get("/api/candidate-moves", headers=AUTH).get_json()["candidates"] == []


def test_candidate_moves_on_a_fresh_install(client):
    assert client.get("/api/candidate-moves", headers=AUTH).get_json()["candidates"] == []


# -------------------------------------------------------------------- export


def test_export_is_csv_with_a_header_and_rooms(app, client):
    client.post("/ingest", headers=AUTH, json={"node": "feather-01", "co2_ppm": 612})
    app.buffer.flush()
    client.post("/placements", json={"node": "feather-01", "room": "office"}, headers=AUTH)

    res = client.get("/api/export", headers=AUTH)
    assert res.status_code == 200
    assert res.mimetype == "text/csv"
    assert "attachment" in res.headers["Content-Disposition"]
    lines = res.get_data(as_text=True).strip().splitlines()
    assert lines[0].split(",")[:4] == ["ts", "iso_utc", "room", "bme_temp_c"]
    assert len(lines) == 2
    assert "612" in lines[1]


def test_export_respects_the_range(app, client):
    _readings(app, [(1000, 1), (2000, 1), (3000, 1)])
    body = client.get("/api/export?from=1500&to=2500", headers=AUTH).get_data(as_text=True)
    rows = body.strip().splitlines()[1:]
    assert [r.split(",")[0] for r in rows] == ["2000"]


def test_export_rejects_a_non_integer_bound(app, client):
    _readings(app, [(1000, 1)])
    assert client.get("/api/export?from=yesterday", headers=AUTH).status_code == 400


def test_export_on_a_fresh_install_is_empty_not_an_error(client):
    res = client.get("/api/export", headers=AUTH)
    assert res.status_code == 200
    assert res.get_data(as_text=True) == ""


# ------------------------------------------------------------------- compare


def _values(app, column, points):
    """Write (ts, value) rows for one column directly."""
    conn = connect(app.config["ENVLOG_DB"])
    try:
        conn.execute("INSERT OR IGNORE INTO nodes (id, name) VALUES (1, 'feather-01')")
        for ts, v in points:
            conn.execute(
                f"INSERT INTO readings (node_id, ts, {column}) VALUES (1, ?, ?)",
                (ts, v),
            )
        conn.commit()
    finally:
        conn.close()


def test_compare_summarises_each_window_from_raw_rows(app, client):
    _values(app, "co2_ppm", [(1000 + i, float(v))
                             for i, v in enumerate([400, 500, 600, 700, 5000])])
    _values(app, "co2_ppm", [(2000 + i, 800.0) for i in range(4)])
    res = client.get(
        "/api/compare?a_from=900&a_to=1100&b_from=1900&b_to=2100", headers=AUTH
    ).get_json()
    a = res["a"]["metrics"]["co2_ppm"]
    assert a["n"] == 5
    assert a["min"] == 400 and a["max"] == 5000
    assert a["median"] == 600
    # p95 of five readings is the largest -- an observed value, not an
    # interpolation between two of them.
    assert a["p95"] == 5000
    assert res["b"]["metrics"]["co2_ppm"] == {
        "n": 4, "mean": 800.0, "median": 800.0, "p95": 800.0,
        "min": 800.0, "max": 800.0,
    }


def test_compare_reports_the_rooms_each_window_covers(app, client):
    _values(app, "co2_ppm", [(1000, 500.0), (3000, 500.0)])
    client.post("/placements", json={"node": "feather-01", "room": "office",
                                     "start_ts": 900}, headers=AUTH)
    client.post("/placements", json={"node": "feather-01", "room": "bedroom",
                                     "start_ts": 2000}, headers=AUTH)
    res = client.get(
        "/api/compare?a_from=900&a_to=1500&b_from=2500&b_to=3500", headers=AUTH
    ).get_json()
    assert res["a"]["rooms"] == ["office"]
    assert res["b"]["rooms"] == ["bedroom"]


def test_compare_flags_a_window_that_straddles_a_move(app, client):
    """Comparing a window that spans two rooms is comparing a room to itself."""
    _values(app, "co2_ppm", [(1000, 500.0), (3000, 500.0)])
    client.post("/placements", json={"node": "feather-01", "room": "office",
                                     "start_ts": 900}, headers=AUTH)
    client.post("/placements", json={"node": "feather-01", "room": "bedroom",
                                     "start_ts": 2000}, headers=AUTH)
    res = client.get(
        "/api/compare?a_from=900&a_to=3500&b_from=2500&b_to=3500", headers=AUTH
    ).get_json()
    assert res["a"]["rooms"] == ["bedroom", "office"]


def test_compare_validation(app, client):
    _values(app, "co2_ppm", [(1000, 500.0)])
    for qs in (
        "a_from=1&a_to=2",                       # b missing entirely
        "a_from=2&a_to=1&b_from=1&b_to=2",       # a inverted
        "a_from=1&a_to=2&b_from=1&b_to=2&metrics=nope",
    ):
        assert client.get(f"/api/compare?{qs}", headers=AUTH).status_code == 400


# --------------------------------------------------------------------- decay


def _exponential(start_ts, count, step, baseline, amplitude, per_hour):
    import math as _m
    return [
        (start_ts + i * step,
         baseline + amplitude * _m.exp(-per_hour / 3600.0 * i * step))
        for i in range(count)
    ]


def test_decay_recovers_a_known_rate(app, client):
    """The whole point: a CO2 curve becomes air changes per hour."""
    _values(app, "co2_ppm",
            _exponential(10_000, 60, 60, 420.0, 600.0, per_hour=0.5))
    res = client.get(
        "/api/decay?metric=co2_ppm&w_from=9000&w_to=20000", headers=AUTH
    ).get_json()
    assert res["fitted"] is True
    assert abs(res["rate_per_hour"] - 0.5) < 0.01
    assert res["r2"] > 0.999
    assert res["baseline"] == 420.0          # outdoor floor, not zero
    assert abs(res["half_life_minutes"] - 83.2) < 1.0


def test_a_wrong_baseline_is_wrong_but_still_fits_beautifully(app, client):
    """The trap this endpoint sets, pinned so nobody removes the warning.

    Fitting CO2 against zero instead of the outdoor floor understates the rate
    by about 20% -- and r^2 stays above 0.999 while it does. r^2 measures
    whether the curve is exponential, NOT whether the baseline you subtracted
    was the right one, so it cannot catch this. The number looks confident and
    is wrong, which is why the default baseline is 420 rather than 0 and why
    the dashboard states the baseline next to the answer.
    """
    _values(app, "co2_ppm",
            _exponential(10_000, 60, 60, 420.0, 600.0, per_hour=0.5))
    right = client.get(
        "/api/decay?metric=co2_ppm&w_from=9000&w_to=20000", headers=AUTH
    ).get_json()
    wrong = client.get(
        "/api/decay?metric=co2_ppm&w_from=9000&w_to=20000&baseline=0", headers=AUTH
    ).get_json()
    assert abs(right["rate_per_hour"] - 0.5) < 0.01
    assert wrong["rate_per_hour"] < 0.42          # materially understated
    assert wrong["r2"] > 0.99                     # and r^2 does not notice


def test_decay_refuses_a_rising_window(app, client):
    _values(app, "co2_ppm", [(1000 + i * 60, 500.0 + i * 10) for i in range(20)])
    res = client.get(
        "/api/decay?metric=co2_ppm&w_from=900&w_to=3000", headers=AUTH
    ).get_json()
    assert res["fitted"] is False
    assert "rises" in res["reason"]


def test_decay_refuses_too_few_points(app, client):
    _values(app, "co2_ppm", [(1000, 900.0), (1060, 800.0)])
    res = client.get(
        "/api/decay?metric=co2_ppm&w_from=900&w_to=2000", headers=AUTH
    ).get_json()
    assert res["fitted"] is False
    assert res["n"] == 2


def test_decay_rejects_an_unknown_metric(app, client):
    assert client.get(
        "/api/decay?metric=nope&w_from=1&w_to=2", headers=AUTH
    ).status_code == 400


# ------------------------------------------------------------------- outdoor


def _seed_outdoor(app, hours):
    """hours: {ts: {column: value}}. Written straight in, as the timer does."""
    conn = connect(app.config["ENVLOG_DB"])
    try:
        with conn:
            for ts, values in hours.items():
                conn.execute(
                    "INSERT INTO outdoor (ts, temp_c, rh_pct, pressure_hpa, "
                    "pm2_5_atm, pm10_atm, us_aqi, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        values.get("temp_c"), values.get("rh_pct"),
                        values.get("pressure_hpa"), values.get("pm2_5_atm"),
                        values.get("pm10_atm"), values.get("us_aqi"),
                        ts,
                    ),
                )
    finally:
        conn.close()


def test_outdoor_is_null_when_nothing_pairs(app, client):
    """CO2 has no outdoor counterpart -- the upstream carries carbon monoxide,
    a different gas -- so the answer must be null rather than an empty object.
    The dashboard tells 'nothing to compare against' from 'comparable, but the
    fetcher is off'."""
    _seed(app, client, samples=[(1100, 500)])
    data = client.get(
        "/api/series?from=0&to=9999&metrics=co2_ppm", headers=AUTH
    ).get_json()
    assert data["outdoor"] is None


def test_both_temperature_sensors_share_one_outdoor_column(app, client):
    """Two sensors indoors, one air outside. Selecting temp_c twice would
    duplicate the line and, worse, hand uPlot more columns than series."""
    _seed(app, client, samples=[(1100, 500)])
    _seed_outdoor(app, {1000: {"temp_c": 9.0}})
    data = client.get(
        "/api/series?from=0&to=9999&metrics=bme_temp_c,scd_temp_c", headers=AUTH
    ).get_json()
    assert data["outdoor"]["columns"] == ["ts", "temp_c"]
    assert data["outdoor"]["pairs"] == {
        "bme_temp_c": "temp_c", "scd_temp_c": "temp_c",
    }


def test_the_hour_before_the_window_is_included(app, client):
    """Outdoor data is hourly and step-held, so without the row at or before
    `from` the reference line starts up to an hour into the chart."""
    _seed(app, client, samples=[(7300, 500)])
    _seed_outdoor(app, {
        3600: {"pm2_5_atm": 4.0},   # before the window
        7200: {"pm2_5_atm": 6.0},   # inside it
        14400: {"pm2_5_atm": 9.0},  # after it
    })
    data = client.get(
        "/api/series?from=7000&to=10000&metrics=pm2_5_atm", headers=AUTH
    ).get_json()
    stamps = [r[0] for r in data["outdoor"]["rows"]]
    assert stamps == [3600, 7200], "the hour before the window must come too"


def test_outdoor_rows_are_empty_when_the_fetcher_never_ran(app, client):
    """The default state. Comparable metric, no data -- not an error."""
    _seed(app, client, samples=[(1100, 500)])
    data = client.get(
        "/api/series?from=0&to=9999&metrics=pm2_5_atm", headers=AUTH
    ).get_json()
    assert data["outdoor"]["rows"] == []


def test_status_reports_outdoor_freshness(app, client):
    """A separate timer can die without the service noticing, and the charts
    would just quietly stop having a reference line."""
    _seed(app, client, samples=[(1100, 500)])
    assert client.get("/api/status", headers=AUTH).get_json()["outdoor_last_ts"] is None
    _seed_outdoor(app, {3600: {"temp_c": 9.0}, 7200: {"temp_c": 10.0}})
    assert client.get("/api/status", headers=AUTH).get_json()["outdoor_last_ts"] == 7200


def test_status_on_an_empty_database_still_carries_the_key(app, client):
    """The early return for a Pi with no nodes yet has to grow the same shape,
    or the dashboard reads undefined on a fresh install."""
    body = client.get("/api/status", headers=AUTH).get_json()
    assert body["node"] is None
    assert "outdoor_last_ts" in body
