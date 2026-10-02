#!/usr/bin/env python3
"""Check the flashed firmware over USB: the new `probe` command and the old commands.

Close the Arduino Serial Monitor (and any collector) first: one port, one owner.
Receive only: every command sent here only listens.

    python tools/firmware_check.py            # auto-detect the board
    python tools/firmware_check.py --port COM5

Prints PASS/FAIL per check and exits non-zero if anything failed.
"""

import argparse
import re
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector import BAUD, find_ports  # noqa: E402

RSSI_RE = re.compile(r"^RSSI,(\d+\.\d{3}),(-?\d+),(-?\d+),(\d+)$")
USAGE = "Usage: probe"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def read_for(ser, seconds):
    """All lines received within `seconds`."""
    end, lines = time.time() + seconds, []
    while time.time() < end:
        raw = ser.readline()
        if raw:
            lines.append(raw.decode("ascii", errors="replace").rstrip("\r\n"))
    return lines


def read_until(ser, pred, seconds):
    """Lines up to and including the first one matching pred (None on timeout)."""
    end, lines = time.time() + seconds, []
    while time.time() < end:
        raw = ser.readline()
        if raw:
            lines.append(raw.decode("ascii", errors="replace").rstrip("\r\n"))
            if pred(lines[-1]):
                return lines
    return None


def send(ser, cmd, seconds):
    ser.reset_input_buffer()
    ser.write((cmd + "\n").encode())
    return read_for(ser, seconds)


def stop_and_collect(ser, seconds=1.5):
    """Send a key (the firmware's 'any key stops') and return what follows."""
    ser.write(b"x\n")
    return read_for(ser, seconds)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    args = ap.parse_args()
    port = args.port
    if not port:
        boards = [d for d, _, hit in find_ports() if hit]
        if not boards:
            sys.exit("No board-like serial port found. Plug it in or pass --port.")
        port = boards[0]
    print(f"Opening {port} @ {BAUD}")
    ser = serial.Serial(port, BAUD, timeout=0.1)
    banner = read_for(ser, 3)
    if banner:
        print("  banner/backlog:", " | ".join(l for l in banner if l.strip())[:300])
    check("no 'CC1101 not found' in banner", not any("not found" in l for l in banner),
          "fix pin numbering (GPIO legacy) / library 2.5.7 / wiring" if banner else "")

    # ---- probe: format
    lines = send(ser, "probe 315.0 50", 1.0)
    rssi = [l for l in lines if l.startswith("RSSI,")]
    check("probe 315.0 50 -> exactly one RSSI line", len(rssi) == 1, repr(lines))
    m = RSSI_RE.match(rssi[0]) if rssi else None
    check("RSSI line format RSSI,<f.3>,<peak>,<avg>,<n>", bool(m), rssi[0] if rssi else "")
    n50 = 0
    if m:
        peak, avg, n50 = int(m[2]), int(m[3]), int(m[4])
        check("frequency echoed as 315.000", m[1] == "315.000", m[1])
        check("peak >= avg, both in -128..0 dBm", -128 <= avg <= peak <= 0, f"peak {peak} avg {avg}")
        check("samples > 10 for 50 ms", n50 > 10, str(n50))
        check("no extra output besides the RSSI line", len([l for l in lines if l.strip()]) == 1, repr(lines))

    # ---- probe: defaults, dwell scaling, other bands
    lines = send(ser, "probe 433.92", 1.0)
    check("probe 433.92 (default dwell) -> RSSI line", any(RSSI_RE.match(l) for l in lines), repr(lines))
    lines = send(ser, "probe 315.0 200", 1.5)
    m2 = next((RSSI_RE.match(l) for l in lines if RSSI_RE.match(l)), None)
    check("200 ms dwell takes ~4x the samples of 50 ms", bool(m2) and n50 and 2.5 < int(m2[4]) / n50 < 6,
          f"{m2[4] if m2 else '-'} vs {n50}")
    lines = send(ser, "probe 915.0 20", 1.0)
    check("probe 915.0 20 -> RSSI line", any(RSSI_RE.match(l) for l in lines), repr(lines))
    t0, answered = time.time(), 0
    for _ in range(20):
        ser.write(b"probe 315.0 10\n")
        if read_until(ser, lambda l: RSSI_RE.match(l), 1.0):
            answered += 1
    check("20 back-to-back probes answered", answered == 20,
          f"{answered}/20, {(time.time() - t0) / 20 * 1000:.0f} ms each")

    # ---- probe: malformed input -> usage, never an RSSI line
    for cmd in ["probe?", "probe ?", "probe", "probe 200 50", "probe 315 abc", "probe 315 0"]:
        lines = send(ser, cmd, 0.6)
        ok = any(l.startswith(USAGE) for l in lines) and not any(l.startswith("RSSI,") for l in lines)
        check(f"'{cmd}' -> usage line, no RSSI", ok, repr(lines))

    # ---- existing commands unchanged
    lines = send(ser, "bands", 0.6)
    check("bands lists 315/433/868/915",
          all(any(b in l for l in lines) for b in ("315:", "433:", "868:", "915:")), repr(lines))
    lines = send(ser, "?", 0.6)
    text = "\n".join(lines)
    check("? help still lists scan/listen/rssi/bands", all(k in text for k in ("scan 433", "listen MHZ", "rssi MHZ", "bands")))
    check("? help lists probe", "probe MHZ" in text)
    lines = send(ser, "rssi 433.92", 1.5)
    meter = [l for l in lines if re.match(r"^\s*-?\d+ dBm \|", l)]
    check("rssi 433.92 shows a live meter (~5 lines/s)", len(meter) >= 3, f"{len(meter)} meter lines")
    after = stop_and_collect(ser)
    check("rssi stops on a key ('Stopped.')", any("Stopped." in l for l in after), repr(after))
    lines = send(ser, "scan 433 2", 4.0)
    text = "\n".join(lines)
    check("scan 433 2 finishes with noise floor + strongest signals",
          "sweeps done" in text and "Noise floor" in text and "Strongest signals" in text, repr(lines[-3:]))
    lines = send(ser, "listen 433.92", 1.0)
    check("listen 433.92 starts", any("Listening on 433.920" in l for l in lines), repr(lines))
    after = stop_and_collect(ser)
    check("listen stops on a key ('Stopped.')", any("Stopped." in l for l in after), repr(after))
    lines = send(ser, "frobnicate", 0.6)
    check("unknown command message unchanged", any("Unknown command" in l for l in lines), repr(lines))

    # ---- radio is usable again after the human commands
    lines = send(ser, "probe 315.0 50", 1.0)
    check("probe works after scan/listen/rssi", any(RSSI_RE.match(l) for l in lines), repr(lines))

    ser.close()
    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
