#!/usr/bin/env bash
# Stage 4d (cloud variant) — measure runtime perf via Flashlight Cloud.
#
# Runs on the HOST that has the Flashlight CLI + the APK (NOT the pod — the pod
# can't run an emulator). Flashlight Cloud runs the APK on real devices in
# BAM/Theodo's cloud and returns a performance report. Free (open beta) as of
# 2026; queue times possible.
#
# Auth: FLASHLIGHT_API_KEY env var (create at https://app.flashlight.dev/api-key).
#   The skill never prompts; if it's missing we emit a tooling finding and skip.
#
# CLI contract (per docs.flashlight.dev/cloud/cli):
#   flashlight cloud --app <apk> --test <flow.yml> --duration <ms> [--beforeAll <login.yml>]
#   - Supports Maestro flows only (we generate Maestro YAML — perfect).
#   - Prints a cloud report URL; metric retrieval is best-effort here.
#
# DATA-RESIDENCY NOTE: this uploads the customer APK to a third-party cloud.
# Only run when that's been cleared. The orchestrator gates on an explicit
# --i-have-upload-consent flag (or DEVICE_CLOUD_CONSENT=1) so we never ship a
# customer binary externally by accident.
#
# Usage:
#   bash scripts/run_flashlight_cloud.sh <audit_id> --apk <path> [--duration-ms 60000] [--before-all <yml>] --consent
#   DEVICE_CLOUD_CONSENT=1 bash scripts/run_flashlight_cloud.sh <audit_id> --apk <path>

set -uo pipefail

AID="${1:-}"
shift || true
APK=""
DURATION_MS="60000"
BEFORE_ALL=""
CONSENT="${DEVICE_CLOUD_CONSENT:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apk)         APK="$2"; shift 2 ;;
    --duration-ms) DURATION_MS="$2"; shift 2 ;;
    --before-all)  BEFORE_ALL="$2"; shift 2 ;;
    --consent|--i-have-upload-consent) CONSENT=1; shift ;;
    *) echo "WARN: unknown arg $1" >&2; shift ;;
  esac
done

if [[ -z "$AID" || -z "$APK" ]]; then
  echo "ERROR: usage: run_flashlight_cloud.sh <audit_id> --apk <path> [--duration-ms N] [--before-all yml] --consent" >&2
  exit 2
fi

RUNS_DIR="${MOBILE_AUDIT_RUNS_DIR:-.audit-runs}"
AUDIT_DIR="${RUNS_DIR}/${AID}"
FLOWS_DIR="${AUDIT_DIR}/flows"
RESULTS_DIR="${AUDIT_DIR}/results"
FINDINGS_DIR="${AUDIT_DIR}/findings"
FLOW_YAML="${FLOWS_DIR}/main.yaml"
RESULTS_FILE="${RESULTS_DIR}/android.json"
RAW_LOG="${AUDIT_DIR}/flashlight_cloud.log"
mkdir -p "${RESULTS_DIR}" "${FINDINGS_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY="${PYTHON:-python3}"

emit_tooling() {
  ${PY} - "${FINDINGS_DIR}/flashlight_cloud.json" "$1" "$2" "$3" <<'PY'
import json, sys
out, rid, title, desc = sys.argv[1:]
json.dump([{
  "id": rid, "layer": "tooling", "category": "tooling_error",
  "severity": "low", "confidence": "high",
  "title": title, "description": desc,
  "evidence": {"file": "flashlight-cloud"},
}], open(out, "w"), indent=2)
PY
}

# Gates ----------------------------------------------------------------------
if [[ "${CONSENT}" != "1" ]]; then
  emit_tooling "tooling.device_cloud_consent_missing" \
    "Flashlight Cloud skipped — upload consent not given" \
    "Running this uploads the customer APK to a third-party cloud. Re-run with --consent (or DEVICE_CLOUD_CONSENT=1) only after that's been cleared."
  echo "[flashlight-cloud] consent not given; skipping." >&2
  exit 0
fi
if [[ -z "${FLASHLIGHT_API_KEY:-}" ]]; then
  emit_tooling "tooling.flashlight_api_key_missing" \
    "Flashlight Cloud skipped — FLASHLIGHT_API_KEY not set" \
    "Set FLASHLIGHT_API_KEY (create at https://app.flashlight.dev/api-key). The skill never prompts for it."
  echo "[flashlight-cloud] FLASHLIGHT_API_KEY missing; skipping." >&2
  exit 0
