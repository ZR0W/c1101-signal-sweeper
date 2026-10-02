#!/usr/bin/env python3
"""RF scanner dashboard: visualizes rfscan.db in a browser.

Reads the SQLite file only (read-only connection, never the serial port), so it
can run alongside collector.py, which is the single writer.

    flask --app dashboard run          # then open http://127.0.0.1:5000
    RFSCAN_DB=/path/to/rfscan.db flask --app dashboard run
"""

from __future__ import annotations

import os
import sqlite3
import statistics
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RFSCAN_DB", HERE / "rfscan.db"))
ACTIVITY_MARGIN_DB = 10       # a reading this far above the noise floor counts as activity
MAX_WATERFALL_ROWS = 300      # older sweeps are merged (max-hold) beyond this
EVENT_GAP_S = 1.0             # activity hits closer than this merge into one detection
FREQ_TOL = 0.0005             # MHz; frequencies are stored rounded to 0.1 kHz

app = Flask(__name__)


# ---------------------------------------------------------------- helpers

def noise_floor(peaks: list[int]) -> float | None:
    """Noise floor = median peak over the window. The one place this is defined."""
    return float(statistics.median(peaks)) if peaks else None


def floor_info(peaks: list[int]) -> dict:
    """Floor + activity threshold, as every API reports them (the UI never recomputes)."""
    nf = noise_floor(peaks)
    return {"noise_floor": nf,
            "threshold": None if nf is None else nf + ACTIVITY_MARGIN_DB,
            "margin_db": ACTIVITY_MARGIN_DB}


def query(sql: str, args: tuple = ()) -> list[tuple]:
    """Run a read-only query; an absent or empty DB just yields no rows."""
    if not DB_PATH.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return []
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:       # e.g. table not created yet
        return []
    finally:
        con.close()


def arg_float(name: str, default: float) -> float:
    try:
        return float(request.args.get(name, default))
    except ValueError:
        return default


def window_start(default_minutes: float = 30) -> float:
    return time.time() - max(0.1, arg_float("minutes", default_minutes)) * 60


def latest_label() -> str | None:
    rows = query("SELECT sweep_label FROM readings ORDER BY ts DESC LIMIT 1")
    return rows[0][0] if rows else None


def selected_label() -> str | None:
    return request.args.get("label") or latest_label()


def fmt_time(ts: float) -> str:
    """Local wall-clock time; browser and server are the same machine."""
    return datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="milliseconds")


def split_runs(rows: list[tuple]) -> list[list[tuple]]:
    """Split time-ordered (ts, freq, ...) rows into sweep passes.

    Sweeps step upward in frequency, so a pass ends when the frequency stops
    increasing. (A monitor run on one frequency makes every reading its own pass.)
    """
    runs: list[list[tuple]] = []
    prev = None
    for r in rows:
        if prev is None or r[1] <= prev:
            runs.append([])
        runs[-1].append(r)
        prev = r[1]
    return runs


def window_peaks(label: str | None, since: float) -> list[int]:
    return [r[0] for r in query(
        "SELECT rssi_peak FROM readings WHERE sweep_label IS ? AND ts >= ?", (label, since))]


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return render_template("index.html", margin_db=ACTIVITY_MARGIN_DB)


@app.route("/plotly.min.js")
def plotly_js():
    """Offline fallback for the Plotly CDN, served from the plotly Python package."""
    from plotly.offline import get_plotlyjs
    return Response(get_plotlyjs(), mimetype="application/javascript",
                    headers={"Cache-Control": "max-age=86400"})


@app.route("/api/summary")
def api_summary():
    """Labels present in the DB, plus floor/threshold for the selected label + window."""
    since = window_start()
    labels = [{"label": l, "count": n, "last": fmt_time(t), "last_ts": t, "fmin": a, "fmax": b}
              for l, n, t, a, b in query(
                  "SELECT sweep_label, COUNT(*), MAX(ts), MIN(freq_mhz), MAX(freq_mhz)"
                  " FROM readings GROUP BY sweep_label ORDER BY MAX(ts) DESC")]
    label = selected_label()
    total = sum(l["count"] for l in labels)
    return jsonify({"labels": labels, "label": label, "total_rows": total,
                    "db": str(DB_PATH), "now": fmt_time(time.time()),
                    **floor_info(window_peaks(label, since))})


