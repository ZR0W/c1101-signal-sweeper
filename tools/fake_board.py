#!/usr/bin/env python3
"""Simulated scanner board for trying the collector/dashboard without hardware.

Linux/macOS only (uses a pseudo-terminal). It answers `probe` like the real
firmware with made-up numbers: a noise floor, a "key fob" near 315.000 MHz that
"presses" every ~20 s, a 433.92 MHz sensor every ~30 s and a 915 MHz hopper.

    python tools/fake_board.py            # prints the port to use
    python collector.py --port /dev/pts/N
"""

import os
import pty
import random
import time
import tty

BANNER = "\r\nCC1101 scanner\r\nCC1101 ready. Receive only.\r\n(simulated board)\r\n"


def signal_at(f: float, t: float) -> float:
    """Simulated peak signal in dBm at frequency f, time t (or -inf)."""
    s = -999.0
    if t % 20 < 1.5:                                # fob press: ~1.5 s burst
        s = max(s, -42 - abs(f - 315.0) * 900)      # ~10 dB down 11 kHz off
    if t % 30 < 0.6:
        s = max(s, -60 - abs(f - 433.92) * 400)
    hop = 902.3 + (int(t * 2) % 50) * 0.5           # hops every 0.5 s
    if 900 < f < 930:
        s = max(s, -75 - abs(f - hop) * 300)
    return s


def probe(f: float, dwell_ms: int) -> str:
    time.sleep(dwell_ms / 1000 + 0.002)
    t = time.time()
    n = max(1, int(dwell_ms / 0.35))
    sig = signal_at(f, t)
    noise = [random.gauss(-97, 2) for _ in range(n)]
    vals = [max(x, sig + random.gauss(0, 1.5)) if random.random() < 0.6 else x for x in noise]
    vals = [int(max(-128, min(-10, v))) for v in vals]
    return f"RSSI,{f:.3f},{max(vals)},{round(sum(vals) / len(vals))},{n}\r\n"


def handle(line: str) -> str:
    parts = line.strip().lower().split()
    if not parts:
        return ""
    if parts[0] == "probe" and len(parts) in (2, 3):
        try:
            f = float(parts[1])
            ms = int(parts[2]) if len(parts) == 3 else 50
            if (300 <= f <= 348 or 387 <= f <= 464 or 779 <= f <= 928) and 1 <= ms <= 10000:
                return probe(f, ms)
        except ValueError:
            pass
    if parts[0].startswith("probe"):
        return "Usage: probe 315.0 [dwell ms, 1-10000, default 50]\r\n"
    return "(simulated board only implements probe)\r\n"


def main() -> None:
    master, slave = pty.openpty()
    tty.setraw(slave)
    print(f"Fake board on {os.ttyname(slave)}  (Ctrl+C to quit)")
    print(f"  python collector.py --port {os.ttyname(slave)}")
    os.write(master, BANNER.encode())
    buf = b""
    while True:
        try:
            data = os.read(master, 1024)
        except OSError:
            time.sleep(0.05)
            continue
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            reply = handle(line.decode(errors="replace"))
            if reply:
                os.write(master, reply.encode())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
