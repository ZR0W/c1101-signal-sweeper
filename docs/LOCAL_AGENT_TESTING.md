# Hardware test plan for a local agent

You are a coding agent (for example Claude Code) running on the user's own
computer, with the CC1101 + Arduino Nano ESP32 plugged in over USB. Your job is
to verify this repo on real hardware, fix what you can, and report back. Work
through the phases in order. **Do not move on until the current phase passes.**

Everything here was tested in the cloud against a simulated board and against
the sketch compiled for the host with stub radio/serial libraries. What has
**not** been tested is the real radio, real USB behaviour and a real key fob.
That is what this plan covers.

## Ground rules (non-negotiable)

1. **Receive only.** Never add, enable or suggest transmit code, and never build
   anything that replays a captured signal. If the user asks for that, decline.
2. **One radio, one port owner.** Only one program may have the serial port
   open at a time: the Arduino IDE Serial Monitor, `tools/firmware_check.py`
   *or* `collector.py`. Make sure the previous one has exited before starting
   the next. The dashboard never opens the port.
3. **Ask the user for physical actions.** You can't plug in cables, press
   buttons or click in the Arduino IDE. When a step is marked
   **ASK THE USER**, stop, say exactly what to do and what they should see,
   and wait for their answer.
4. **Don't change the firmware's existing commands** (`scan`, `listen`, `rssi`,
   `bands`, `?`). If a check fails, find the root cause before editing anything,
   and keep fixes minimal.
5. **Process hygiene.** Start long-running programs (dashboard, collector) so
   you can stop them by PID. Never kill by a pattern that could match your own
   shell (for example `pkill -f collector`).
6. Record every command you run and its key output for the final report (Phase 7).

## Phase 0: Figure out the machine

```
python --version            # need 3.10+
git status; git log --oneline -3
```

Note the OS. Windows uses `COMx` ports and PowerShell. macOS uses
`/dev/cu.usbmodem*` and Linux uses `/dev/ttyACM*`. On Linux, if opening the
port fails with "permission denied", the user needs to be in the `dialout`
group. **ASK THE USER** to run `sudo usermod -aG dialout $USER` and log out and
back in.

## Phase 1: Python environment and offline checks (no hardware needed)

```
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
python -m py_compile collector.py dashboard.py tools/fake_board.py tools/firmware_check.py
python collector.py --help
```

**macOS/Linux only:** run an end-to-end check against the simulated board. Skip
this on Windows, since `fake_board.py` needs a pty.

```
python tools/fake_board.py &                # note the /dev/pts/N (or /dev/ttysNNN) it prints, and its PID
printf 'sweep keyfob\nwatch keyfob 25\nexport demo.csv\nstatus\n' | \
    python collector.py --port <that port> --db demo.db
```

Pass if:
- `sweep keyfob` prints 41 lines from 314.8000 to 315.2000.
- `watch keyfob 25` prints about 13 pass lines, and at least one is marked
  `<-- activity` near 315.0000. The simulated fob fires every 20 s.
- `demo.csv` has a header plus about 570 rows, and `status` shows the same count.

Kill the fake board by its PID. Delete `demo.db*` and `demo.csv` afterwards.

## Phase 2: Arduino setup and flashing

**ASK THE USER** to confirm these four settings in the Arduino IDE (or do them
with `arduino-cli`, below). They cause almost every failure:

1. Board: **Arduino ESP32 Boards → Arduino Nano ESP32** (not "Arduino Nano").
2. Tools → Pin Numbering → **By GPIO number (legacy)**.
3. Library **SmartRC-CC1101-Driver-Lib version 2.5.7** (not 3.x).
4. Wiring, Nano → CC1101: GND→1, **3V3**→2 (not 5V), D2→3 GDO0, D10→4 CSN,
   D13→5 SCK, D11→6 MOSI, D12→7 MISO.

Then flash `firmware/cc1101_scanner.ino`. Either:

- **IDE:** **ASK THE USER** to open the sketch, click Upload and tell you
  the port it uploaded to.
