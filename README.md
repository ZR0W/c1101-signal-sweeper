# CC1101 signal sweeper

A small, local RF monitor. A CC1101 radio on an Arduino Nano ESP32 measures
signal strength; `collector.py` drives it over USB, runs frequency sweeps and
logs every reading to SQLite (`rfscan.db`); `dashboard.py` shows that file in
a browser as a live spectrum, waterfall, activity log and per-frequency history.

Typical use is finding the exact frequency of your own devices: a car key fob,
a weather sensor or a doorbell.

```
firmware/cc1101_scanner.ino   board sketch (scan / listen / rssi / bands / probe)
firmware/probe.diff           the change that added `probe` to the original sketch
collector.py                  owns the serial port; sweeps and writes rfscan.db
dashboard.py                  Flask app; reads rfscan.db only
templates/index.html          the dashboard page (Plotly)
presets.json                  named sweep ranges (keyfob, survey_915, survey_433)
tools/fake_board.py           simulated board for trying things without hardware
rfscan.db                     created at runtime (git-ignored)
```

## Ground rules

* **Receive only.** The firmware never transmits, and nothing here records a
  signal in order to replay it. Logging RSSI and raw pulse timings is fine;
  re-sending a captured signal is out of scope for this project.
* **One radio, one job.** There is a single CC1101, so only `collector.py`
  opens the serial port. Close the Arduino Serial Monitor before starting the
  collector, and don't run two collectors. The dashboard reads `rfscan.db` and
  never touches the port, so it is safe to run alongside the collector.

## 1. Arduino setup checklist

These four settings cause almost every "it doesn't work" problem. Check them first.

1. **Board:** Tools → Board → Arduino ESP32 Boards → **Arduino Nano ESP32**.
   Not the look-alike "Arduino Nano", which is a 5 V AVR board. Symptoms of the
   wrong board: `Serial.printf` doesn't exist, and the SPI pins are wrong.
2. **Pin numbering (critical):** Tools → Pin Numbering → **"By GPIO number (legacy)"**.
   The sketch uses raw GPIO numbers (48, 47, 38, 21, 5). With "By Arduino pin" the
   SPI pins resolve wrong and the CC1101 is not found. This menu only appears
   after the Nano ESP32 is selected.
3. **Library version:** Library Manager → **SmartRC-CC1101-Driver-Lib, version 2.5.7**.
   The 3.x rewrite starts SPI differently, and `getCC1101()` returns false even
   when the wiring is correct.
4. **Wiring** (Nano label → CC1101 pin). VCC must be **3V3, not VBUS/5V**:

   ```
   GND -> 1 GND     3V3 -> 2 VCC     D2  -> 3 GDO0    D10 -> 4 CSN
   D13 -> 5 SCK     D11 -> 6 MOSI    D12 -> 7 MISO    (8 GDO2 unused)
   ```

   If pin 1 is unclear, it's the pad with continuity to the SMA antenna body.

**"CC1101 not found"?** Re-check #2 and #3 first; they cause about 90% of
cases. Then check the wiring. The companion PixMob sketch has a per-pin wiring
test if you need it.

## 2. Flash the firmware

1. Open `firmware/cc1101_scanner.ino` in the Arduino IDE, with the settings above.
2. Upload. The Nano ESP32 uses native USB, so **the port can change number
   after flashing** (for example, COM4 becomes COM5). Re-select it if the IDE
   loses it.
3. Open Serial Monitor at **115200 baud**, line ending **"Newline"**. You should see
   `CC1101 ready. Receive only.` followed by the help text.
4. Verify the new command by hand:

   ```
   probe 315.0 50
   RSSI,315.000,-92,-97,142
   ```

   The format is `RSSI,<freq_mhz>,<peak_dbm>,<avg_dbm>,<samples>`. The dwell
   defaults to 50 ms and accepts 1–10000. `probe?` or bad arguments print a
   usage line instead. The existing commands (`scan`, `listen`, `rssi`,
   `bands`, `?`) work exactly as before.
5. **Close the Serial Monitor** so the collector can open the port.

## 3. Python setup

Python 3.10+.

