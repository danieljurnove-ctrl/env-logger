# Hardware

Bill of materials, wiring, and the specific traps this board sets for you.

All part numbers below were verified against Adafruit's catalogue on 2026-08-30.

---

## Bill of materials

| Part | PID | Notes |
| --- | --- | --- |
| ESP32 Feather V2 with headers — 8 MB flash + 2 MB PSRAM, STEMMA QT | [5900](https://www.adafruit.com/product/5900) | Headers pre-soldered; stacking headers can't be added later |
| BME280 temperature / humidity / pressure, STEMMA QT | [2652](https://www.adafruit.com/product/2652) | Ships at I²C address **0x77** |
| SCD-41 true NDIR CO₂ (+ temp/RH), STEMMA QT | [5190](https://www.adafruit.com/product/5190) | I²C address **0x62** |
| PM2.5 air quality sensor + breadboard adapter kit (PMS5003) | [3686](https://www.adafruit.com/product/3686) | UART, **not** I²C |
| STEMMA QT cable, 100 mm (×2) | [4210](https://www.adafruit.com/product/4210) | |
| Female/female jumper wires | [1950](https://www.adafruit.com/product/1950) | 20 × 150 mm |

No soldering. The Feather has male headers pre-soldered and the PMS5003 adapter ships with male
pins, so female/female jumpers are the correct choice for both ends.

**Possibly worth adding:** a 200 mm STEMMA QT cable ([4399](https://www.adafruit.com/product/4399))
— see the BME280 placement note below.

---

## Pinout

**These GPIO numbers differ from the original ESP32 Feather.** Following a tutorial written for
the older board is the single likeliest way to lose an evening.

| Signal | GPIO |
| --- | --- |
| SDA | GPIO22 |
| SCL | GPIO20 |
| UART RX | GPIO7 |
| UART TX | GPIO8 |
| STEMMA QT / NeoPixel power | GPIO2 |
| User button (SW38) | GPIO38 |
| NeoPixel data | GPIO0 |

Verified against `espressif/arduino-esp32` → `variants/adafruit_feather_esp32_v2/pins_arduino.h`
and ESPHome's own `esphome/components/esp32/boards.py`.

**ESPHome refuses GPIO7 and GPIO8 by default**, with *"This pin cannot be used on ESP32s and is
already used by the flash interface"*. It validates against the generic ESP32, where GPIO6–11 are
the SPI flash bus. This board is an **ESP32-PICO-MINI-02**, whose flash is inside the module on
other pins — which is exactly why Adafruit could route RX/TX out to 7 and 8. The error names the
escape hatch itself, and the config uses it on both UART pins:

```yaml
rx_pin:
  number: GPIO7
  ignore_pin_validation_error: true
```

It downgrades to a warning on every compile, which is the correct outcome and not something to
silence. Note that this applies to the *UART* pins only: nothing about GPIO2 or the I²C pins
needs it.

---

## Wiring

### I²C sensors — no wiring needed

Feather STEMMA QT port → SCD-41 → BME280, daisy-chained with the two 100 mm cables. Plug-and-play,
no jumpers, no polarity to get wrong.

Addresses don't collide: SCD-41 sits at 0x62, BME280 at 0x77.

### PMS5003 — four jumpers

| PMS5003 adapter | Feather V2 pin | Why |
| --- | --- | --- |
| VCC | **USB** | **5 V, not 3.3 V** — see below |
| GND | GND | |
| TX | RX (GPIO7) | sensor → Feather, the data itself |
| RX | TX (GPIO8) | Feather → sensor, **required to duty-cycle the fan** |

**VCC must be 5 V.** The PMS5003 needs 5 V to drive its fan; the `3V` pin will not do. Its
*logic* is 3.3 V, which is why the data lines connect straight to the ESP32 with no level
shifter. On this board 5 V comes from the `USB` pin, which is only live while USB-C is plugged
in — so the PM sensor cannot run from a LiPo without a 3.7 V→5 V boost converter.

**The fourth jumper is not optional if you want the fan duty-cycled.** ESPHome's `pmsx003`
component works receive-only, but its `update_interval` option — the one that spins the fan down
between readings — is validated as requiring `tx_pin`. Without that wire you get continuous
operation and roughly eight months of laser life.

---

## The I2C power trap

**Symptom:** every I²C sensor fails to enumerate. `scan` finds nothing. The sensors are fine.

The STEMMA QT connector and the onboard NeoPixel share a 3.3 V regulator gated by **GPIO2**
(`NEOPIXEL_I2C_POWER` in Arduino and CircuitPython, `NEOPIXEL_POWER` in ESPHome's board table).
It must be driven **HIGH** to power the port.

Whether anything does that for you depends on the framework:

| Framework | Drives GPIO2 high? |
| --- | --- |
| CircuitPython | Yes — `board.c` sets it at startup |
| Arduino / arduino-esp32 | Yes — `initVariant()` in `variant.cpp` |
| **ESPHome, `esp-idf` (the current default)** | **No. Nothing touches it.** |
| ESPHome, `framework: type: arduino` | Yes, via the same `initVariant()` |

So under ESPHome's default framework you must do it yourself:

```yaml
switch:
  - platform: gpio
    pin: GPIO2
    restore_mode: ALWAYS_ON
    setup_priority: 1200      # POWER, above BUS (1000)
    internal: true

i2c:
  sda: GPIO22
  scl: GPIO20                 # leave setup_priority at its default
```

`setup_priority: 1200` is `POWER` in ESPHome's own scale, which sits above `BUS` (1000) where
the I²C bus initialises. Don't instead demote the bus to 600 — that's `DATA`, the exact priority
the sensor components use, so it only works by tie-break on registration order.

Include the switch even if you're on the Arduino framework. It's redundant there and harmless,
and it means a future framework switch doesn't hand you a mystery.

---

## Per-sensor notes

### BME280

Self-heats by 1–3 °C, worse in still air and worse near the PM sensor's fan. Keep it physically
distant from both the Feather and the PMS5003.

Note that "put it last in the daisy chain" is not a fix — position on an I²C bus is electrically
irrelevant, and with two 100 mm cables the BME280 can only get about 200 mm from the board
anyway. If the offset turns out to be large, a longer cable is the actual remedy.

The node posts this sensor **raw** — no offset filter — because a correction baked into the
firmware is burnt into the archive permanently. The SCD-41's independent temperature sits in its
own column right beside it, which is what lets you measure the real offset later and apply it at
query time, retunable, over history you still have unmodified.

### SCD-41

- `temperature_offset` defaults to **4 °C** in ESPHome. Worth revisiting once you can compare
  against the BME280 in the same enclosure.
- Ambient pressure compensation can read directly from the BME280 via
  `ambient_pressure_compensation_source`. Don't also set `altitude_compensation` — it's ignored
  when a pressure source is configured.
- `low_power_periodic` measurement mode requires `update_interval` of at least 30 s.

**Automatic self-calibration versus a portable sensor — unresolved, check on hardware.** ASC
needs days of continuous running to converge, and assumes the sensor periodically sees roughly
outdoor-level air. But GPIO2 gates this sensor's power, so *every* unplug — every room move —
power-cycles it. If ASC state doesn't survive that, a node moved daily may never produce
trustworthy absolute ppm.

If that turns out to be the case: perform one forced recalibration outdoors at ~420 ppm, disable
ASC, and treat CO₂ as a relative trend. Relative trends are unaffected either way, and are what
most of the useful questions ("is this room stuffy by bedtime?") actually depend on.

### PMS5003

- 9600 baud, UART.
- **`update_interval` must be strictly greater than 30 s.** The config validator accepts `>= 30s`,
  but the duty-cycle code path tests `> 30000 ms` — so exactly `30s` compiles cleanly, demands
  the `tx_pin` wire, and then runs the fan continuously anyway with no warning. Use `5min`.
- The first 30 s of every cycle is fan warm-up and produces no reading. At a 5-minute interval
  that's a ~10% duty cycle and roughly 2.5 years of laser life instead of eight months.
- Reports two concentration sets (CF=1 "standard particle" and atmospheric). This project stores
  the atmospheric values — hence the `_atm` column suffix.
- **A reading of 0 µg/m³ is usually clean air, not a broken sensor.** The mass concentrations are
  *derived* from particle counts and reported as integers, so a genuinely clean room rounds to
  zero on all three sizes indefinitely. Confirmed on this hardware 2026-09-05: mass all zero while
  the same frames carried `PM0.3 Particles: 126 Count/0.1L, PM0.5: 112, PM1.0: 24, PM2.5: 2`.
  The counts are the proof the laser is working, and they appear in the ESPHome log at DEBUG even
  though this project does not store them. Check there before suspecting the sensor — a running
  fan says nothing about the optical path, but a non-zero particle count does.

---

## Possible upgrades

Not ordered, not planned — recorded so the reasoning isn't lost.

### The highest-value addition isn't a sensor

**A second node.** Every question in [Purpose](design.md#purpose) is a comparison, and right now
comparisons are sequential: carry the node to the bedroom, wait, carry it to the office, wait.
Two nodes make them simultaneous, which removes the confound that the two rooms were measured at
different times of day with different weather outside.

Put the second one **outdoors** and it does double duty as a baseline. PM2.5 of 20 µg/m³ means
"your kitchen is smoky" on a clean day and "the windows aren't sealing" during a wildfire. CO₂
decay needs an outdoor baseline for a true air-change figure, and heat-loss rates need outdoor
temperature to normalise against. A Feather with just a BME280 and an SCD-41 covers it, and the
schema already supports multiple nodes. Cheaper still: pull a public AQI/weather API into the
same database as a pseudo-node.

### Sensors worth adding

| Sensor | Adafruit PID | ESPHome | Fills |
| --- | --- | --- | --- |
| Sensirion **SGP41** VOC + NOx | [6455](https://www.adafruit.com/product/6455) | [`sgp4x`](https://esphome.io/components/sensor/sgp4x/) | The biggest real gap |
| **VEML7700** ambient light | [4162](https://www.adafruit.com/product/4162) | verify before buying | Context, sleep/circadian |
| **LD2410** mmWave presence | third-party | [`ld2410`](https://esphome.io/components/sensor/ld2410/) | Occupancy — but see the UART conflict |

**SGP41** is the one to buy first. It's STEMMA QT, so it daisy-chains onto the existing I²C run
with no wiring and no free GPIO needed, and it covers what nothing here can currently see:
cooking gases, cleaning products, solvents, new-furniture off-gassing. ESPHome's `sgp4x`
autodetects SGP40 versus SGP41 and exposes `voc_index` and `nox_index`.

Two caveats. It reports Sensirion's unitless Gas Index rather than a concentration — a *relative*
signal that needs a learning period to establish its baseline, not an absolute ppb figure. That
suits room comparison but means the index is not portable between rooms until it has settled,
which interacts badly with moving the node daily. And the component drives the sensor at 1 Hz
internally regardless of `update_interval`, which matters for a battery build but not here.

**LD2410** would make CO₂ genuinely interpretable, since knowing whether a room is occupied turns
a CO₂ curve into a ventilation measurement rather than a guess. But it wants a hardware UART at
256000 baud and the PMS5003 already has the obvious one (GPIO7/8, and note those two pins already
need `ignore_pin_validation_error`). The ESP32 has three UARTs, so it's solvable on other pins —
just not free.

### Deliberately not recommended

- **A DIY carbon monoxide sensor.** CO is a real safety gap in this build, and the right fix is a
  certified CO alarm from a shop, not an MQ-7 on a breadboard. Cheap electrochemical CO sensors
  drift, need calibration, and carry no alarm certification. Don't build the thing whose job is to
  wake you up.
- **BME680 as a BME280 replacement.** It adds a gas-resistance reading, but turning that into a
  useful IAQ figure depends on Bosch's BSEC library, whose ESPHome support is awkward and
  licence-encumbered. The SGP41 does this job better as a separate device.
- **Radon** is a genuine health risk and completely invisible here, but it belongs to a dedicated
  device with a long integration time, not this node. Worth a one-off test kit regardless of this
  project.

---

## Flashing

The first flash is over USB-C, from a laptop. Use a **data** cable; charge-only USB-C cables are
common and produce no enumerated serial port and no useful error.

The board's USB-to-serial chip is a CH9102F, which handles 921600 baud reliably. If auto-reset
into the bootloader doesn't take, hold **BOOT**, press and release **RESET**, then let go of
BOOT.

Everything after the first flash can go over the air.