@app.route("/api/latest_sweep")
def api_latest_sweep():
    """Latest value per frequency from the most recent pass of a label: [{freq, peak, avg}].

    If the newest pass is still in progress, frequencies it hasn't reached yet
    come from the pass before, so the spectrum doesn't flicker.
    """
    label = selected_label()
    rows = query("SELECT ts, freq_mhz, rssi_peak, rssi_avg FROM readings WHERE sweep_label IS ?"
                 " ORDER BY ts DESC LIMIT 5000", (label,))
    runs = split_runs(rows[::-1])[-2:]
    latest: dict[float, dict] = {}
    for run in runs:                          # older pass first, newer overwrites
        for ts, f, peak, avg in run:
            latest[f] = {"freq": f, "peak": peak, "avg": avg}
    return jsonify(sorted(latest.values(), key=lambda d: d["freq"]))


@app.route("/api/waterfall")
def api_waterfall():
    """Time x frequency grid of peak dBm for a Plotly heatmap: {times, freqs, z}."""
    label = selected_label()
    since = window_start(30)
    rows = query("SELECT ts, freq_mhz, rssi_peak FROM readings WHERE sweep_label IS ? AND ts >= ?"
                 " ORDER BY ts", (label, since))
    freqs = sorted({r[1] for r in rows})
    col = {f: i for i, f in enumerate(freqs)}
    runs = split_runs(rows)
    group = max(1, -(-len(runs) // MAX_WATERFALL_ROWS))   # merge passes when there are too many
    times, z = [], []
    for g in range(0, len(runs), group):
        line: list[int | None] = [None] * len(freqs)
        for run in runs[g:g + group]:
            for _, f, peak in run:
                i = col[f]
                if line[i] is None or peak > line[i]:
                    line[i] = peak
        times.append(fmt_time(runs[g][0][0]))
        z.append(line)
    return jsonify({"label": label, "times": times, "freqs": freqs, "z": z,
                    "passes": len(runs), "passes_per_row": group,
                    **floor_info([r[2] for r in rows])})


@app.route("/api/activity")
def api_activity():
    """Recent detections well above their label's noise floor, newest first."""
    limit = int(max(1, min(500, arg_float("limit", 50))))
    since = window_start(60)
    label = request.args.get("label")
    sql = "SELECT ts, freq_mhz, rssi_peak, rssi_avg, sweep_label FROM readings WHERE ts >= ?"
    args: tuple = (since,)
    if label:
        sql += " AND sweep_label IS ?"
        args += (label,)
    sql += " ORDER BY ts"
    by_label: dict[str | None, list[tuple]] = {}
    for r in query(sql, args):
        by_label.setdefault(r[4], []).append(r)
    events = []
    for lbl, rows in by_label.items():
        info = floor_info([r[2] for r in rows])
        nf = info["noise_floor"]
        hits = [r for r in rows if r[2] >= info["threshold"]]      # rows are time-ordered
        # One transmission lights up several neighbouring bins (and maybe the next pass):
        # hits less than EVENT_GAP_S apart become one detection, reported at its strongest bin.
        group: list[tuple] = []
        for h in hits + [None]:
            if group and (h is None or h[0] - group[-1][0] > EVENT_GAP_S):
                ts, f, peak, avg, _ = max(group, key=lambda r: r[2])
                events.append({"ts": ts, "time": fmt_time(ts), "label": lbl, "freq": f,
                               "peak": peak, "avg": avg, "noise_floor": nf,
                               "above_db": round(peak - nf, 1), "readings": len(group),
                               "freq_lo": min(r[1] for r in group), "freq_hi": max(r[1] for r in group),
                               "start": fmt_time(group[0][0]), "end": fmt_time(group[-1][0])})
                group = []
            if h is not None:
                group.append(h)
    events.sort(key=lambda e: e["ts"], reverse=True)
    return jsonify(events[:limit])


@app.route("/api/freq_history")
def api_freq_history():
    """One frequency over time: {freq, times, peak, avg} (+ floor/threshold)."""
    freq = arg_float("freq", 0)
    since = window_start(60)
    label = request.args.get("label")
    sql = ("SELECT ts, rssi_peak, rssi_avg FROM readings"
           " WHERE freq_mhz BETWEEN ? AND ? AND ts >= ?")
    args: tuple = (freq - FREQ_TOL, freq + FREQ_TOL, since)
    if label:
        sql += " AND sweep_label IS ?"
        args += (label,)
    rows = query(sql + " ORDER BY ts", args)
    # Floor from the whole label window when known, so it matches the other panels.
    peaks = window_peaks(label, since) if label else [r[1] for r in rows]
    return jsonify({"freq": freq, "label": label,
                    "times": [fmt_time(r[0]) for r in rows],
                    "peak": [r[1] for r in rows], "avg": [r[2] for r in rows],
                    **floor_info(peaks)})


if __name__ == "__main__":
    app.run(debug=False)
