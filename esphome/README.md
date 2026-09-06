# esphome/

Firmware config for the sensor node.

| File | Purpose |
| --- | --- |
| `env-node.yaml` | The node — board, I²C/UART buses, three sensors, the button, the status LED, HTTP POST |
| `i2c-scan.yaml` | Bring-up step 4: I²C scan and nothing else |
| `minimal.yaml` | Bring-up step 0: does the toolchain compile for this board at all |
| `secrets.yaml.example` | Template for WiFi credentials, the ingest token, the OTA password |

`secrets.yaml` itself is gitignored and must never be committed. ESPHome looks for it next to
the config being compiled, so it belongs in this directory:

```powershell
copy esphome\secrets.yaml.example esphome\secrets.yaml
```

Build and flash from a **laptop**, not the Pi — ESPHome needs Python ≥3.12 and more RAM than a
Pi 2B has. See [bring-up step 0](../docs/bring-up.md#step-0--confirm-esphome-builds-for-this-board).

---

## Staged bring-up

Do not flash the whole thing at once. Each stage adds one new failure mode, and the config is
laid out so that staging costs nothing but deleting downward from a marked comment:

| Step | Config | Verify |
| --- | --- | --- |
| 4 | `i2c-scan.yaml` | Boot log lists **both** `0x62` and `0x77` |
| 5 | `env-node.yaml`, cut at `--- STEP 7` | Plausible values in the log from each I²C sensor |
| 6 | *(same image — wire the PM sensor, reboot)* | Fan audible ~30 s, stops, restarts ~5 min later |
| 7 | `env-node.yaml` entire | Rows accumulating on the Pi at the expected cadence |

**`--- STEP 7` is the only contiguous cut, and steps 5 and 6 share one image.** `uart:` is a
top-level key while `pmsx003` is a list item under `sensor:`, so no single deletion isolates the
PM sensor without also removing the two I²C ones. It doesn't need isolating: with nothing on the
UART the component never publishes, PM is simply absent from the row, and that is the designed
NULL-means-gap behaviour rather than a failure. Wire the jumpers at step 6 and reboot — no
reflash.

Step 4 is a separate file rather than a marker because it must contain **no sensor components at
all** — that is what makes an empty scan mean "the port has no power" and nothing else.

First flash is over USB-C with a **data** cable. Everything after step 5 can go over the air.

```powershell
& $HOME\.venvs\esphome\Scripts\esphome.exe run esphome\i2c-scan.yaml
& $HOME\.venvs\esphome\Scripts\esphome.exe run esphome\env-node.yaml
```

---

## The four things this config gets right

All four are load-bearing, and three of them fail in ways that point nowhere near the cause.
Details in [docs/hardware.md](../docs/hardware.md).

- **GPIO2 driven HIGH** by a `restore_mode: ALWAYS_ON` switch at `setup_priority: 1200`. Under
  ESPHome's `esp-idf` framework nothing else does this, and without it *no* I²C device
  enumerates — the sensors look dead.
- **Explicit pins** — `sda: GPIO22`, `scl: GPIO20`, `rx_pin: GPIO7`, `tx_pin: GPIO8`. These
  differ from the original ESP32 Feather.
- **`ignore_pin_validation_error: true` on the UART pins.** ESPHome validates against the
  generic ESP32, where GPIO6–11 are the flash interface, and **refuses to compile** with
  *"already used by the flash interface"*. This board is an ESP32-PICO-MINI-02 whose flash is
  in-package on other pins — which is why Adafruit could route RX/TX there at all.
- **`update_interval: 5min`** on the PM sensor — strictly greater than 30 s, or the fan runs
  continuously and silently.

Two more that are only obvious in hindsight:

- **`api:` needs `reboot_timeout: 0s`.** It defaults to 15 minutes and reboots the node when no
  API client has connected — which, with no Home Assistant on the network, is always. The
  component is here only so `esphome logs` works once the cable is off.
- **`request_headers:`, not `headers:`.** The key was renamed; the old name is now a hard
  validation error, and most examples still online use it.
- **The logger must stay at `DEBUG` through bring-up.** ESPHome's levels run
  `ERROR < WARN < INFO < CONFIG < DEBUG`, so `INFO` is *more* restrictive than it looks: the I²C
  scan is logged at CONFIG and sensor readings at DEBUG. Setting `INFO` suppresses both and
  leaves a node that looks dead while working perfectly — which is exactly what happened on the
  first flash here.

---

## The user button

The onboard tactile switch (**SW38** on the silk) records a marker on the Pi when
pressed. The moment you open a window is when you are *at the window*, not at
your phone, and a timestamp captured then is worth more than a label typed
twenty minutes later.

The label is typed later: a button cannot name what it saw, so the press lands as
`"button"` and gets renamed in the dashboard's marker history.

Two hardware facts drive the config, and getting either wrong gives you a pin
that never fires or fires constantly:

- **GPIO34–39 are input-only and have no internal pull resistors.** `pullup: true`
  is not merely unnecessary on GPIO38, it is unavailable. The Feather V2 fits an
  external pull-up on SW38, so the pin idles HIGH and reads LOW while pressed —
  hence `inverted: true`.
- **`on_click`, not `on_press`.** It fires on release after a deliberate hold
  (150 ms–3 s), so brushing the board while carrying it between rooms does not
  litter the charts. A 40 ms `delayed_on` filter debounces on top of that.

### The blink

**A green flash means the Pi stored it. A longer red flash means it did not.**
The node has no display, so without this a press is an act of faith — and a
press that reached the Pi looks exactly like one that came back 401.

The onboard NeoPixel does it, and needs no new hardware: it is on **GPIO0**,
verified against `espressif/arduino-esp32` →
`variants/adafruit_feather_esp32_v2/pins_arduino.h` (`PIN_NEOPIXEL 0`), and it
sits on the same GPIO2-gated 3.3 V rail as the STEMMA QT port
(`NEOPIXEL_I2C_POWER 2`) that the `i2c_power` switch already turns on, at a
higher setup priority than the light.

- **The success blink is conditional on the status code, not on the POST
  returning.** `on_response` checks for 201; anything else logs the code and
  blinks red, as does `on_error` and as does a press with no WiFi.
- **Red is longer than green (800 ms against 200 ms)**, so the two are told
  apart by duration as well as by colour. Red/green is the most common form of
  colour blindness.
- **20% brightness.** This is a bright LED at close range and the node may be
  in a bedroom.
- **`channel_colors: GRB` is required**, not defaulted from `chipset: WS2812` —
  omitting it fails validation. On an ESPHome older than this key it is
  `rgb_order: GRB`, which still works but warns that it goes away in 2027.3.
- **GPIO0 is a strapping pin**, shared with the BOOT button, and ESPHome warns
  about it on every compile. Safe for the same reason the GPIO2 warning is:
  strapping is latched at reset and the pin is only claimed as an output during
  setup, long after. Adafruit ships the board this way.

Only the button blinks. Readings post every 45 seconds and blinking for those
would be a light flashing all day in a bedroom.

A press with WiFi down is logged, blinked red, and lost — the same trade the
readings make: there is no queue and no batch endpoint to replay one into.

---

## Freshness, and why `has_state()` is not enough

The [contract](../docs/design.md#node--server-contract) is that each POST carries only sensors
that produced a *new* reading; omitted fields become NULL, which the schema renders as a gap.

`has_state()` cannot express that — it is true forever once a sensor has published. With PM
duty-cycled to 5 minutes against 45-second POSTs, using it would repeat the last PM value across
six posts in seven, and roughly 85% of the PM series would be fabricated.

So each sensor group sets a `fresh_*` global in `on_value`, the POST includes a group only if its
flag is set, and all three are cleared afterwards — whether or not the POST succeeded, because a
reading that failed to send is stale by the next interval and there is no batch endpoint to
replay it into.

The POST is skipped entirely when nothing is fresh and when WiFi is down.

---

## Verified so far

**`esphome config` passes on ESPHome 2026.8.2** for `env-node.yaml`, with no warnings other than
the two expected pin ones (GPIO0 and GPIO2 strapping, GPIO7/8 flash-interface). Every component,
pin, key and option is schema-valid, and this is the check that found the GPIO7/8 flash-pin
rejection, and later that `rgb_order` was deprecated while `channel_colors` was mandatory —
neither of which is visible by reading.

**A full `esphome compile` of `env-node.yaml` passed** on the Windows laptop on 2026-09-04, and
again on 2026-09-06 with the button block. That is what type-checks the C++ in the lambdas, so
the freshness globals, the `std::isnan` guards, the JSON building and the button's marker POST
are known to compile rather than merely known to be schema-valid.

**The status-LED change on top of that has NOT been compiled.** It adds a `light:`, two `script:`
blocks and a one-expression `on_response` lambda — all schema-valid on 2026.8.2, none of them
type-checked. Run the command below before flashing.

The compile has to happen on the laptop and not in an agent session. ESPHome fetches the ESP-IDF
toolchain and its pip constraints from `dl.espressif.com`, which a sandboxed session's egress
proxy refuses, so *schema-valid* is the ceiling any change made there can claim.

Re-run it after editing the lambda; the toolchain stays warm, so it is a couple of minutes rather
than twenty:

```powershell
& $HOME\.venvs\esphome\Scripts\esphome.exe compile esphome\env-node.yaml
```

**Nothing here has been checked against real sensors.** That is what bring-up steps 4–7 are for,
and the `temperature_offset` and ASC questions in
[docs/hardware.md](../docs/hardware.md#scd-41) can only be answered with hardware on the desk.
