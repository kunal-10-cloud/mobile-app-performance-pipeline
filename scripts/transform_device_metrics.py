#!/usr/bin/env python3
"""
Transform a normalised perf_result.json (matching schemas/perf_result.schema.json)
into the audit's Finding shape.

Thresholds match the architecture's targets for low-end devices. Severity
scales with how badly the measurement misses the threshold.

  FPS  (average across iterations, lower is worse):
    avg <  30  → critical
    avg <  45  → high
    avg <  55  → medium
    else       → no finding

  Startup time (ms, cold start):
    > 4000 → critical
    > 2500 → high
    > 1500 → medium

  Memory growth (MB across iterations, positive = leak suspect):
    > 30 → high
    > 10 → medium

  CPU per thread (mean % over iteration, biggest offender per iteration):
    > 90 → high
    > 70 → medium

  Blocking intervals (any interval > 500 ms anywhere):
    > 1000 → high
    > 500  → medium

Emits findings under layer = device_android / device_ios depending on
result.platform. Each finding carries metric_name / metric_value /
metric_threshold so Pass A can re-verify.

Usage:
  python3 scripts/transform_device_metrics.py <audit_id> <perf_result.json>
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


# ── Thresholds ───────────────────────────────────────────────────────────────
FPS_CRITICAL = 30
FPS_HIGH = 45
FPS_MEDIUM = 55

STARTUP_CRITICAL_MS = 4000
STARTUP_HIGH_MS = 2500
STARTUP_MEDIUM_MS = 1500

MEMORY_GROWTH_HIGH_MB = 30
MEMORY_GROWTH_MEDIUM_MB = 10

CPU_HIGH_PCT = 90
CPU_MEDIUM_PCT = 70

BLOCKING_INTERVAL_HIGH_MS = 1000
BLOCKING_INTERVAL_MEDIUM_MS = 500


def severity_for_fps(avg: float) -> str | None:
    if avg < FPS_CRITICAL: return "critical"
    if avg < FPS_HIGH:     return "high"
    if avg < FPS_MEDIUM:   return "medium"
    return None


def severity_for_startup(ms: int) -> str | None:
    if ms > STARTUP_CRITICAL_MS: return "critical"
    if ms > STARTUP_HIGH_MS:     return "high"
    if ms > STARTUP_MEDIUM_MS:   return "medium"
    return None


def severity_for_growth(mb: float) -> str | None:
    if mb > MEMORY_GROWTH_HIGH_MB:   return "high"
    if mb > MEMORY_GROWTH_MEDIUM_MB: return "medium"
    return None


def severity_for_cpu(pct: float) -> str | None:
    if pct > CPU_HIGH_PCT:   return "high"
    if pct > CPU_MEDIUM_PCT: return "medium"
    return None


def severity_for_blocking(ms: float) -> str | None:
    if ms > BLOCKING_INTERVAL_HIGH_MS:   return "high"
    if ms > BLOCKING_INTERVAL_MEDIUM_MS: return "medium"
    return None


def make_finding(rule_id: str, layer: str, *,
                 category: str, severity: str, title: str, description: str,
                 metric_name: str, metric_value, metric_threshold,
                 file_path: str = "results/", function: str = "") -> dict:
    return {
        "id": rule_id,
        "layer": layer,
        "category": category,
        "severity": severity,
        "confidence": "high",  # device measurements are direct evidence
        "title": title,
        "description": description,
        "evidence": {
            "file": file_path,
            "function": function or layer.replace("device_", "device "),
            "metric_name": metric_name,
            "metric_value": metric_value,
            "metric_threshold": metric_threshold,
        },
    }


def transform(result: dict) -> list[dict]:
    platform = result.get("platform")
    if platform not in ("android", "ios"):
        return []
    layer = f"device_{platform}"
    findings: list[dict] = []
    iterations = result.get("iterations") or []
    intervals = result.get("intervals") or []
    device_profile = result.get("device_profile") or "unknown"
    pretty_platform = "Android" if platform == "android" else "iOS"

    # 1) FPS across iterations
    fps_avgs = []
    for it in iterations:
        v = (it.get("fps") or {}).get("average")
        if isinstance(v, (int, float)):
            fps_avgs.append(float(v))
    if fps_avgs:
        mean_fps = statistics.fmean(fps_avgs)
        sev = severity_for_fps(mean_fps)
        if sev:
            findings.append(make_finding(
                f"device.fps_below_threshold",
                layer,
                category="runtime_jank",
                severity=sev,
                title=f"{pretty_platform} mean FPS {mean_fps:.1f} on {device_profile}",
                description=(
                    f"Mean FPS across {len(fps_avgs)} iterations was {mean_fps:.1f}. "
                    "Sustained FPS below 55 is perceptibly jankier than 60-Hz smooth motion; below 30 the app feels unresponsive. "
                    "Cross-reference the per-interval data in `results/{platform}.json` to identify the worst Maestro step "
                    "and the screen that drove the drop."
                ),
                metric_name="fps_avg",
                metric_value=round(mean_fps, 2),
                metric_threshold=FPS_MEDIUM,
                file_path=f"results/{platform}.json",
            ))

    # 2) Startup
    startup = result.get("startup_time_ms")
    if isinstance(startup, int):
        sev = severity_for_startup(startup)
        if sev:
            findings.append(make_finding(
                "device.startup_too_slow",
                layer,
                category="startup",
                severity=sev,
                title=f"{pretty_platform} cold start {startup} ms",
                description=(
                    f"Cold start measured at {startup} ms on {device_profile}. "
                    "Users abandon at ~3 s; the perceived threshold for 'instant' is closer to 1 s. "
                    "Biggest startup levers: enable Hermes (if not on), trim the JS bundle, lazy-load non-first-screen routes."
                ),
                metric_name="startup_time_ms",
                metric_value=startup,
                metric_threshold=STARTUP_MEDIUM_MS,
                file_path=f"results/{platform}.json",
            ))

    # 3) Memory growth across iterations (leak signal)
    for it in iterations:
        growth = (it.get("memory_mb") or {}).get("growth")
        if not isinstance(growth, (int, float)) or growth <= 0:
            continue
        sev = severity_for_growth(float(growth))
        if sev:
            findings.append(make_finding(
                "device.memory_growth_suspected_leak",
                layer,
                category="memory",
                severity=sev,
                title=f"{pretty_platform} memory grew {growth:.1f} MB in iteration {it.get('index')}",
                description=(
                    f"Iteration {it.get('index')} on {device_profile} ended {growth:.1f} MB higher than it started. "
                    "Sustained growth across iterations indicates a leak: listener never unsubscribed, "
                    "screen retained after pop, in-memory cache without bound. Run the flow once more in DevTools' "
                    "Memory profiler to capture a heap snapshot diff."
                ),
                metric_name="memory_growth_mb",
                metric_value=round(float(growth), 2),
                metric_threshold=MEMORY_GROWTH_MEDIUM_MB,
                file_path=f"results/{platform}.json",
            ))

    # 4) CPU per thread (Android only — iOS isn't captured today)
    for it in iterations:
        cpu = (it.get("cpu_per_thread") or {})
        if not isinstance(cpu, dict):
            continue
        for thread, pct in cpu.items():
            if not isinstance(pct, (int, float)):
                continue
            sev = severity_for_cpu(float(pct))
            if sev is None:
                continue
            findings.append(make_finding(
                "device.cpu_thread_saturated",
                layer,
                category="runtime_jank",
                severity=sev,
                title=f"{pretty_platform} thread `{thread}` averaged {pct:.0f}% CPU",
                description=(
                    f"Thread `{thread}` ran at {pct:.0f}% mean CPU during iteration {it.get('index')} on {device_profile}. "
                    "Sustained CPU above ~70% on one thread means render or JS work is the bottleneck on cheap phones. "
                    "If this is the JS thread, audit list virtualization and effects. If it's the UI thread, "
                    "review Reanimated worklets and native module calls."
                ),
                metric_name="cpu_thread_pct",
                metric_value=round(float(pct), 1),
                metric_threshold=CPU_MEDIUM_PCT,
                file_path=f"results/{platform}.json",
                function=thread,
            ))

    # 5) Long blocking intervals
    for it in iterations:
        for ms in (it.get("blocking_intervals_ms") or []):
            if not isinstance(ms, (int, float)):
                continue
            sev = severity_for_blocking(float(ms))
            if sev is None:
                continue
            findings.append(make_finding(
                "device.long_blocking_interval",
                layer,
                category="runtime_jank",
                severity=sev,
                title=f"{pretty_platform} blocking interval {ms:.0f} ms",
                description=(
                    f"A main-thread blocking interval of {ms:.0f} ms was recorded during iteration {it.get('index')}. "
                    "Anything over ~500 ms is a perceptible freeze. "
                    "Use a CPU profile to identify the call stack — synchronous JSON.parse / large list flat-map / "
                    "native bridge round-trips are the usual offenders."
                ),
                metric_name="blocking_interval_ms",
                metric_value=round(float(ms), 0),
                metric_threshold=BLOCKING_INTERVAL_MEDIUM_MS,
                file_path=f"results/{platform}.json",
            ))

    # 6) Per-step intervals where FPS dipped (best signal for "which screen?")
    for iv in intervals:
        fps_min = iv.get("fps_min")
        if not isinstance(fps_min, (int, float)):
            continue
        if fps_min >= FPS_MEDIUM:
            continue
        sev = severity_for_fps(float(fps_min))
        if sev is None:
            continue
        findings.append(make_finding(
            "device.step_fps_dipped",
            layer,
            category="runtime_jank",
            severity=sev,
            title=f"{pretty_platform}: FPS dipped to {fps_min:.0f} during step `{iv.get('step_label')}`",
            description=(
                f"During Maestro step `{iv.get('step_label')}`, minimum FPS was {fps_min:.0f}. "
                "This anchors the jank to a specific screen / interaction. Open the screen referenced by the step and "
                "audit it for inline arrows, missing memoization, or unbounded list renders. Cross-reference the "
                "static-layer findings for the same file."
            ),
            metric_name="step_fps_min",
            metric_value=round(float(fps_min), 1),
            metric_threshold=FPS_MEDIUM,
            file_path=f"results/{platform}.json",
            function=iv.get("step_label") or "",
        ))

    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Transform a device perf_result.json into Findings.")
    ap.add_argument("audit_id")
    ap.add_argument("perf_result", type=Path)
    args = ap.parse_args()

    if not args.perf_result.is_file():
        print(json.dumps([{
            "id": "tooling.device_results_missing",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "Device perf result file missing",
            "description": f"Expected {args.perf_result}; nothing to transform.",
            "evidence": {"file": str(args.perf_result)},
        }], indent=2))
        return 0

    try:
        result = json.loads(args.perf_result.read_text(encoding="utf-8"))
    except Exception as e:
        print(json.dumps([{
            "id": "tooling.device_results_invalid_json",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "Device perf result is not valid JSON",
            "description": f"{type(e).__name__}: {e}",
            "evidence": {"file": str(args.perf_result)},
        }], indent=2))
        return 0

    print(json.dumps(transform(result), indent=2))
    print(f"transform_device_metrics: audit={args.audit_id} platform={result.get('platform','?')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
