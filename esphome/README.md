# esphome/

Firmware config for the sensor node. **Not yet written** — see the roadmap in the
[root README](../README.md#roadmap).

## What will live here

| File | Purpose |
| --- | --- |
| `env-node.yaml` | The node config — board, I²C/UART buses, three sensors, HTTP POST |
| `secrets.yaml.example` | Template for WiFi credentials, the ingest token, and OTA password |

`secrets.yaml` itself is gitignored and must never be committed.

## Before writing any of it

Build and flash from a **laptop**, not the Pi — ESPHome needs Python ≥3.12 and more RAM than a
Pi 2B has. See [bring-up step 0](../docs/bring-up.md#step-0--confirm-esphome-builds-for-this-board).

Three things this config must get right, all detailed in [docs/hardware.md](../docs/hardware.md):

- **GPIO2 driven HIGH** via a `restore_mode: ALWAYS_ON` switch at `setup_priority: 1200`, or no
  I²C sensor will be found at all.
- **Explicit pins** — `sda: GPIO22`, `scl: GPIO20`, `rx_pin: GPIO7`, `tx_pin: GPIO8`. These
  differ from the original ESP32 Feather.
- **`update_interval: 5min`** on the PM sensor — strictly greater than 30 s, or duty cycling
  silently doesn't happen.

The POST body should carry only sensors that published since the last interval; see the
[node → server contract](../docs/design.md#node--server-contract).
