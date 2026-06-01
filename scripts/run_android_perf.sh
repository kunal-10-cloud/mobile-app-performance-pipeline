#!/usr/bin/env bash
# Stage 4d.7 — Android device measurement.
#
# Runs Flashlight (https://docs.flashlight.dev) over the validated Maestro
# flow, captures FPS, CPU, memory, blocking intervals per iteration, and
# writes results to ${AUDIT_DIR}/results/android.json.
#
# Fail-soft: each failure mode emits a tooling.* finding and exits 0 so the
# iOS stage and downstream report still complete.
#
# Assumes the emulator is already booted (validate_flow.sh boots it on first
# run and leaves it running). If not booted, attempts to discover one;
# otherwise emits a finding and exits.
#
# Usage:
#   bash scripts/run_android_perf.sh <audit_id>
#   bash scripts/run_android_perf.sh <audit_id> --duration-ms 60000 --iterations 3

set -uo pipefail

AUDIT_ID="${1:-}"
shift || true
DURATION_MS=""
ITERATIONS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration-ms) DURATION_MS="$2"; shift 2 ;;
    --iterations)  ITERATIONS="$2";  shift 2 ;;
    *) echo "WARN: unknown arg $1" >&2; shift ;;
  esac
done

if [[ -z "$AUDIT_ID" ]]; then
  echo "ERROR: audit_id required" >&2
  exit 2
fi

RUNS_DIR="${MOBILE_AUDIT_RUNS_DIR:-.audit-runs}"
AUDIT_DIR="${RUNS_DIR}/${AUDIT_ID}"
RESULTS_DIR="${AUDIT_DIR}/results"
FLOWS_DIR="${AUDIT_DIR}/flows"
FINDINGS_DIR="${AUDIT_DIR}/findings"
ARTIFACTS_DIR="${AUDIT_DIR}/artifacts"
APK="${ARTIFACTS_DIR}/${AUDIT_ID}.apk"
FLOW_YAML="${FLOWS_DIR}/main.yaml"
RAW_RESULTS="${RESULTS_DIR}/android_flashlight.json"
NORMALISED_RESULTS="${RESULTS_DIR}/android.json"

mkdir -p "${RESULTS_DIR}" "${FINDINGS_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
EMU_CONFIG="${REPO_ROOT}/configs/android-emulator.json"

# Defaults from configs/android-emulator.json
if [[ -z "${DURATION_MS}" ]]; then
  DURATION_MS="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('measurement_defaults',{}).get('duration_ms',60000))" "${EMU_CONFIG}")"
fi
if [[ -z "${ITERATIONS}" ]]; then
  ITERATIONS="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('measurement_defaults',{}).get('iteration_count',3))" "${EMU_CONFIG}")"
fi

# Findings TSV → JSON helper -------------------------------------------------

FINDINGS_TSV="${AUDIT_DIR}/.android_perf_findings.tsv"
: > "${FINDINGS_TSV}"

push_finding() {
  printf '%s\t%s\t%s\t%s\n' \
    "$(printf '%s' "$1" | tr '\t\n' '  ')" \
    "$(printf '%s' "$2" | tr '\t\n' '  ')" \
    "$(printf '%s' "$3" | tr '\t\n' '  ')" \
    "$(printf '%s' "$4" | tr '\t\n' '  ')" \
    >> "${FINDINGS_TSV}"
}

flush_tooling_findings() {
  python3 - "${FINDINGS_TSV}" "${FINDINGS_DIR}/android_run.json" <<'PY'
import json, sys
arr = []
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line: continue
        parts = line.split("\t")
        if len(parts) < 4: continue
        rid, sev, title, desc = parts[0], parts[1], parts[2], "\t".join(parts[3:])
        arr.append({
            "id": rid, "layer": "tooling", "category": "tooling_error",
            "severity": sev, "confidence": "high",
            "title": title, "description": desc,
            "evidence": {"file": "results/android.json"},
        })
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(arr, fh, indent=2)
PY
}

