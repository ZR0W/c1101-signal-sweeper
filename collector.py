#!/usr/bin/env python3
"""RF scanner collector: drives the CC1101 board over USB and logs to SQLite.

This is the ONLY process that may open the serial port (one radio, one job at
a time). The dashboard reads the SQLite file and never touches the port.

Receive only: the firmware has no transmit path and this tool never asks for one.

    python collector.py                 # auto-detect the board
    python collector.py --port COM4     # or pick the port yourself
    rf> watch keyfob                    # loop a preset sweep (Enter stops)
"""

from __future__ import annotations

import argparse
import csv
import json
import shlex
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import serial
import serial.tools.list_ports

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "rfscan.db"
DEFAULT_PRESETS = HERE / "presets.json"
BAUD = 115200
DEFAULT_DWELL_MS = 50
BOARD_HINTS = ("esp32", "arduino", "usb jtag", "nano")
SEED_PRESETS = {
    "keyfob": {"start": 314.8, "end": 315.2, "step_khz": 10, "dwell_ms": 40},
    "survey_915": {"start": 902.0, "end": 928.0, "step_khz": 100, "dwell_ms": 30},
    "survey_433": {"start": 430.0, "end": 437.0, "step_khz": 50, "dwell_ms": 30},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL NOT NULL,         -- unix epoch seconds
  freq_mhz   REAL NOT NULL,
  rssi_peak  INTEGER NOT NULL,
  rssi_avg   INTEGER NOT NULL,
  samples    INTEGER,
  sweep_label TEXT                   -- e.g. "keyfob", "survey_915", "monitor"
);
CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_freq ON readings(freq_mhz);
"""


def valid_freq(f: float) -> bool:
    """Same tuning ranges as the firmware's validFreq()."""
    return 300 <= f <= 348 or 387 <= f <= 464 or 779 <= f <= 928


def sweep_freqs(start: float, end: float, step_khz: float) -> list[float]:
    n = int(round((end - start) * 1000 / step_khz)) + 1
    return [round(start + i * step_khz / 1000, 4) for i in range(n)]


def bar(dbm: int, width: int = 30) -> str:
    n = max(0, min(width, int((dbm + 110) * width / 80)))   # -110..-30 dBm
    return "#" * n


# ---------------------------------------------------------------- serial

def find_ports() -> list[tuple[str, str, bool]]:
    """All serial ports as (device, description, looks_like_board), best first."""
    out = []
    for p in serial.tools.list_ports.comports():
        text = " ".join(filter(None, [p.description, p.manufacturer, p.product, p.hwid])).lower()
        out.append((p.device, p.description or "", any(h in text for h in BOARD_HINTS)))
    out.sort(key=lambda t: not t[2])
    return out


