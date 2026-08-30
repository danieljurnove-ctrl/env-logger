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
| `simulate_node.py` | Fake node, for driving the stack without hardware |
| `test_app.py` | Test suite, including the placement-overlap invariant |
| `systemd/` | Service and backup timer units |

---

## Install on the Pi

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

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENVLOG_TOKEN` | *(required)* | Shared secret. The service refuses to start without it. |
| `ENVLOG_DB` | `/var/lib/envlog/envlog.db` | Database path |
| `ENVLOG_BIND` / `ENVLOG_PORT` | `0.0.0.0` / `8000` | Listen address |
| `ENVLOG_FLUSH_INTERVAL` | `60` | Seconds between buffer flushes |
| `ENVLOG_BACKUP_DEST` | *(unset)* | Where snapshots are copied. **Unset means backups never leave the SD card.** |

---

## Endpoints

Every one of them requires `X-Auth-Token`. A tailnet is a flat network.

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/ingest` | Returns `202`; the row is buffered, not yet written |
| `GET` | `/placements` | All placements, newest first |
| `POST` | `/placements` | Records a move; closes the open placement first |
| `PATCH` | `/placements/:id` | Retroactive correction |
| `GET` | `/api/status` | Liveness: last reading, seconds since, current room |
| `GET` | `/api/series` | Chart data, segmented by placement |
| `GET` | `/` | Dashboard (roadmap item 5; a placeholder for now) |

`/api/series` takes `node`, `metrics` (comma-separated column names), `from`, `to` (unix
seconds), `tz` (IANA name), and `bucket` — an integer number of seconds, or `day` for local
calendar days in `tz`.

Its `segments` array holds one entry per placement, so **a move breaks the line** rather than
drawing a slope between two rooms that never happened. Unlabelled periods come back as
`"Unknown"` rather than disappearing.

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
python -m pytest pi/test_app.py -q
```