# Preconditions --------------------------------------------------------------

if [[ ! -f "${APK}" ]]; then
  push_finding "tooling.android_apk_missing" "low" \
    "Android APK missing — measurement skipped" \
    "Expected ${APK}; the build step must run before measurement."
  flush_tooling_findings
  exit 0
fi
if [[ ! -f "${FLOW_YAML}" ]]; then
  push_finding "tooling.android_flow_missing" "low" \
    "Main flow YAML missing — Android measurement skipped" \
    "Expected ${FLOW_YAML}; flow generation must run first."
  flush_tooling_findings
  exit 0
fi
if ! command -v flashlight > /dev/null 2>&1; then
  push_finding "tooling.flashlight_unavailable" "low" \
    "Flashlight CLI not available — Android measurement skipped" \
    "Install with 'npm i -g @perf-profiler/flashlight'. Subsequent stages will run with no Android device data."
  flush_tooling_findings
  exit 0
fi
if ! command -v maestro > /dev/null 2>&1; then
  push_finding "tooling.maestro_unavailable" "low" \
    "Maestro CLI not on PATH" \
    "Flashlight needs maestro to drive the flow. Install Maestro from maestro.mobile.dev."
  flush_tooling_findings
  exit 0
fi
if ! command -v adb > /dev/null 2>&1; then
  push_finding "tooling.adb_unavailable" "low" \
    "adb not on PATH" \
    "Android measurement requires the Android SDK platform-tools."
  flush_tooling_findings
  exit 0
fi

# Resolve bundle id from the workspace facts ---------------------------------
BUNDLE_ID="$(python3 - "${AUDIT_DIR}/facts/audit_facts.json" <<'PY'
import json, sys
try:
    f = json.load(open(sys.argv[1]))
except Exception:
    print(""); sys.exit(0)
sig = f.get("project_signature") or {}
print(sig.get("android_package") or "")
PY
)"
if [[ -z "${BUNDLE_ID}" ]]; then
  # Fallback: derive from APK
  if command -v aapt > /dev/null 2>&1; then
    BUNDLE_ID="$(aapt dump badging "${APK}" 2>/dev/null | awk -F"'" '/^package: name=/ {print $2; exit}')"
  fi
fi
if [[ -z "${BUNDLE_ID}" ]]; then
  push_finding "tooling.android_bundleid_unknown" "low" \
    "Could not determine Android bundle ID" \
    "Neither facts.project_signature.android_package nor aapt could resolve the package name; Flashlight needs --bundleId."
  flush_tooling_findings
  exit 0
fi

# Run Flashlight -------------------------------------------------------------

echo "[android] flashlight test --bundleId ${BUNDLE_ID} --duration ${DURATION_MS} --iterationCount ${ITERATIONS}" >&2
flashlight test \
  --bundleId "${BUNDLE_ID}" \
  --testCommand "maestro test ${FLOW_YAML}" \
  --duration "${DURATION_MS}" \
  --iterationCount "${ITERATIONS}" \
  --resultsFilePath "${RAW_RESULTS}" \
  > "${AUDIT_DIR}/flashlight_stdout.log" 2>&1
FL_RC=$?

if [[ ${FL_RC} -ne 0 || ! -s "${RAW_RESULTS}" ]]; then
  TAIL="$(tail -n 60 "${AUDIT_DIR}/flashlight_stdout.log" 2>/dev/null | tr '\n' ' ' | head -c 1500)"
  push_finding "tooling.flashlight_run_failed" "low" \
    "Flashlight returned non-zero or produced no output" \
    "Exit ${FL_RC}. Last log: ${TAIL}"
  flush_tooling_findings
  exit 0
fi

