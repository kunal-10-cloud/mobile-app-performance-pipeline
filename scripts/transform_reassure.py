#!/usr/bin/env python3
"""
Stage 4c (part 3) — transform Reassure's JSON output into Findings.

Reads a `.reassure/current.perf` file (NDJSON or JSON depending on Reassure
version) and emits a Findings array on stdout matching
schemas/finding.schema.json.

Thresholds (verbatim from architecture.md §4c):
  - render count  > 5 per state change → medium
                  > 10                  → high
                  > 20                  → critical
  - render duration > 16 ms (1 frame @ 60 Hz) → medium
                    > 33 ms (2 frames)        → high
                    > 50 ms                   → critical

A test that failed to render at all becomes a `reassure.render_failure`
Finding with category=tooling_error so the rest of the audit continues.

Usage:
  python3 scripts/transform_reassure.py <audit_id> <reassure_output_path>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ── Thresholds ───────────────────────────────────────────────────────────────
COUNT_MEDIUM = 5
COUNT_HIGH = 10
COUNT_CRITICAL = 20

DURATION_MEDIUM_MS = 16.0
DURATION_HIGH_MS = 33.0
DURATION_CRITICAL_MS = 50.0


def severity_from_count(c: float) -> str | None:
    if c >= COUNT_CRITICAL:
        return "critical"
    if c >= COUNT_HIGH:
        return "high"
    if c >= COUNT_MEDIUM:
        return "medium"
    return None


def severity_from_duration(ms: float) -> str | None:
    if ms >= DURATION_CRITICAL_MS:
        return "critical"
    if ms >= DURATION_HIGH_MS:
        return "high"
    if ms >= DURATION_MEDIUM_MS:
        return "medium"
    return None


def load_reassure_output(p: Path) -> list[dict]:
    """Reassure may emit a single JSON object or NDJSON; tolerate both."""
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    # Try whole-file JSON first.
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        # Common shapes:
        # { "current": [ {entry}, ... ] }
        # { "currentEntries": [ {entry}, ... ] }
        for key in ("current", "currentEntries", "entries", "results"):
            if isinstance(obj.get(key), list):
                return obj[key]
        # Or a single entry object
        if "name" in obj:
            return [obj]
        return []
    if isinstance(obj, list):
        return obj
    # NDJSON fallback
    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _safe_float(d: dict, *keys: str) -> float | None:
    cur: object = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        if k not in cur:
            return None
        cur = cur[k]
    if isinstance(cur, (int, float)):
        return float(cur)
    return None


def transform_entry(entry: dict) -> list[dict]:
    """Turn one Reassure entry into 0..N Findings."""
    out: list[dict] = []
    name = entry.get("name") or "<unnamed test>"
    component_name = name.split(" ")[0]  # "FeedScreen render perf" → "FeedScreen"

    # Detect a failed run. Reassure marks failures via "error" / "errors" /
    # missing render block. We surface those as tooling-error findings rather
    # than silently dropping them — the operator should know which screens
    # the audit could not measure.
    if entry.get("error") or entry.get("errors"):
        msg = entry.get("error") or json.dumps(entry.get("errors"))[:300]
        out.append({
            "id": "reassure.render_failure",
            "layer": "reassure",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": f"`{component_name}` could not be rendered in the Reassure environment",
            "description": (
                f"`{name}` threw during render: {msg}. "
                "Render-perf measurement skipped for this component. "
                "Most often this is a missing mock for a native module the component imports. "
                "Add a hand-written perf test alongside the generator output to cover it."
            ),
            "evidence": {
                "file": f"__reassure_tests__/{component_name}",
                "function": component_name,
                "metric_name": "render_error",
                "code_snippet": str(msg)[:300],
            },
        })
        return out

    count_mean = _safe_float(entry, "render", "count", "mean")
    if count_mean is None:
        count_mean = _safe_float(entry, "renderCount", "mean")
    duration_mean = _safe_float(entry, "render", "duration", "mean")
    if duration_mean is None:
        duration_mean = _safe_float(entry, "renderDuration", "mean")

    if count_mean is not None:
        sev = severity_from_count(count_mean)
        if sev is not None:
            out.append({
                "id": "reassure.excessive_render_count",
                "layer": "reassure",
                "category": "runtime_jank",
                "severity": sev,
                "confidence": "high",
                "title": f"`{component_name}` renders {count_mean:.1f}× per state change",
                "description": (
                    f"Reassure measured a mean of {count_mean:.1f} renders per state change for `{component_name}`. "
                    "Above ~5 renders, every interaction does work that React's reconciler should not need to do. "
                    "Common causes: inline-object or inline-arrow props, missing `React.memo` on heavy children, "
                    "context providers whose value identity changes every render."
                ),
                "evidence": {
                    "file": f"__reassure_tests__/{component_name}",
                    "function": component_name,
                    "metric_name": "render_count_mean",
                    "metric_value": count_mean,
                    "metric_threshold": COUNT_MEDIUM,
                },
            })

    if duration_mean is not None:
        sev = severity_from_duration(duration_mean)
        if sev is not None:
            out.append({
                "id": "reassure.excessive_render_duration",
                "layer": "reassure",
                "category": "runtime_jank",
                "severity": sev,
                "confidence": "high",
                "title": f"`{component_name}` renders take {duration_mean:.1f} ms on average",
                "description": (
                    f"Mean render duration for `{component_name}` is {duration_mean:.1f} ms — "
                    f"that exceeds the 16 ms 60-Hz frame budget. A single state update on this screen will drop at least one frame, "
                    "producing visible jank. Split the subtree, memoize expensive children, or move heavy work off-render "
                    "(`useMemo`, lazy hydration, server-pre-computed values)."
                ),
                "evidence": {
                    "file": f"__reassure_tests__/{component_name}",
                    "function": component_name,
                    "metric_name": "render_duration_ms_mean",
                    "metric_value": duration_mean,
                    "metric_threshold": DURATION_MEDIUM_MS,
                },
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Transform Reassure JSON into Findings.")
    ap.add_argument("audit_id")
    ap.add_argument("reassure_output", type=Path)
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    if not args.reassure_output.is_file():
        print(json.dumps([{
            "id": "tooling.reassure_no_output",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "Reassure output file missing",
            "description": f"Expected {args.reassure_output}; nothing to transform.",
            "evidence": {"file": str(args.reassure_output)},
        }], indent=2))
        return 0

    entries = load_reassure_output(args.reassure_output)
    findings: list[dict] = []
    for e in entries:
        findings.extend(transform_entry(e))

    print(json.dumps(findings, indent=2))
    print(f"transform_reassure: {len(entries)} entries → {len(findings)} findings", file=sys.stderr)
    print(f"  audit_dir: {audit_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