- **arduino-cli (if installed; you can run this yourself):**

  ```
  arduino-cli core install arduino:esp32
  arduino-cli lib install "SmartRC-CC1101-Driver-Lib@2.5.7"
  arduino-cli board details -b arduino:esp32:nano_nora     # find the pin-numbering option + value
  arduino-cli compile --fqbn "arduino:esp32:nano_nora:PinNumbers=byGPIONumber" firmware
  arduino-cli board list                                   # find the port
  arduino-cli upload  --fqbn "arduino:esp32:nano_nora:PinNumbers=byGPIONumber" -p <PORT> firmware
  ```

  Use the exact option key and value that `board details` prints for
  "By GPIO number (legacy)"; `PinNumbers=byGPIONumber` is the expected spelling,
  but confirm it. Note that `firmware` is the sketch folder, and its `.ino`
  must have the folder's name. If it doesn't, copy the sketch into a folder
  named `cc1101_scanner` and compile that. If upload can't find the board,
  **ASK THE USER** to double-tap the reset button (bootloader mode) and retry.

After flashing, the port number **may change** because native USB
re-enumerates. Re-list the ports:

```
python -c "import collector; [print(p) for p in collector.find_ports()]"
```

Pass if the board shows up with `True` (looks like the board) in the last column.

## Phase 3: Firmware verification over USB

Make sure the IDE Serial Monitor is **closed**, then:

```
python tools/firmware_check.py                 # or: --port COM5
```

This sends about 35 commands and prints PASS/FAIL for each. It covers the
`probe` format, default dwell, dwell scaling, malformed arguments, and every
legacy command (`bands`, `?`, `rssi` with stop, `scan 433 2`, `listen` with
stop, unknown command). It finishes with a `probe` after the legacy commands,
to confirm the radio returns to a clean state. Pass = `27/27 checks passed`
and exit code 0.

If it fails:
- **Banner says "CC1101 not found"**: it's pin numbering or library version
  (about 90% of cases), then wiring. Re-check the Phase 2 list with the user.
- **No RSSI lines at all, but legacy commands pass**: the board is running the
  old sketch. Re-flash.
- **Nothing at all**: wrong port, or another program has it open.
- **Timing checks** (`200 ms dwell takes ~4x the samples`, back-to-back probes):
  report the numbers. A ratio slightly outside 2.5–6 is not a firmware bug by
  itself.

Then a quick sanity check of the readings (this is a judgement call, not a hard
pass/fail). Run `probe 433.92 50` a few times. Peak should usually be in
roughly −100 to −80 dBm with nothing transmitting, and the average a few dB
below the peak. Values pinned at −128 or 0 mean the radio isn't really
receiving.

## Phase 4: Collector on real hardware

Start it with auto-detect, no `--port`:

```
python collector.py
```

Check, in this order:

| step | type | pass if |
|---|---|---|
| startup | (none) | prints `Connected to <port> @ 115200.`, no "no RSSI reply" warning |
| `status` | | `connected, probe ok` |
| `ports` | | the board's port is marked `<- looks like the board` |
| `sweep keyfob` | | 41 live lines, `41 readings logged as 'keyfob'`, under ~5 s |
| `sweep 433.8 434.0 50 20` | | 5 lines, label `sweep` |
| `sweep 340 400 100` | | refused: range crosses a gap the CC1101 can't tune |
| `monitor 433.92` then Enter | | about 1 line/s, stops cleanly on Enter |
| `watch survey_433` then `status` then Enter | | `status` works while busy; other commands say Busy; Enter stops |
| `export test_export.csv 10` | | CSV written; its row count matches the readings from this session |

Then the **re-enumeration test.** Start `watch keyfob`. **ASK THE USER** to
unplug the USB cable, wait 3 s and plug it back in (within 20 s). Pass if the
collector prints `Serial link lost…`, then `Reconnected on <port>`, and pass
lines resume, even if the port name changed. Also try a failure: start
`watch keyfob`, **ASK THE USER** to unplug and leave it out for 30 s. Pass if
the job ends with "serial link lost" and the prompt still works. After
re-plugging, `reconnect` should work.