# Normalise raw flashlight JSON into our perf_result schema ------------------
python3 - "${RAW_RESULTS}" "${NORMALISED_RESULTS}" "${BUNDLE_ID}" "${ITERATIONS}" <<'PY'
import json, sys, datetime as dt

def _f(d, keys):
    for k in keys:
        if isinstance(d, dict) and k in d and isinstance(d[k], (int, float)):
            return float(d[k])
    return None

raw_path, out_path, bundle_id, iters = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
try:
    raw = json.load(open(raw_path, encoding="utf-8"))
except Exception as e:
    json.dump({
        "platform": "android", "device_profile": "unknown",
        "bundle_id": bundle_id,
        "captured_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score": None, "startup_time_ms": None, "iterations": [],
        "tool_warnings": [f"could not parse flashlight output: {e}"],
        "raw_artifact_path": raw_path,
    }, open(out_path, "w"), indent=2)
    sys.exit(0)

iterations_raw = raw.get("iterations") or raw.get("runs") or []
norm_iters = []
for idx, it in enumerate(iterations_raw):
    fps = it.get("fps") or it.get("framesPerSecond") or {}
    cpu = it.get("cpu") or it.get("cpu_per_thread") or {}
    mem = it.get("memory") or it.get("memory_mb") or it.get("memory_per_process") or {}
    blocking = it.get("blockingIntervalsMs") or it.get("blocking_intervals_ms") or []
    norm_iters.append({
        "index": idx,
        "fps": {
            "average": _f(fps, ("average", "avg", "mean")),
            "min":     _f(fps, ("min", "minimum")),
            "max":     _f(fps, ("max", "maximum")),
        },
        "cpu_per_thread": cpu if isinstance(cpu, dict) else {},
        "memory_mb": {
            "average": _f(mem, ("average", "avg", "mean")),
            "peak":    _f(mem, ("peak", "max", "maximum")),
            "growth":  _f(mem, ("growth",)) or 0.0,
        },
        "blocking_intervals_ms": [b for b in blocking if isinstance(b, (int, float))],
    })

intervals_raw = raw.get("intervals") or raw.get("interactions") or []
intervals = []
for iv in intervals_raw:
    intervals.append({
        "step_label": iv.get("label") or iv.get("name") or "(unnamed)",
        "start_ms":   int(iv.get("start") or iv.get("start_ms") or 0),
        "end_ms":     int(iv.get("end") or iv.get("end_ms") or 0),
        "fps_avg":    _f(iv.get("fps") or {}, ("average", "avg", "mean")),
        "fps_min":    _f(iv.get("fps") or {}, ("min",)),
        "cpu_avg":    _f(iv.get("cpu") or {}, ("average", "avg", "mean")),
        "memory_peak_mb": _f(iv.get("memory") or {}, ("peak",)),
    })

out = {
    "platform": "android",
    "device_profile": raw.get("deviceProfile") or raw.get("device_profile") or "unknown",
    "bundle_id": bundle_id,
    "captured_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "score": raw.get("score"),
    "startup_time_ms": raw.get("startupTimeMs") or raw.get("startup_time_ms"),
    "iterations": norm_iters,
    "intervals": intervals,
    "raw_artifact_path": raw_path,
    "tool_warnings": raw.get("warnings", []) or [],
}
json.dump(out, open(out_path, "w"), indent=2)
PY

# Transform normalised perf_result.json → Findings ---------------------------
python3 "${REPO_ROOT}/scripts/transform_device_metrics.py" "${AUDIT_ID}" "${NORMALISED_RESULTS}" \
  > "${FINDINGS_DIR}/android_perf.json" || {
  push_finding "tooling.android_metrics_transform_failed" "low" \
    "Could not transform Flashlight output into Findings" \
    "transform_device_metrics.py failed. Normalised JSON preserved at ${NORMALISED_RESULTS}."
}

flush_tooling_findings
echo "[android] complete → ${NORMALISED_RESULTS}, findings in ${FINDINGS_DIR}/" >&2
exit 0
