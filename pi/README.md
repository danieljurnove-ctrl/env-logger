# pi/

The ingest service, database schema, and system units that run on the Raspberry Pi.
**Not yet written** — see the roadmap in the [root README](../README.md#roadmap).

## What will live here

| File | Purpose |
| --- | --- |
| `app.py` | Flask app — ingest, placements, series API, dashboard |
| `schema.sql` | Table definitions and the persistent pragmas |
| `requirements.txt` | Pinned `flask` + `waitress` |
| `backup.sh` | Nightly `VACUUM INTO` + rsync off-box |
| `simulate_node.py` | Fake node for testing the whole stack without hardware |
| `systemd/envlog.service` | The ingest service |
| `systemd/envlog-backup.{service,timer}` | Nightly backup |

`.env` (holding the shared ingest token) and any `*.db` files are gitignored.

## Notes for whoever writes this

Full reasoning is in [docs/design.md](../docs/design.md). The parts easiest to get wrong:

- **Pragmas split two ways.** `journal_mode` belongs in `schema.sql`; `synchronous`,
  `busy_timeout` and `foreign_keys` reset on every connection and belong in the connection
  factory. `foreign_keys` defaults to **OFF**.
- **`ON CONFLICT DO NOTHING`** on reading inserts — retries and backward clock steps otherwise
  collide on the primary key.
- **Placements must not overlap.** Close the open one before opening the next; SQLite can't
  enforce this for you.
- **Auth on every endpoint**, not just `/ingest` — a tailnet is a flat network. Plus
  `MAX_CONTENT_LENGTH` and `hmac.compare_digest`.
- **`VACUUM INTO` for backups, never `cp`** — a plain copy of a live WAL database can be corrupt.

`simulate_node.py` should be able to drive the entire service — including realistic NULLs for
duty-cycled PM readings and `boot_count` increments — so the Pi side can be developed and tested
before any hardware exists.