Then quit (`quit`). Scripted mode should also work, and is handy for you as an
agent:

```
python collector.py -c "watch keyfob 30"          # interactive after the job
printf 'watch keyfob 30\nstatus\n' | python collector.py      # macOS/Linux
"watch keyfob 30`nstatus" | python collector.py               # PowerShell
```

Piped commands run one job at a time to completion, so always give `watch` a
duration there.

## Phase 5: Dashboard

Start it in the background and note the PID:

```
flask --app dashboard run --port 5000
```

With the DB from Phase 4, check the APIs (`curl` or Python `urllib`):

```
/api/summary                                   -> labels includes keyfob; noise_floor about -95..-85; threshold = floor + 10
/api/latest_sweep?label=keyfob                 -> 41 items, freq 314.8..315.2
/api/waterfall?label=keyfob&minutes=60         -> len(freqs) = 41; len(z) = len(times); each row has 41 values
/api/activity?minutes=60                       -> list (may be empty before Phase 6)
/api/freq_history?freq=315.0&label=keyfob      -> times/peak/avg arrays of equal length
```

Also check that it degrades gracefully. Run `RFSCAN_DB=nonexistent.db flask
--app dashboard run --port 5001` (PowerShell: `$env:RFSCAN_DB="nonexistent.db"`).
Every API should return 200 with empty data, and `nonexistent.db` must **not**
be created.

Finally, **ASK THE USER** to open http://127.0.0.1:5000 and confirm all four
panels render. If Playwright is available, you can take a screenshot yourself.
The page should load Plotly from the CDN, or from `/plotly.min.js` when offline.

## Phase 6: Live key fob test (the real goal)

Run the collector and the dashboard at the same time. This also proves the
WAL setup: one writer, one reader.

1. Start: `python collector.py -c "watch keyfob 120"`.
2. **ASK THE USER** to hold the fob about 1 m from the antenna and press a button
   (lock) 3 times, about 15 s apart, and tell you the clock time of each press.
3. Pass if all of these hold:
   - Collector lines at those times show `<-- activity`.
   - `/api/activity?minutes=10&label=keyfob` has one row per press, with
     timestamps within a few seconds of the user's times and `above_db` ≥ 10.
   - The user sees a bright horizontal stripe on the waterfall at each press.
4. Record the frequency of the strongest bin (`freq` in the activity rows).
   That is the fob's frequency, to within the 10 kHz step.

**If nothing shows up:**
- Check the press was short, and the 41-step sweep (~1.8 s) may have missed
  it. Ask for a longer hold of 2–3 s.
- Many fobs aren't exactly 315.0 MHz. Widen the search:
  `addpreset fob_wide 310 320 50 20`, then `watch fob_wide 90` with presses.
  If it's still nothing, try `watch survey_433 90`, since some fobs (EU, and
  some US) use 433.92 MHz.
- Once you've found the frequency, add a narrow preset around it
  (±200 kHz, 10 kHz steps) and repeat steps 1–3 to confirm.

Optional: a weather sensor or other periodic transmitter. Run `watch survey_433 300`
(or `survey_915`). Pass if the waterfall shows dots at a regular interval at one
frequency.

## Phase 7: Report

Write `docs/TEST_REPORT.md` (and summarise it to the user) with:

- OS, Python version, Arduino core version, library version, port name(s) seen
- Phase-by-phase PASS/FAIL, with `firmware_check.py`'s full output pasted in
- Noise floor observed per preset, and the fob frequency found in Phase 6
- Every failure: what you saw, the root cause, and the fix (with commit hash) or
  why you didn't fix it
- Anything confusing in the README or the collector's messages that a person
  would trip over

Commit fixes in small commits with clear messages. Push only if the user asks.
Never commit `rfscan.db*` or CSV exports; they're git-ignored.
