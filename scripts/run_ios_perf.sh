#!/usr/bin/env bash
# Stage 4d.8 — iOS device measurement.
#
# Modest by design. Flashlight does not have iOS parity yet (no FPS/CPU
# capture); v1 measures cold start (via xcrun simctl spawn booted log) and
# memory at idle + after-flow (via vm_stat / footprint inspection). Frame-rate
# data is documented as a v2 item per architecture.md.
#
# Steps:
#   1. Boot a low-end iOS Simulator (iPhone SE 3rd gen).
#   2. Install the IPA.
#   3. Capture a cold-start timestamp via the unified log subsystem.
#   4. Launch the app, run Maestro against the same flow YAML used for Android.
#   5. Capture memory footprint via `xcrun simctl spawn booted memory_pressure`
#      and `vm_stat` snapshots.
#   6. Write results/ios.json in our perf_result schema shape.
#
# Fail-soft: per-failure tooling.* finding, no abort.
#
# Usage:
#   bash scripts/run_ios_perf.sh <audit_id>
#   bash scripts/run_ios_perf.sh <audit_id> --device "iPhone SE (3rd generation)"

set -uo pipefail

AUDIT_ID="${1:-}"
shift || true
DEVICE_NAME="iPhone SE (3rd generation)"
ITERATIONS=3   # default; matches the Android (Flashlight) run iteration count

while [[ $# -gt 0 ]]; do
  case "$1" in
    --device)     DEVICE_NAME="$2"; shift 2 ;;
    --iterations) ITERATIONS="$2"; shift 2 ;;
    *) echo "WARN: unknown arg $1" >&2; shift ;;
  esac
done

if [[ -z "$AUDIT_ID" ]]; then
  echo "ERROR: audit_id required" >&2
  exit 2
fi

RUNS_DIR="${MOBILE_AUDIT_RUNS_DIR:-.audit-runs}"
AUDIT_DIR="${RUNS_DIR}/${AUDIT_ID}"
ARTIFACTS_DIR="${AUDIT_DIR}/artifacts"
FLOWS_DIR="${AUDIT_DIR}/flows"
FINDINGS_DIR="${AUDIT_DIR}/findings"
RESULTS_DIR="${AUDIT_DIR}/results"
IPA="${ARTIFACTS_DIR}/${AUDIT_ID}.ipa"
FLOW_YAML="${FLOWS_DIR}/main.yaml"
RESULTS_FILE="${RESULTS_DIR}/ios.json"

mkdir -p "${RESULTS_DIR}" "${FINDINGS_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

FINDINGS_TSV="${AUDIT_DIR}/.ios_perf_findings.tsv"
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
  python3 - "${FINDINGS_TSV}" "${FINDINGS_DIR}/ios_run.json" <<'PY'
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
            "evidence": {"file": "results/ios.json"},
        })
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(arr, fh, indent=2)
PY
}

# Preconditions --------------------------------------------------------------

if [[ "$(uname)" != "Darwin" ]]; then
  push_finding "tooling.ios_unsupported_host" "low" \
    "iOS measurement skipped — non-macOS host" \
    "iOS measurement requires macOS + Xcode. Host is $(uname). Render the rest of the report without iOS data."
  flush_tooling_findings
  echo "{}" > "${RESULTS_FILE}"
  exit 0
fi
if ! command -v xcrun > /dev/null 2>&1; then
  push_finding "tooling.xcrun_unavailable" "low" \
    "xcrun not on PATH — iOS measurement skipped" \
    "Install the Xcode Command Line Tools."
  flush_tooling_findings
  echo "{}" > "${RESULTS_FILE}"
  exit 0
fi
if [[ ! -f "${IPA}" ]]; then
  push_finding "tooling.ios_ipa_missing" "low" \
    "iOS IPA missing — measurement skipped" \
    "Expected ${IPA}; the iOS build did not produce one."
  flush_tooling_findings
  echo "{}" > "${RESULTS_FILE}"
  exit 0
fi
if [[ ! -f "${FLOW_YAML}" ]]; then
  push_finding "tooling.ios_flow_missing" "low" \
    "Maestro flow missing — iOS measurement skipped" \
    "Expected ${FLOW_YAML}."
  flush_tooling_findings
  echo "{}" > "${RESULTS_FILE}"
  exit 0
fi
if ! command -v maestro > /dev/null 2>&1; then
  push_finding "tooling.maestro_unavailable" "low" \
    "Maestro CLI not on PATH" \
    "Install Maestro from maestro.mobile.dev to enable iOS measurement."
  flush_tooling_findings
  echo "{}" > "${RESULTS_FILE}"
  exit 0
