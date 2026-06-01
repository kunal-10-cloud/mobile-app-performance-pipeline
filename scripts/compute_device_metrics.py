#!/usr/bin/env python3
"""
Compute Lighthouse-style device metrics — Android (from Flashlight) AND iOS
(from our `results/ios.json` perf_result, written by `run_ios_perf.sh`).

Flashlight's single 0-100 score is FPS+CPU-only (no memory term, min-FPS
averaged away) — it rated a leaky, occasionally-frozen app ~98. Lighthouse's
value was never the composite number; it was the named, individually-rated
metrics. This produces that: each metric gets a value, a target, and a
🟢/🟡/🔴 rating, so the reader sees exactly what's good and what's broken.

Inputs:
  flashlight `test` JSON — { iterations: [ { measures: [ {fps, ram, cpu:{perName,perCore}, time} ... ] } ] }

Outputs (under ${AUDIT_DIR}/results/):
  device_lighthouse.json — [ {metric, value, display, target, rating, insight} ]  (drives the report dashboard)
  android.json           — normalized perf_result (drives transform_device_metrics → findings)

Usage:
  python3 scripts/compute_device_metrics.py <audit_id> <flashlight_test.json> [--platform android] [--device-profile "..."]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

# ── Metric thresholds (good ≤/≥ ; mid) tuned for a low-end device ─────────────
# (name, higher_is_better, good, mid, unit, target_str, insight_when_bad)
METRIC_SPECS = [
    ("Mean FPS",                 True,  55, 45, "fps", "≥ 55 fps",
     "Sustained FPS below 55 is perceptibly less smooth than 60 Hz."),
    ("Worst-frame FPS (min)",    True,  30, 15, "fps", "≥ 30 fps",
     "A single very low frame is a visible freeze — find the screen/interaction that caused it."),
    ("Jank ratio (frames < 50 fps)", False, 5, 15, "pct", "< 5%",
     "A high share of dropped frames means scrolling/transitions stutter."),
    ("Frozen frames (< 10 fps)", False, 1, 5, "pct", "< 1%",
     "Frames under 10 fps read as the app hanging."),
    ("Peak memory",              False, 300, 500, "mb", "< 300 MB",
     "High peak RSS risks low-memory kills on cheap phones."),
    ("Memory growth / run",      False, 20, 80, "mb", "< 20 MB",
     "Memory that climbs each pass through the flow indicates a leak (listeners, caches, retained screens)."),
    ("Avg total CPU",            False, 60, 120, "cpu", "< 60%",
     "Sustained high CPU drains battery and heats the device."),
    ("Peak JS-thread CPU",       False, 70, 90, "cpu", "< 70%",
     "JS-thread saturation is the classic RN bottleneck — audit effects, list work, and serialization."),
    ("Single-thread saturation time (> 80%)", False, 5, 20, "pct", "< 5%",
     "Time with any one thread pegged correlates with frame drops."),
]

KEY = {  # internal short keys for the perf_result mapping
    "Mean FPS": "mean_fps", "Worst-frame FPS (min)": "min_fps",
    "Jank ratio (frames < 50 fps)": "jank_ratio", "Frozen frames (< 10 fps)": "frozen_ratio",
    "Peak memory": "peak_mb", "Memory growth / run": "growth_mb",
    "Avg total CPU": "avg_cpu", "Peak JS-thread CPU": "peak_js_cpu",
    "Single-thread saturation time (> 80%)": "sat_time",
}


def _mean(xs): return sum(xs) / len(xs) if xs else 0.0
def _pct(flags): return 100.0 * sum(1 for f in flags if f) / len(flags) if flags else 0.0


def compute(flashlight: dict) -> tuple[list[dict], dict]:
    its = flashlight.get("iterations") or []
    fps_all, cpu_tot, js_thread, busiest = [], [], [], []
    growths, peaks = [], []
    norm_iters = []
    for idx, it in enumerate(its):
        ms = it.get("measures") or []
        rams = [m["ram"] for m in ms if isinstance(m.get("ram"), (int, float))]
        i_fps = [m["fps"] for m in ms if isinstance(m.get("fps"), (int, float))]
        if len(rams) > 1:
            growths.append(rams[-1] - rams[0])
        if rams:
            peaks.append(max(rams))
        thread_tot, nsamp = {}, 0
        for m in ms:
            if isinstance(m.get("fps"), (int, float)):
                fps_all.append(m["fps"])
            per = (m.get("cpu") or {}).get("perName") or {}
            if per:
                nsamp += 1
                cpu_tot.append(sum(v for v in per.values() if isinstance(v, (int, float))))
                js_thread.append(per.get("mqt_v_js", 0))
                busiest.append(max(per.values()))
                for t, v in per.items():
                    if isinstance(v, (int, float)):
                        thread_tot[t] = thread_tot.get(t, 0) + v
        norm_iters.append({
            "index": idx,
            "fps": {"average": round(_mean(i_fps), 1) if i_fps else None,
                    "min": round(min(i_fps), 1) if i_fps else None,
                    "max": round(max(i_fps), 1) if i_fps else None},
            "cpu_per_thread": {t: round(v / nsamp, 1) for t, v in thread_tot.items() if nsamp and v / nsamp >= 1.0},
            "memory_mb": {"average": round(_mean(rams), 1) if rams else None,
                          "peak": round(max(rams), 1) if rams else None,
                          "growth": round(rams[-1] - rams[0], 1) if len(rams) > 1 else 0.0},
            "blocking_intervals_ms": [],
        })

    vals = {
        "mean_fps": round(_mean(fps_all), 1),
        "min_fps": round(min(fps_all), 1) if fps_all else 0,
        "jank_ratio": round(_pct([f < 50 for f in fps_all]), 1),
        "frozen_ratio": round(_pct([f < 10 for f in fps_all]), 2),
        "peak_mb": round(max(peaks), 0) if peaks else 0,
        "growth_mb": round(_mean(growths), 0) if growths else 0,
        "avg_cpu": round(_mean(cpu_tot), 1),
        "peak_js_cpu": round(max(js_thread), 1) if js_thread else 0,
        "sat_time": round(_pct([b > 80 for b in busiest]), 1),
    }
    unit_suffix = {"fps": " fps", "pct": "%", "mb": " MB", "cpu": "%"}
    rows = []
    for name, higher, good, mid, unit, target, insight in METRIC_SPECS:
        v = vals[KEY[name]]
        if higher:
            rating = "good" if v >= good else ("needs" if v >= mid else "poor")
        else:
            rating = "good" if v <= good else ("needs" if v <= mid else "poor")
        rows.append({
            "metric": name, "value": v, "display": f"{v}{unit_suffix[unit]}",
            "target": target, "rating": rating,
            "insight": insight if rating != "good" else "",
        })
    return rows, norm_iters


# ── iOS metric spec ──────────────────────────────────────────────────────────
# The set is intentionally smaller than Android — only the metrics that survive
# the Simulator-vs-device reality check. FPS is omitted by design (Mac GPU is
# not iPhone-comparable regardless of chip family). Each metric carries a
# `reliability` label so the renderer can label rows honestly.
#
#   "reliable"             → matches device behavior (memory growth, crashes)
#   "device-class estimate" → useful absolute number on Apple Silicon; rough on Intel
#   "directional"           → comparable threshold-crossings, not absolute %
#   "regression-relative"   → only meaningful when compared to another build's number
IOS_METRIC_SPECS = [
    # (name, higher_is_better, good_threshold, mid_threshold, unit, target_str,
    #  insight_when_bad, reliability_on_apple_silicon, reliability_on_intel)
    ("Cold start (Simulator launch)",     False, 1500, 2500, "ms", "< 1500 ms",
     "Cold start above 1.5 s feels slow to launch. Apply MOB-007 (bundle trim) first.",
     "device-class estimate", "regression-relative"),
    ("Peak memory",                       False, 300, 500, "mb", "< 300 MB",
     "High peak RSS risks low-memory kills on older iPhones (8 GB or less of RAM).",
     "device-class estimate", "directional"),
    ("Memory growth / run",               False, 20, 80, "mb", "< 20 MB",
     "Memory that climbs each pass through the flow indicates a leak (listeners, caches, retained screens).",
     "reliable", "reliable"),
    ("Memory iterations measured",        True,  3, 1, "count", "≥ 3 iterations",
     "More iterations make the growth signal more confident.",
     "reliable", "reliable"),
]


def compute_ios(perf_result: dict) -> list[dict]:
    """Build the iOS Lighthouse-style row set from results/ios.json."""
    env = perf_result.get("measurement_environment") or {}
    on_apple_silicon = bool(env.get("on_apple_silicon"))

    iters = perf_result.get("iterations") or []
    mem_peaks = [it.get("memory_mb", {}).get("peak") for it in iters]
    mem_peaks = [v for v in mem_peaks if isinstance(v, (int, float))]
    mem_growths = [it.get("memory_mb", {}).get("growth") for it in iters]
    mem_growths = [v for v in mem_growths if isinstance(v, (int, float))]

    cold_start_ms = perf_result.get("startup_time_ms")
    peak_mb = max(mem_peaks) if mem_peaks else None
    growth_mb = sum(mem_growths) / len(mem_growths) if mem_growths else None
    iter_count = len(iters)

    vals = {
        "Cold start (Simulator launch)": cold_start_ms,
        "Peak memory":                   peak_mb,
        "Memory growth / run":           growth_mb,
        "Memory iterations measured":    iter_count,
    }
    unit_suffix = {"fps": " fps", "pct": "%", "mb": " MB", "cpu": "%", "ms": " ms", "count": ""}
    rows: list[dict] = []
    for name, higher, good, mid, unit, target, insight, rel_as, rel_intel in IOS_METRIC_SPECS:
        v = vals.get(name)
        if v is None:
            rating = "not_measured"
            display = "—"
        else:
            if higher:
                rating = "good" if v >= good else ("needs" if v >= mid else "poor")
            else:
                rating = "good" if v <= good else ("needs" if v <= mid else "poor")
            if isinstance(v, float):
                display = f"{v:.1f}{unit_suffix[unit]}"
            else:
                display = f"{v}{unit_suffix[unit]}"
        rows.append({
            "metric": name,
            "value": v,
            "display": display,
            "target": target,
            "rating": rating,
            "reliability": rel_as if on_apple_silicon else rel_intel,
            "insight": insight if rating in ("needs", "poor") else "",
        })

    # Append two explicit "skipped by design" rows so the report's table doesn't
    # silently omit the metrics readers expect to see for parity with Android.
    rows.append({
        "metric": "Mean FPS / worst-frame FPS",
        "value": None, "display": "—",
        "target": "—",
        "rating": "skipped",
        "reliability": "unreliable on Simulator",
        "insight": "Mac GPU ≠ iPhone GPU even on Apple Silicon; absolute FPS does not translate. Measure on a real iPhone via Xcode Instruments for device-quality FPS.",
    })
    rows.append({
        "metric": "Thermal throttling / sustained perf",
        "value": None, "display": "—",
        "target": "—",
        "rating": "skipped",
        "reliability": "not modeled",
        "insight": "Mac has active cooling; iPhone throttles after ~30s of sustained CPU. Real-device profiling required.",
    })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute Lighthouse-style device metrics from Android Flashlight or iOS perf_result.")
    ap.add_argument("audit_id")
    ap.add_argument("input_json", type=Path, help="Flashlight test JSON (Android) OR results/ios.json (iOS).")
    ap.add_argument("--platform", default="android", choices=("android", "ios"))
    ap.add_argument("--device-profile", default="emulator (low-end profile)")
    ap.add_argument("--bundle-id", default="")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs")) / args.audit_id
    results = base / "results"
    results.mkdir(parents=True, exist_ok=True)
    if not args.input_json.is_file():
        print(f"ERROR: {args.input_json} not found", file=sys.stderr)
        return 2

    payload = json.loads(args.input_json.read_text(encoding="utf-8"))

    if args.platform == "android":
        rows, norm_iters = compute(payload)
        out_file = "device_lighthouse.json"
        perf_result_out = {
            "platform": "android",
            "device_profile": args.device_profile,
            "bundle_id": args.bundle_id,
            "captured_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "score": None, "startup_time_ms": None,
            "iterations": norm_iters, "intervals": [],
            "raw_artifact_path": str(args.input_json),
            "tool_warnings": [
                "Flashlight's single 0-100 score is FPS+CPU-only (no memory term); use the per-metric breakdown.",
            ],
        }
        (results / "android.json").write_text(json.dumps(perf_result_out, indent=2), encoding="utf-8")
    else:
        # iOS: input is our perf_result JSON (already written by run_ios_perf.sh).
        rows = compute_ios(payload)
        out_file = "device_lighthouse_ios.json"

    lighthouse = {
        "platform": args.platform,
        "device_profile": payload.get("device_profile") or args.device_profile,
        "metrics": rows,
        "measurement_environment": payload.get("measurement_environment") or {},
        "tool_warnings": payload.get("tool_warnings") or [],
    }
    (results / out_file).write_text(json.dumps(lighthouse, indent=2), encoding="utf-8")

    poor = [r["metric"] for r in rows if r["rating"] == "poor"]
    needs = [r["metric"] for r in rows if r["rating"] == "needs"]
    print(f"{args.platform} metrics: {len(rows)} rows  | poor: {poor or '-'}  | needs-work: {needs or '-'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