class Board:
    """Owns the serial connection. All radio I/O goes through probe()."""

    def __init__(self, port: str | None = None):
        self.port_override = port
        self.port: str | None = None
        self.ser: serial.Serial | None = None
        self.lock = threading.RLock()     # reconnect() re-enters probe()
        self.probe_ok = False

    @property
    def connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def _candidates(self, fallback: bool) -> list[str]:
        ports = find_ports()
        boards = [d for d, _, hit in ports if hit]
        if self.port_override:
            # After re-enumeration the board can come back under a new name.
            extra = [d for d in boards if d != self.port_override] if fallback else []
            return [self.port_override] + extra
        return boards or [d for d, _, _ in ports]

    def connect(self, quiet: bool = False, fallback: bool = False) -> bool:
        self.close()
        cands = self._candidates(fallback)
        if not cands:
            if not quiet:
                print("No serial ports found. Is the board plugged in? (try: ports)")
            return False
        for dev in cands:
            try:
                ser = serial.Serial(dev, BAUD, timeout=0.2, write_timeout=2)
            except (serial.SerialException, OSError) as e:
                if not quiet:
                    print(f"  {dev}: {e}")
                continue
            self.ser, self.port = ser, dev
            try:
                self._wait_banner(quiet)
                ok = self._check_probe(quiet)
            except (serial.SerialException, OSError, ConnectionError):
                self.close()
                continue
            if ok or len(cands) == 1:
                if not quiet:
                    print(f"Connected to {dev} @ {BAUD}.")
                return True
            self.close()
        return False

    def _wait_banner(self, quiet: bool) -> None:
        """Opening the port may reset the board; give the banner time to arrive."""
        deadline = time.time() + 2.5
        while time.time() < deadline:
            line = self._readline()
            if line is None:
                continue
            if "CC1101 not found" in line and not quiet:
                print("Board says: CC1101 not found. Check Tools > Pin Numbering = "
                      "'By GPIO number (legacy)', library version 2.5.7, then wiring.")
            if "Receive only" in line or "bands" in line:
                break
        time.sleep(0.1)
        self.ser.reset_input_buffer()

    def _check_probe(self, quiet: bool) -> bool:
        with self.lock:
            r = self._probe_once(433.92, 5)
        self.probe_ok = r is not None
        if not self.probe_ok and not quiet:
            print(f"  {self.port}: no RSSI reply to 'probe'. Is the board flashed with "
                  "firmware/cc1101_scanner.ino (the version with the probe command)?")
        return self.probe_ok

    def reconnect(self, wait_s: float = 20) -> bool:
        """Native USB re-enumerates after flashing/replugging; the port may move."""
        print(f"Serial link lost; looking for the board for up to {wait_s:.0f} s...")
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.connect(quiet=True, fallback=True):
                print(f"Reconnected on {self.port}.")
                return True
            time.sleep(1)
        print("Board not found. Plug it back in and type: reconnect [port]")
        return False

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def _readline(self) -> str | None:
        raw = self.ser.readline()
        if not raw:
            return None
        return raw.decode("ascii", errors="replace").strip()

    def probe(self, freq: float, dwell_ms: int, retry: bool = True) -> dict | None:
        """Send 'probe <f> <ms>' and return the parsed RSSI line (None on timeout)."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.connected:
                        raise serial.SerialException("not connected")
                    return self._probe_once(freq, dwell_ms)
                except (serial.SerialException, OSError):
                    self.close()
                    if not retry or attempt == 2 or not self.reconnect():
                        raise ConnectionError("serial link lost")
        return None

    def _probe_once(self, freq: float, dwell_ms: int) -> dict | None:
        self.ser.write(f"probe {freq:.4f} {dwell_ms}\n".encode())
        deadline = time.time() + dwell_ms / 1000 + 1.5
        while time.time() < deadline:
            line = self._readline()
            if not line or not line.startswith("RSSI,"):
                continue                    # human-readable output, banner, usage...
            try:
                _, f, peak, avg, n = line.split(",")
                f = float(f)
                if abs(f - freq) > 0.0015:
                    continue                # stale reply to an earlier probe
                return {"freq": round(freq, 4), "peak": int(peak), "avg": int(avg),
                        "samples": int(n), "ts": time.time()}
            except ValueError:
                print(f"  unparseable line: {line!r}")
        print(f"  timeout probing {freq:.4f} MHz, skipped")
        return None


# ---------------------------------------------------------------- storage

class Store:
    """Single writer connection; WAL so the dashboard can read concurrently."""

    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL;")
        self.db.execute("PRAGMA synchronous=NORMAL;")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def insert(self, rows: list[dict], label: str) -> None:
        if not rows:
            return
        with self.lock, self.db:            # one transaction per batch
            self.db.executemany(
                "INSERT INTO readings (ts, freq_mhz, rssi_peak, rssi_avg, samples, sweep_label)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(r["ts"], r["freq"], r["peak"], r["avg"], r["samples"], label) for r in rows])

    def stats(self) -> tuple[int, float | None]:
        with self.lock:
            return self.db.execute("SELECT COUNT(*), MAX(ts) FROM readings").fetchone()

    def export(self, out: Path, minutes: float | None, label: str | None) -> int:
        q, args = "SELECT * FROM readings WHERE 1=1", []
        if minutes:
            q += " AND ts >= ?"
            args.append(time.time() - minutes * 60)
        if label:
            q += " AND sweep_label = ?"
            args.append(label)
        with self.lock:
            cur = self.db.execute(q + " ORDER BY ts", args)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols + ["time_local"])
            for r in rows:
                w.writerow(list(r) + [datetime.fromtimestamp(r[1]).isoformat(timespec="milliseconds")])
        return len(rows)


# ---------------------------------------------------------------- presets

def load_presets(path: Path) -> dict:
    if not path.exists():
        save_presets(path, SEED_PRESETS)
    with open(path) as fh:
        return json.load(fh)


def save_presets(path: Path, presets: dict) -> None:
    with open(path, "w") as fh:
        json.dump(presets, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- jobs + REPL

HELP = """\
sweep START END STEP_KHZ [DWELL_MS]   one pass, probe each step, log it (label "sweep")
sweep PRESET                         one pass of a preset (label = preset name)
watch PRESET [SECONDS]               loop that preset (forever, or SECONDS); Enter or 'stop' ends it
monitor MHZ [DWELL_MS]               probe one frequency repeatedly (label "monitor")
presets                              list presets
addpreset NAME START END STEP_KHZ [DWELL_MS]
rmpreset NAME
export [CSV_PATH] [LAST_MINUTES] [LABEL]   dump readings to CSV (default rfscan_export.csv)
status   ports   reconnect [PORT]   stop   help   quit
While a job runs, press Enter (or type stop) to end it."""


class Collector:
    def __init__(self, board: Board, store: Store, presets_path: Path):
        self.board = board
        self.store = store
        self.presets_path = presets_path
        self.presets = load_presets(presets_path)
        self.running = False      # own flag: Thread.is_alive() can lie after Ctrl+C in join()
        self.job_desc = ""
        self.stop_evt = threading.Event()
        self.last_reading: dict | None = None
        self.passes = 0

    # ---- job plumbing

    @property
    def busy(self) -> bool:
        return self.running

    def start_job(self, desc: str, fn, *args) -> None:
        if self.busy:
            print(f"Busy with '{self.job_desc}'. Press Enter or type stop first.")
            return
        if not self.board.connected and not self.board.connect():
            print("Not connected. Try: ports / reconnect [port]")
            return
        self.stop_evt.clear()
        self.job_desc = desc
        self.passes = 0

        def run():
            try:
                fn(*args)
            except ConnectionError:
                print("Job stopped: serial link lost. Type 'reconnect' when the board is back.")
            except Exception as e:      # keep the REPL alive whatever happens
                print(f"Job crashed: {e!r}")
            finally:
                print(f"[{self.job_desc} finished]")
                self.running = False

        self.running = True
        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        if self.busy:
            self.stop_evt.set()
            deadline = time.time() + 15
            while self.running and time.time() < deadline:
                time.sleep(0.05)

    # ---- radio work (runs in the job thread)

    def _sweep_once(self, freqs: list[float], dwell: int, label: str, verbose: bool) -> list[dict]:
        rows = []
        for f in freqs:
            if self.stop_evt.is_set():
                break
            r = self.board.probe(f, dwell)
            if r is None:
                continue
            rows.append(r)
            self.last_reading = r
            if verbose:
                print(f"  {f:9.4f} MHz  peak {r['peak']:4d}  avg {r['avg']:4d}  |{bar(r['peak'])}")
        self.store.insert(rows, label)       # one transaction per sweep (even a partial one)
        return rows

    def do_sweep(self, freqs, dwell, label):
        rows = self._sweep_once(freqs, dwell, label, verbose=True)
        if rows:
            best = max(rows, key=lambda r: r["peak"])
            print(f"{len(rows)} readings logged as '{label}'. "
                  f"Strongest: {best['freq']:.4f} MHz at {best['peak']} dBm")

    def do_watch(self, name, freqs, dwell, seconds=None):
        ramp = " .:-=+*#%@"
        end = time.time() + seconds if seconds else None
        while not self.stop_evt.is_set() and (end is None or time.time() < end):
            t0 = time.time()
            rows = self._sweep_once(freqs, dwell, name, verbose=False)
            if not rows:
                continue
            self.passes += 1
            peaks = sorted(r["peak"] for r in rows)
            floor = peaks[len(peaks) // 2]
            best = max(rows, key=lambda r: r["peak"])
            spark = "".join(ramp[max(0, min(9, (r["peak"] - floor) // 3))] for r in rows)
            if len(spark) > 60:
                k = len(spark) / 60
                spark = "".join(max(spark[int(i * k):int((i + 1) * k)], key=ramp.index)
                                for i in range(60))
            flag = "  <-- activity" if best["peak"] - floor >= 10 else ""
            print(f"{time.strftime('%H:%M:%S')} #{self.passes:<5d} [{spark}] floor {floor} "
                  f"max {best['peak']} @ {best['freq']:.4f} ({time.time() - t0:.1f}s){flag}")

    def do_monitor(self, freq, dwell):
        batch, last_flush, window_max = [], time.time(), -999
        while not self.stop_evt.is_set():
            r = self.board.probe(freq, dwell)
            if r is not None:
                batch.append(r)
                self.last_reading = r
                window_max = max(window_max, r["peak"])
            if time.time() - last_flush >= 1.0 or self.stop_evt.is_set():
                self.store.insert(batch, "monitor")
                if batch:
                    print(f"{time.strftime('%H:%M:%S')} {freq:.4f} MHz  max {window_max:4d} dBm "
                          f"({len(batch)} probes) |{bar(window_max)}")
                batch, last_flush, window_max = [], time.time(), -999
        self.store.insert(batch, "monitor")

    # ---- commands

    def preset_freqs(self, name: str):
        p = self.presets.get(name)
        if not p:
            print(f"No preset '{name}'. Known: {', '.join(self.presets) or '(none)'}")
            return None
        return sweep_freqs(p["start"], p["end"], p["step_khz"]), int(p.get("dwell_ms", DEFAULT_DWELL_MS))

    def check_range(self, start: float, end: float, step_khz: float, dwell: int) -> bool:
        if not (valid_freq(start) and valid_freq(end)):
            print("CC1101 range: 300-348, 387-464, 779-928 MHz")
            return False
        if end < start or step_khz <= 0 or not 1 <= dwell <= 10000:
            print("Need END >= START, STEP_KHZ > 0 and DWELL_MS in 1-10000")
            return False
        bad = [f for f in sweep_freqs(start, end, step_khz) if not valid_freq(f)]
        if bad:
            print(f"Range crosses a gap the CC1101 can't tune ({bad[0]:.3f} MHz)")
            return False
        return True

    def handle(self, line: str) -> bool:
        """Run one REPL line. Returns False to quit."""
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(e)
            return True
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]
        try:
            return self._dispatch(cmd, args)
        except (ValueError, IndexError):
            print("Bad arguments. Type help.")
            return True

    def _dispatch(self, cmd: str, a: list[str]) -> bool:
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd in ("help", "?"):
            print(HELP)
        elif cmd == "stop":
            if self.busy:
                self.stop()
            else:
                print("Nothing running.")
        elif cmd == "sweep":
            if len(a) == 1:
                pf = self.preset_freqs(a[0])
                if pf:
                    self.start_job(f"sweep {a[0]}", self.do_sweep, pf[0], pf[1], a[0])
                return True
            start, end, step = float(a[0]), float(a[1]), float(a[2])
            dwell = int(a[3]) if len(a) > 3 else DEFAULT_DWELL_MS
            if self.check_range(start, end, step, dwell):
                self.start_job("sweep", self.do_sweep, sweep_freqs(start, end, step), dwell, "sweep")
        elif cmd == "watch":
            pf = self.preset_freqs(a[0])
            if pf:
                secs = float(a[1]) if len(a) > 1 else None
                limit = f" for {secs:g} s" if secs else ""
                print(f"Watching '{a[0]}'{limit}: {len(pf[0])} steps x {pf[1]} ms. Enter stops.")
                self.start_job(f"watch {a[0]}", self.do_watch, a[0], pf[0], pf[1], secs)
        elif cmd == "monitor":
            freq = float(a[0])
            dwell = int(a[1]) if len(a) > 1 else DEFAULT_DWELL_MS
            if self.check_range(freq, freq, 1, dwell):
                print(f"Monitoring {freq:.4f} MHz. Enter stops.")
                self.start_job(f"monitor {freq}", self.do_monitor, round(freq, 4), dwell)
        elif cmd == "presets":
            for name, p in self.presets.items():
                n = len(sweep_freqs(p["start"], p["end"], p["step_khz"]))
                print(f"  {name:<14} {p['start']:.3f}-{p['end']:.3f} MHz  step {p['step_khz']} kHz"
                      f"  dwell {p.get('dwell_ms', DEFAULT_DWELL_MS)} ms  ({n} steps)")
        elif cmd == "addpreset":
            name, start, end, step = a[0], float(a[1]), float(a[2]), float(a[3])
            dwell = int(a[4]) if len(a) > 4 else DEFAULT_DWELL_MS
            if self.check_range(start, end, step, dwell):
                self.presets[name] = {"start": start, "end": end, "step_khz": step, "dwell_ms": dwell}
                save_presets(self.presets_path, self.presets)
                print(f"Saved preset '{name}'.")
        elif cmd == "rmpreset":
            if self.presets.pop(a[0], None) is None:
                print(f"No preset '{a[0]}'.")
            else:
                save_presets(self.presets_path, self.presets)
                print(f"Removed '{a[0]}'.")
        elif cmd == "export":
            out = Path(a[0]) if a else HERE / "rfscan_export.csv"
            minutes = float(a[1]) if len(a) > 1 else None
            label = a[2] if len(a) > 2 else None
            n = self.store.export(out, minutes, label)
            print(f"Wrote {n} rows to {out}")
        elif cmd == "status":
            count, last = self.store.stats()
            print(f"  port:     {self.board.port or '-'} "
                  f"({'connected' if self.board.connected else 'disconnected'}"
                  f"{', probe ok' if self.board.probe_ok else ''})")
            print(f"  job:      {self.job_desc + f' ({self.passes} passes)' if self.busy else 'idle'}")
            print(f"  db:       {self.store.path} ({count} readings"
                  f"{', last ' + time.strftime('%H:%M:%S', time.localtime(last)) if last else ''})")
            if self.last_reading:
                r = self.last_reading
                print(f"  last:     {r['freq']:.4f} MHz peak {r['peak']} avg {r['avg']} dBm")
        elif cmd == "ports":
            for dev, desc, hit in find_ports() or [("(none)", "", False)]:
                print(f"  {dev:<16} {desc}{'   <- looks like the board' if hit else ''}")
        elif cmd == "reconnect":
            if self.busy:
                print("Stop the running job first.")
            else:
                if a:
                    self.board.port_override = a[0]
                self.board.connect()
        else:
            print("Unknown command, type help")
        return True

    def repl(self) -> None:
        print("RF collector ready (receive only). Type help.")
        scripted = not sys.stdin.isatty()     # piped commands: run each job to completion
        while True:
            try:
                while scripted and self.busy:
                    time.sleep(0.2)
                line = input("" if self.busy else "rf> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                if self.busy:
                    print("\nStopping...")
                    self.stop()
                    continue
                print()
                break
            if self.busy and line.strip().lower() in ("", "stop"):
                print("Stopping...")
                self.stop()
                continue
            if self.busy and line.strip().lower().split()[:1] not in (["status"], ["help"], ["?"]):
                print(f"Busy with '{self.job_desc}'. Press Enter (or type stop) first.")
                continue
            if not self.handle(line):
                break
        self.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="CC1101 RF scanner collector (receive only)")
    ap.add_argument("--port", help="serial port (default: auto-detect), e.g. COM4 or /dev/ttyACM0")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite file (default: rfscan.db)")
    ap.add_argument("--presets", type=Path, default=DEFAULT_PRESETS)
    ap.add_argument("-c", "--command", action="append", default=[],
                    help="run a command before the prompt (repeatable), e.g. -c 'watch keyfob'")
    args = ap.parse_args()

    store = Store(args.db)
    board = Board(args.port)
    print("Looking for the board...")
    if not board.connect():
        print("Could not connect yet. Use 'ports' and 'reconnect PORT' (or run with --port).")
    col = Collector(board, store, args.presets)
    for c in args.command:
        col.handle(c)
    try:
        col.repl()
    finally:
        board.close()
        store.db.close()


if __name__ == "__main__":
    main()