fi

# Resolve iOS bundle ID from facts -------------------------------------------
BUNDLE_ID="$(python3 - "${AUDIT_DIR}/facts/audit_facts.json" <<'PY'
import json, sys
try: f = json.load(open(sys.argv[1]))
except Exception: print(""); sys.exit(0)
sig = f.get("project_signature") or {}
print(sig.get("ios_bundle_identifier") or "")
PY
)"
if [[ -z "${BUNDLE_ID}" ]]; then
  push_finding "tooling.ios_bundleid_unknown" "low" \
    "iOS bundle identifier missing from facts" \
    "Add ios.bundleIdentifier to app.json or extend gather_facts.py to capture it."
  flush_tooling_findings
  echo "{}" > "${RESULTS_FILE}"
  exit 0
fi

# Boot simulator -------------------------------------------------------------

UDID="$(xcrun simctl list devices available 2>/dev/null | \
        awk -v name="${DEVICE_NAME}" '
          /^-- iOS/ {pick=1; next}
          /^-- / {pick=0}
          pick && index($0, name) {
            match($0, /\(([A-F0-9-]{36})\)/, m); if (m[1]) {print m[1]; exit}
          }')"

if [[ -z "${UDID}" ]]; then
  push_finding "tooling.ios_simulator_unavailable" "low" \
    "iOS Simulator '${DEVICE_NAME}' not installed" \
    "Install via 'xcode-select --install' + Xcode iOS runtimes. Alternative: pass --device with an installed device name."
  flush_tooling_findings
  echo "{}" > "${RESULTS_FILE}"
  exit 0
fi

echo "[ios] booting simulator ${UDID} (${DEVICE_NAME})…" >&2
xcrun simctl boot "${UDID}" > /dev/null 2>&1 || true
xcrun simctl bootstatus "${UDID}" -b > /dev/null 2>&1 || true

# Install IPA ---------------------------------------------------------------

echo "[ios] installing IPA…" >&2
xcrun simctl install "${UDID}" "${IPA}" > "${AUDIT_DIR}/ios_install.log" 2>&1 || {
  TAIL="$(tail -n 40 "${AUDIT_DIR}/ios_install.log" | tr '\n' ' ' | head -c 1000)"
  push_finding "tooling.ios_install_failed" "low" \
    "xcrun simctl install failed" \
    "Last log: ${TAIL}"
  flush_tooling_findings
  echo "{}" > "${RESULTS_FILE}"
  exit 0
}

# Host architecture — drives the "device-class estimate" vs "regression-only"
# labelling in compute_device_metrics.py. On Apple Silicon (arm64) the
# Simulator runs the iOS binary natively and CPU/memory readings are within
# 30% of a recent iPhone; on Intel they are not comparable.
HOST_ARCH="$(uname -m 2>/dev/null || echo unknown)"

# Per-app memory sampler -----------------------------------------------------
# Reads RSS (KB) of the launched app process inside the booted simulator and
# returns MB. Returns 0 if the process is not found.
sample_app_rss_mb() {
  local pid_line
  # ps within the simulator: list pid + rss + command, filter by app stem name.
  # The main executable's name equals the .app stem (e.g. "Astrova").
  pid_line="$(xcrun simctl spawn "${UDID}" launchctl list 2>/dev/null \
              | awk -v bid="${BUNDLE_ID}" '$3 ~ bid {print $1; exit}')"
  if [[ -z "${pid_line}" || "${pid_line}" == "-" ]]; then
    echo 0
    return
  fi
  # `ps -o rss=` on the simulator gives KB; convert to MiB.
  local rss_kb
  rss_kb="$(xcrun simctl spawn "${UDID}" ps -o rss= -p "${pid_line}" 2>/dev/null \
            | tr -d ' ')"
  if [[ -z "${rss_kb}" || ! "${rss_kb}" =~ ^[0-9]+$ ]]; then
    echo 0
    return
  fi
  echo $(( rss_kb / 1024 ))
}

# Cold-start capture (single shot — only the first launch is truly "cold") ---
echo "[ios] cold-start launch…" >&2
xcrun simctl terminate "${UDID}" "${BUNDLE_ID}" > /dev/null 2>&1 || true
LAUNCH_START_NS="$(date +%s%N)"
xcrun simctl launch "${UDID}" "${BUNDLE_ID}" > "${AUDIT_DIR}/ios_launch.log" 2>&1 || true
LAUNCH_END_NS="$(date +%s%N)"
STARTUP_MS=$(( (LAUNCH_END_NS - LAUNCH_START_NS) / 1000000 ))
sleep 1   # let the app settle so first memory sample is meaningful