fi
if ! command -v flashlight > /dev/null 2>&1; then
  emit_tooling "tooling.flashlight_unavailable" \
    "Flashlight CLI not installed" \
    "Install with 'npm i -g @perf-profiler/flashlight'. Cloud run skipped."
  exit 0
fi
if [[ ! -f "${APK}" ]]; then
  emit_tooling "tooling.apk_missing" "APK not found — cloud run skipped" \
    "Expected an APK at ${APK}."
  exit 0
fi
if [[ ! -f "${FLOW_YAML}" ]]; then
  emit_tooling "tooling.flow_missing" "Maestro flow missing — cloud run skipped" \
    "Expected ${FLOW_YAML}; run the flow-generation stages first."
  exit 0
fi

# Run ------------------------------------------------------------------------
CMD=(flashlight cloud --app "${APK}" --test "${FLOW_YAML}" --duration "${DURATION_MS}")
[[ -n "${BEFORE_ALL}" && -f "${BEFORE_ALL}" ]] && CMD+=(--beforeAll "${BEFORE_ALL}")

echo "[flashlight-cloud] ${CMD[*]}" >&2
"${CMD[@]}" > "${RAW_LOG}" 2>&1
RC=$?
echo "[flashlight-cloud] exit ${RC}" >&2

# Extract whatever the CLI produced: a report URL (always) + a results JSON
# (when the CLI writes one locally). We parse defensively and hand any
# perf_result-shaped JSON to transform_device_metrics; otherwise we surface
# the report URL so the operator can read the cloud report.
${PY} - "${RAW_LOG}" "${RESULTS_FILE}" "${FINDINGS_DIR}/flashlight_cloud.json" "${RC}" <<'PY'
import json, re, sys, datetime as dt
raw_path, results_file, findings_file, rc = sys.argv[1:]
text = open(raw_path, encoding="utf-8", errors="replace").read() if __import__("os").path.exists(raw_path) else ""

# 1) Try to find a report URL.
m = re.search(r"https?://(?:app\.)?flashlight\.dev/\S+", text)
report_url = m.group(0).rstrip(".,)") if m else ""

# 2) Try to find an inline JSON results blob (some CLI versions print one).
result = None
for blob in re.findall(r"\{.*?\}", text, re.DOTALL):
    try:
        cand = json.loads(blob)
    except Exception:
        continue
    if isinstance(cand, dict) and ("iterations" in cand or "averageFPS" in cand or "measures" in cand):
        result = cand
        break

findings = []
if result is not None:
    # Normalise into our perf_result shape (best-effort field mapping).
    iters = result.get("iterations") or result.get("measures") or []
    norm = {
        "platform": "android",
        "device_profile": result.get("deviceModel") or "flashlight-cloud",
        "bundle_id": result.get("bundleId") or "",
        "captured_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score": result.get("score"),
        "startup_time_ms": result.get("startupTimeMs") or result.get("timeToFullDisplay"),
        "iterations": [],
        "tool_warnings": [f"flashlight cloud report: {report_url}"] if report_url else [],
        "raw_artifact_path": raw_path,
    }
    json.dump(norm, open(results_file, "w"), indent=2)
    # transform separately (the shell calls transform_device_metrics next)
    json.dump([], open(findings_file, "w"))  # placeholder; transform fills real findings
    print("PARSED_RESULT", report_url)
else:
    # No machine-readable result; surface the report URL as an informational finding.
    desc = (f"Flashlight Cloud run completed (exit {rc}). "
            + (f"Full report: {report_url}" if report_url else
               "No report URL found in output — check flashlight_cloud.log. The run may be queued."))
    json.dump([{
        "id": "device.cloud_report_available" if report_url else "tooling.flashlight_cloud_no_result",
        "layer": "device_android" if report_url else "tooling",
        "category": "runtime_jank" if report_url else "tooling_error",
        "severity": "info", "confidence": "high",
        "title": "Flashlight Cloud performance report" if report_url else "Flashlight Cloud produced no parseable result",
        "description": desc,
        "evidence": {"file": "flashlight-cloud", "code_snippet": report_url[:300]},
    }], open(findings_file, "w"), indent=2)
    print("NO_RESULT", report_url)
PY

# If we got a normalised perf_result, transform it into real metric findings.
if [[ -s "${RESULTS_FILE}" ]]; then
  ${PY} "${REPO_ROOT}/scripts/transform_device_metrics.py" "${AID}" "${RESULTS_FILE}" \
    > "${FINDINGS_DIR}/android_perf.json" 2>/dev/null || true
fi

echo "[flashlight-cloud] done — raw log ${RAW_LOG}" >&2
exit 0
