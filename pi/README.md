# pi/

The ingest service, database schema, and system units that run on the Raspberry Pi.

Full reasoning for every decision here is in [docs/design.md](../docs/design.md); this file is
how to install and run it.

| File | Purpose |
| --- | --- |
| `app.py` | Flask app — ingest, placements, series API, dashboard route |
| `schema.sql` | Table definitions and the persistent pragmas |
| `requirements.txt` | Pinned `flask` + `waitress` |
| `backup.sh` | Nightly `VACUUM INTO` + copy off-box |
| `fetch_outdoor.py` | Hourly outdoor reference from Open-Meteo |
| `simulate_node.py` | Fake node, for driving the stack without hardware |
| `test_app.py` | Test suite, including the placement-overlap invariant |
| `test_outdoor.py` | The outdoor fetcher, against a local stand-in for the API |
| `install.sh` | Idempotent installer for the Pi |
| `static/` | The dashboard, plus vendored uPlot |
| `systemd/` | Service and backup timer units |

---

## Install on the Pi

The short way, from a checkout on the Pi:

```sh
sudo bash pi/install.sh
```

**Python 3.9 or newer is required** — `app.py` imports `zoneinfo`, and the pinned Flask and
waitress both require 3.9+. Bookworm's system Python (3.11) is fine. An older Raspberry Pi OS is
not: Buster ships 3.7, where the service dies at import. If the system `python3` is too old,
build a newer one alongside with `make altinstall` (which does *not* replace `python3`, so
Pi-hole and anything else on the box keep the interpreter they expect) and point the installer at
it:

```sh
sudo ENVLOG_PYTHON=python3.11 bash pi/install.sh
```

**Only the first time.** After that the venv exists and the installer reuses the interpreter it
was built on, so updating is a plain `git pull && sudo bash pi/install.sh` with nothing to
remember. `ENVLOG_PYTHON` still overrides when set, which is how you'd move to a newer
interpreter later.

`install.sh` checks the version up front and refuses rather than leaving you with a venv that
fails at first start.

It is safe to re-run — it never overwrites an existing token and never touches the database. It
prints the token and the dashboard URL when it finishes.

<details>
<summary>Or by hand</summary>

```sh
sudo mkdir -p /opt/envlog /var/lib/envlog /etc/envlog
sudo cp app.py schema.sql backup.sh requirements.txt /opt/envlog/
python3 -m venv /opt/envlog/.venv
/opt/envlog/.venv/bin/pip install -r /opt/envlog/requirements.txt
```

Bookworm marks its system Python externally managed (PEP 668), so the venv is required. Do not
reach for `--break-system-packages` on a service meant to run for years.

The shared secret goes in `/etc/envlog/envlog.env`, which must be root-owned and mode `0600` —
it authenticates *every* endpoint, not just `/ingest`:

```sh
printf 'ENVLOG_TOKEN=%s\n' "$(openssl rand -hex 32)" | sudo tee /etc/envlog/envlog.env
sudo chmod 600 /etc/envlog/envlog.env
```

The schema is applied automatically on first start; there is no separate init step.

```sh
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl enable --now envlog.service envlog-backup.timer
```

</details>

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENVLOG_PYTHON` | `python3` | Interpreter the venv is built on. **Needs 3.9+** — see below. |
| `ENVLOG_TOKEN` | *(required)* | Shared secret. The service refuses to start without it. |
| `ENVLOG_DB` | `/var/lib/envlog/envlog.db` | Database path |
| `ENVLOG_BIND` / `ENVLOG_PORT` | `0.0.0.0` / `8000` | Listen address |
| `ENVLOG_FLUSH_INTERVAL` | `60` | Seconds between buffer flushes |
| `ENVLOG_BACKUP_DEST` | *(unset)* | Where snapshots are copied. **Unset means backups never leave the SD card.** |
| `ENVLOG_LAT` / `ENVLOG_LON` | *(unset)* | Your coordinates. **Unset means no outdoor reference** — see below. |
| `ENVLOG_OUTDOOR_PAST_DAYS` | `2` | How far back each hourly fetch re-requests, so a missed run heals. |

---

## Endpoints

Every one of them requires `X-Auth-Token`. A tailnet is a flat network.

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/ingest` | Returns `202`; the row is buffered, not yet written |
| `GET` | `/placements` | All placements, newest first |
| `POST` | `/placements` | Records a move; closes the open placement first |
| `PATCH` | `/placements/:id` | Retroactive correction |
| `GET` | `/markers` | Every annotation, newest first |
| `POST` | `/markers` | `{label, ts?}` — `ts` defaults to now |
| `PATCH` | `/markers/:id` | Correct the time or the wording |
| `DELETE` | `/markers/:id` | Returns `204` |
| `GET` | `/api/status` | Liveness: last reading, seconds since, current room, newest outdoor hour |
| `GET` | `/api/series` | Chart data, segmented by placement |
| `GET` | `/api/candidate-moves` | Reboots with a gap that no placement explains |
| `GET` | `/api/export` | Raw rows as CSV, room resolved per reading |
| `GET` | `/api/compare` | Two windows summarised side by side, from raw rows |
| `GET` | `/api/decay` | Exponential fit over one window: rate, half-life, r² |
| `GET` | `/rooms` | The room preset list |
| `GET` | `/` | The dashboard |
| `POST` | `/login` | Exchanges the token for a cookie |

`/api/series` takes `node`, `metrics` (comma-separated column names), `from`, `to` (unix
seconds), `tz` (IANA name), and `bucket` — an integer number of seconds, or `day` for local
calendar days in `tz`.