```
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

## 4. Run the collector

```
python collector.py              # auto-detects the board
python collector.py --port COM4  # or name the port (/dev/ttyACM0, /dev/cu.usbmodem...)
```

The collector looks for a port whose description mentions ESP32, Arduino or
USB JTAG. It waits for the banner, then sends a test `probe` to confirm the
firmware is the right version. If the USB link drops (replug, reflash,
re-enumeration), it searches for the board for 20 s, including under a new port
name, and carries on. Otherwise, type `ports` and `reconnect [PORT]`.

Start gathering data for the key fob investigation:

```
rf> watch keyfob
Watching 'keyfob': 41 steps x 40 ms. Enter stops.
18:13:00 #18    [                                         ] floor -91 max -90 @ 314.8300 (1.8s)
18:13:02 #19    [               :+%@@@@                   ] floor -91 max -38 @ 315.0000 (1.8s)  <-- activity
```

Each line is one pass. The bracketed strip is a text spectrum: brighter
characters mean further above the floor. Press the fob near the antenna and
watch for `<-- activity`. Press **Enter** (or type `stop`, or Ctrl+C) to end
a job.

| command | what it does |
|---|---|
| `sweep START END STEP_KHZ [DWELL_MS]` | one pass, a live line per step, logged as label `sweep` |
| `sweep PRESET` | one pass of a preset, logged under the preset's name |
| `watch PRESET` | loop a preset forever; the main data-gathering mode for the waterfall |
| `monitor MHZ [DWELL_MS]` | probe one frequency repeatedly (label `monitor`) |
| `presets` / `addpreset NAME START END STEP_KHZ [DWELL_MS]` / `rmpreset NAME` | manage `presets.json` |
| `export [CSV_PATH] [LAST_MINUTES] [LABEL]` | dump readings to CSV (default `rfscan_export.csv`) |
| `status`, `ports`, `reconnect [PORT]`, `stop`, `help`, `quit` | |

Seeded presets:

| name | range | step | dwell |
|---|---|---|---|
| `keyfob` | 314.8–315.2 MHz | 10 kHz | 40 ms |
| `survey_915` | 902–928 MHz | 100 kHz | 30 ms |
| `survey_433` | 430–437 MHz | 50 kHz | 30 ms |

Commands can also be scripted: `python collector.py -c "watch keyfob"`, or pipe
commands into stdin. In piped mode, each job runs to completion before the next
line is read.

Each reading is one row in `rfscan.db` (`readings` table: `ts`, `freq_mhz`,
`rssi_peak`, `rssi_avg`, `samples`, `sweep_label`). The database uses WAL
mode, and each sweep pass is written in one transaction.

## 5. Run the dashboard

In a second terminal (the collector can keep running):

```
flask --app dashboard run        # open http://127.0.0.1:5000
```

Set `RFSCAN_DB=/path/to/rfscan.db` to point it at a different file.

* **Waterfall** shows time × frequency × peak dBm, newest at the top. A fob press
  is a short bright horizontal stripe. A periodic sensor is a row of dots at a
  regular interval.
* **Spectrum** shows the latest pass. Bars at or above the activity threshold are orange.
* **Activity** lists signals at least 10 dB above the noise floor, newest
  first. One transmission lights up several neighbouring bins, so these are
  merged into a single row at the strongest bin, with its span.
* **History** opens when you click a spectrum bar, a waterfall cell or an
  activity row. It plots that frequency's peak and average over time.

The **noise floor** is the median peak over the selected window for the
selected sweep label. The **activity threshold** is that floor plus 10 dB.
`dashboard.py` computes both in one helper, and every panel uses those values.
Use the controls at the top to pick the sweep label and time window. The page
polls every 4 s, and shows "waiting for data" until the collector has written
something.

Plotly loads from its CDN. If you're offline, the page falls back to the copy
bundled in the `plotly` Python package.

## Trying it without the board

On Linux or macOS, `tools/fake_board.py` creates a pseudo-terminal that
answers `probe` with simulated data. That data includes a "key fob" at
315.000 MHz that "presses" every 20 s:

```
python tools/fake_board.py                 # prints e.g. /dev/pts/3
python collector.py --port /dev/pts/3 --db demo.db
RFSCAN_DB=demo.db flask --app dashboard run
```

## Troubleshooting

* **"no RSSI reply to 'probe'"**: the board is running an older sketch
  without `probe`. Flash `firmware/cc1101_scanner.ino`.
* **"could not open port" / access denied**: another program has the port open
  (Serial Monitor, a second collector). Close it.
* **Port vanished after flashing**: expected with native USB. The collector
  re-detects it automatically, or use `ports` then `reconnect PORT`.
* **Board says "CC1101 not found"**: see the checklist in section 1. Check
  pin numbering and library 2.5.7 first.