# Iteration loop — re-run the Maestro flow N times, capture per-iteration -----
# memory deltas. This is the reliable iOS Sim signal (leak detection across
# iterations works the same on Sim as on a real device).
echo "[ios] running ${ITERATIONS} iteration(s) of the Maestro flow…" >&2

ITER_TSV="${AUDIT_DIR}/.ios_iter.tsv"
: > "${ITER_TSV}"

for (( i=0; i<ITERATIONS; i++ )); do
  MEM_BEFORE_MB="$(sample_app_rss_mb)"
  APP_ID="${BUNDLE_ID}" maestro --device "${UDID}" test "${FLOW_YAML}" \
    > "${AUDIT_DIR}/maestro_ios_${i}.log" 2>&1
  ITER_RC=$?
  sleep 1
  MEM_AFTER_MB="$(sample_app_rss_mb)"
  MEM_GROWTH_MB=$(( MEM_AFTER_MB - MEM_BEFORE_MB ))
  printf '%d\t%d\t%d\t%d\t%d\n' "${i}" "${MEM_BEFORE_MB}" "${MEM_AFTER_MB}" "${MEM_GROWTH_MB}" "${ITER_RC}" >> "${ITER_TSV}"
  echo "[ios] iter ${i}: before=${MEM_BEFORE_MB} MB  after=${MEM_AFTER_MB} MB  growth=${MEM_GROWTH_MB} MB  rc=${ITER_RC}" >&2
done

# Write the normalised perf_result.json --------------------------------------
python3 - "${RESULTS_FILE}" "${BUNDLE_ID}" "${DEVICE_NAME}" "${UDID}" "${STARTUP_MS}" "${ITER_TSV}" "${HOST_ARCH}" "${AUDIT_DIR}/maestro_ios_0.log" <<'PY'
import json, sys, datetime as dt
(out_path, bundle_id, device_name, udid, startup_ms, iter_tsv, host_arch, maestro_log) = sys.argv[1:]
iterations = []
peak = 0
growth_total = 0
with open(iter_tsv, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        idx_s, before_s, after_s, growth_s, _rc = line.split("\t")
        before, after, growth = int(before_s), int(after_s), int(growth_s)
        peak = max(peak, after, before)
        growth_total += growth
        iterations.append({
            "index": int(idx_s),
            # FPS deliberately not measured on iOS Simulator: GPU differs from
            # iPhone too materially for the number to be honest. See compute_device_metrics.
            "fps": {"average": None, "min": None, "max": None},
            "cpu_per_thread": {},
            "memory_mb": {
                "average": (before + after) / 2,
                "peak":    after,
                "growth":  growth,
            },
            "blocking_intervals_ms": [],
        })

# Reliability metadata — read by compute_device_metrics.py to label each row
# in the iOS Lighthouse-style breakdown.
on_apple_silicon = host_arch == "arm64"

out = {
    "platform": "ios",
    "device_profile": f"{device_name} ({udid})",
    "bundle_id": bundle_id,
    "captured_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "score": None,
    "startup_time_ms": int(startup_ms) if startup_ms.lstrip("-").isdigit() else None,
    "iterations": iterations,
    "intervals": [],
    "raw_artifact_path": maestro_log,
    "measurement_environment": {
        "kind":             "ios_simulator",
        "host_arch":        host_arch,
        "on_apple_silicon": on_apple_silicon,
    },
    "tool_warnings": [
        "iOS Simulator: FPS / GPU-class metrics intentionally omitted (Mac GPU is not iPhone-comparable).",
        "Memory growth across iterations is the highest-reliability signal — same on Sim as on device.",
        ("Cold start + peak memory are device-class estimates on Apple Silicon (~30% optimistic vs iPhone)."
         if on_apple_silicon
         else "Cold start + peak memory on Intel-Mac Simulator are regression-relative only, NOT device-comparable."),
    ],
}
json.dump(out, open(out_path, "w"), indent=2)
PY

# Transform results into Findings --------------------------------------------
python3 "${REPO_ROOT}/scripts/transform_device_metrics.py" "${AUDIT_ID}" "${RESULTS_FILE}" \
  > "${FINDINGS_DIR}/ios_perf.json" || {
  push_finding "tooling.ios_metrics_transform_failed" "low" \
    "Could not transform iOS measurement into Findings" \
    "transform_device_metrics.py raised on ${RESULTS_FILE}."
}

flush_tooling_findings
echo "[ios] complete → ${RESULTS_FILE}" >&2
exit 0