Its `segments` array holds one entry per placement, so **a move breaks the line** rather than
drawing a slope between two rooms that never happened. Unlabelled periods come back as
`"Unknown"` rather than disappearing.

Its `outdoor` key carries the reference series for whichever requested metrics have an outdoor
counterpart, and is `null` when none do — CO₂ and the particle counts have none. It rides along
with the readings rather than living at its own endpoint, because a second round trip could
answer for a different window than the first.

---

## The dashboard

Open `http://envlog.home:8000/` and paste the token once. A browser cannot send an
`X-Auth-Token` header when you follow a link, so the token is exchanged for an HttpOnly cookie
and remembered; `http://envlog.home:8000/?token=...` works too and immediately redirects to strip
the token back out of the URL.

It shows liveness in the header, a room timeline with the settling period hatched, five charts,
a move control, and an editable placement history. Moves appear as dashed vertical rules on every
chart and break the line, so a trend is never drawn across two different rooms.

**Markers** annotate what you did. Type a label, press *Place on a chart…*, then click any chart
at the moment it happened; the marker draws as a solid magenta rule on all six charts, labelled.
Arming is deliberate rather than a bare click, because uPlot already uses drag-on-a-chart to zoom
— a drag of more than a few pixels is treated as a zoom and places nothing. Escape cancels. The
label field autocompletes from every marker you have ever made, so "opened windows" does not
acquire a second spelling. Misplaced ones are editable and deletable in the history table below.

**The outdoor reference** draws as a dashed line in the same hue as the indoor series it answers,
on the temperature, humidity, pressure and PM charts. It is step-held between hourly points
rather than interpolated — an hourly figure is a claim about that hour, and a slope between two
of them would be invented — and it stops after two hours without new data instead of running on
flat, so a dead fetcher looks like a dead fetcher. The header says so too, once the newest hour
is more than three hours old.

CO₂ has no outdoor line: the upstream air-quality model carries carbon *monoxide*, a different
gas. The decay fit's 420 ppm is still an assumption, not a measurement.

uPlot is vendored in `static/vendor/` rather than loaded from a CDN — the Pi may have no route to
the internet, and the dashboard has to work when it doesn't.

---

## The outdoor reference

Off by default, because it needs your coordinates and those are not something to guess. To turn
it on, add them to `/etc/envlog/envlog.env` (root-owned, `0600` — they never belong in the
repository) and run it once:

```sh
printf 'ENVLOG_LAT=40.71\nENVLOG_LON=-74.01\n' | sudo tee -a /etc/envlog/envlog.env
sudo systemctl start envlog-outdoor.service
```

Two decimals is plenty and is all that gets sent: the fetcher rounds before it asks, the model's
own grid is kilometres wide, and there is no reason to hand a third party a sharper fix on your
house than the answer needs.

The timer is installed and enabled by `install.sh` whether or not coordinates are set — with
none, the fetch exits 0 and says it is disabled, so enabling it later needs no reinstall.

**The upstream contract was checked on 2026-09-06** — 72 hours, all six columns populated — so
the variable names are known good, not merely documented. Re-run the same check any time a column
starts coming back empty, or after changing coordinates:

```sh
sudo -u envlog ENVLOG_LAT=37.85 ENVLOG_LON=-122.27 \
  /opt/envlog/.venv/bin/python /opt/envlog/fetch_outdoor.py --dry-run
```

It writes nothing. Every column should carry a number; a column of `None` across every hour means
that name has changed upstream — fix it in `WEATHER_VARS` or `AIR_QUALITY_VARS`. The fetcher
treats an unknown name as an empty column rather than an error, so that failure is a blank line
on a chart and not an hourly unit going red every hour forever.

**`us_aqi` will not track the PM columns, and that is correct.** US AQI is the maximum across
pollutants, not PM2.5's own sub-index, so an AQI in the 40s alongside a PM2.5 of 4 µg/m³ is
ozone doing the driving.

**It is a model, not a sensor.** Open-Meteo serves a numerical forecast on a grid of several
kilometres. It is the trend over your neighbourhood, and when it disagrees with a thermometer
outside your window, the thermometer is right. Its value here is comparative: whether an indoor
number is yours or the whole region's.

---

## Running it without hardware

Start the service, then:

```sh
# Two days of history, written straight to the database, for dashboard work
python simulate_node.py --backfill-hours 48 --db /var/lib/envlog/envlog.db

# Or post like the real node does, over HTTP
ENVLOG_TOKEN=... python simulate_node.py --url http://localhost:8000 --fast --count 60
```

`--fast` posts once a second and no faster. The server assigns timestamps at one-second
resolution, so anything quicker lands every reading on the same `ts`, collides on
`PRIMARY KEY (node_id, ts)`, and `ON CONFLICT DO NOTHING` silently collapses the batch into a
single row. `--backfill-hours` exists for that reason: it bypasses `/ingest` and writes spaced
timestamps directly, which is a fixture generator, not a node.

The simulator reproduces the awkward parts of the contract on purpose — PM absent from ~90% of
posts, occasional sensor dropouts, `boot_count` increments — because those are what the service
has to cope with.

## Tests

```sh
python -m pytest pi -q
```

`test_dashboard.py` runs the dashboard's gap-detection JavaScript through `node`, and skips
cleanly when node is absent — so a Pi without it still runs everything else. Don't name a single
test file on the command line; that is how a test stops being run without anyone noticing.
