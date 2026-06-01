#!/usr/bin/env bash
# Stage 4d orchestrator — chains the device sub-stages with the right
# pre-checks and degradation rules.
#
# Sequence:
#   4d.1  build_app.sh                  (Android + iOS, parallel where possible)
#   4d.2  extract_screen_map.py
#   4d.3  generate_draft_flow.py
#   4d.4  refine_flow_with_llm.py --prepare    (then SKILL.md drives the LLM)
#         refine_flow_with_llm.py --render
#   4d.5  validate_flow.sh
#   4d.6  repair_flow_with_llm.py (only if validation reported failures)
#   4d.7  run_android_perf.sh           (if APK present + emulator available)
#   4d.8  run_ios_perf.sh               (macOS + IPA + simulator)
#
# Degradation rules:
#   - --quick: skip device entirely (write a single tooling.device_skipped finding).
#   - --platform android|ios: run only that side.
#   - No host emulator/simulator → corresponding side becomes a tooling finding.
#   - LLM refine step is OPTIONAL — if the LLM never wrote refined_intent.json,
#     the renderer falls back to draft_intent.json.
#
# Usage:
#   bash scripts/device_perf.sh <audit_id>
#   bash scripts/device_perf.sh <audit_id> --platform android
#   bash scripts/device_perf.sh <audit_id> --quick

set -uo pipefail

AUDIT_ID="${1:-}"
shift || true

PLATFORM="both"
QUICK=0
SKIP_LLM_REFINE=0
PROVIDED_APK=""        # infra-supplied real Hermes APK (skips build_app.sh)
PROVIDED_IPA=""
RUNNER="local"         # local | cloud
CONSENT=0              # required for cloud (uploads customer binary externally)
DURATION_MS="60000"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform)         PLATFORM="$2"; shift 2 ;;
    --quick)            QUICK=1; shift ;;
    --skip-llm-refine)  SKIP_LLM_REFINE=1; shift ;;
    --apk)              PROVIDED_APK="$2"; shift 2 ;;
    --ipa)              PROVIDED_IPA="$2"; shift 2 ;;
    --runner)           RUNNER="$2"; shift 2 ;;   # local | cloud
    --cloud)            RUNNER="cloud"; shift ;;
    --consent|--i-have-upload-consent) CONSENT=1; shift ;;
    --duration-ms)      DURATION_MS="$2"; shift 2 ;;
    *) echo "WARN: unknown arg $1" >&2; shift ;;
  esac
done

if [[ -z "$AUDIT_ID" ]]; then
  echo "ERROR: audit_id required" >&2
  exit 2
fi

RUNS_DIR="${MOBILE_AUDIT_RUNS_DIR:-.audit-runs}"
AUDIT_DIR="${RUNS_DIR}/${AUDIT_ID}"
FINDINGS_DIR="${AUDIT_DIR}/findings"
FLOWS_DIR="${AUDIT_DIR}/flows"
mkdir -p "${FINDINGS_DIR}" "${FLOWS_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY="${PYTHON:-python3}"

emit_tooling() {
  python3 - "${FINDINGS_DIR}/device_orchestrator.json" "$1" "$2" "$3" "$4" <<'PY'
import json, sys
out_path, rid, sev, title, desc = sys.argv[1:]
existing = []
try:
    existing = json.load(open(out_path))
except Exception:
    existing = []
existing.append({
    "id": rid, "layer": "tooling", "category": "tooling_error",
    "severity": sev, "confidence": "high",
    "title": title, "description": desc,
    "evidence": {"file": "device_perf.sh"},
})
json.dump(existing, open(out_path, "w"), indent=2)
PY
}

# Quick mode: write a single finding, skip everything else.
if [[ ${QUICK} -eq 1 ]]; then
  emit_tooling "tooling.device_stage_skipped_quick" "low" \
    "Device measurement skipped (--quick)" \
    "Static + bundle + reassure findings still ran; the report omits device-perf sections."
  echo "[device] --quick set; device stages skipped." >&2
  exit 0
fi

# 4d.1 — obtain build ---------------------------------------------------------
mkdir -p "${AUDIT_DIR}/artifacts"
if [[ -n "${PROVIDED_APK}" || -n "${PROVIDED_IPA}" ]]; then
  # Infra supplied a real Hermes build — preferred. Copy it in, skip EAS build.
  if [[ -n "${PROVIDED_APK}" && -f "${PROVIDED_APK}" ]]; then
    cp "${PROVIDED_APK}" "${AUDIT_DIR}/artifacts/${AUDIT_ID}.apk"
    echo "[device] 4d.1 using provided APK → artifacts/${AUDIT_ID}.apk" >&2
    # Exact shipped sizes from the real APK (better than pod-side expo export).
    ${PY} "${REPO_ROOT}/scripts/apk_scan.py" "${AUDIT_ID}" "${AUDIT_DIR}/artifacts/${AUDIT_ID}.apk" --platform android || true
  fi
  if [[ -n "${PROVIDED_IPA}" && -f "${PROVIDED_IPA}" ]]; then
    cp "${PROVIDED_IPA}" "${AUDIT_DIR}/artifacts/${AUDIT_ID}.ipa"
    echo "[device] 4d.1 using provided IPA → artifacts/${AUDIT_ID}.ipa" >&2
  fi
else
  echo "[device] 4d.1 build (no APK provided; attempting EAS local build)…" >&2
  bash "${SCRIPT_DIR}/build_app.sh" "${AUDIT_ID}" --platform "${PLATFORM}" --skip-if-exists || true
fi

ANDROID_OK=0; IOS_OK=0
[[ -f "${AUDIT_DIR}/artifacts/${AUDIT_ID}.apk" ]] && ANDROID_OK=1
[[ -f "${AUDIT_DIR}/artifacts/${AUDIT_ID}.ipa" ]] && IOS_OK=1

if [[ "${PLATFORM}" == "android" ]]; then IOS_OK=0; fi
if [[ "${PLATFORM}" == "ios"     ]]; then ANDROID_OK=0; fi

if [[ ${ANDROID_OK} -eq 0 && ${IOS_OK} -eq 0 ]]; then
  emit_tooling "tooling.device_no_artifacts" "low" \
    "Neither APK nor IPA available — device stages skipped" \
    "build_app.sh produced no artefacts for the selected platform(s). See build.json findings."
  echo "[device] no artefacts; skipping remaining device stages." >&2
  exit 0
fi

# 4d.2 — screen map -----------------------------------------------------------
echo "[device] 4d.2 extract screen map…" >&2
${PY} "${REPO_ROOT}/scripts/extract_screen_map.py" "${AUDIT_ID}" || \
  emit_tooling "tooling.screen_map_extract_failed" "low" \
    "extract_screen_map.py exited non-zero" \
    "Will attempt to continue with whatever flows/screen_map.json contains."

# 4d.3 — draft flow -----------------------------------------------------------
echo "[device] 4d.3 generate draft flow…" >&2
${PY} "${REPO_ROOT}/scripts/generate_draft_flow.py" "${AUDIT_ID}" || \
  emit_tooling "tooling.draft_flow_failed" "low" \
    "generate_draft_flow.py exited non-zero" \
    "Falling back to whatever main.yaml may already exist."

# Seed main.yaml from draft so validation always has something to run, even if
# the LLM refine step is skipped or never completes.
if [[ -f "${FLOWS_DIR}/draft.yaml" && ! -f "${FLOWS_DIR}/main.yaml" ]]; then
  cp "${FLOWS_DIR}/draft.yaml" "${FLOWS_DIR}/main.yaml"
fi

# 4d.4 — LLM refine (prepare) -------------------------------------------------
if [[ ${SKIP_LLM_REFINE} -eq 0 ]]; then
  echo "[device] 4d.4 prepare LLM-refine inputs…" >&2
  ${PY} "${REPO_ROOT}/scripts/refine_flow_with_llm.py" "${AUDIT_ID}" --prepare || true

  # If the calling SKILL has already produced refined_intent.json, render it.
  # Otherwise, the SKILL's Step 4d.4 will prompt the LLM, then call us with
  # --render after writing the LLM's output.
  if [[ -f "${FLOWS_DIR}/refined_intent.json" ]]; then
    ${PY} "${REPO_ROOT}/scripts/refine_flow_with_llm.py" "${AUDIT_ID}" --render || true
  else
    echo "[device] 4d.4 LLM-refine pending (no refined_intent.json yet; main.yaml stays = draft)." >&2
  fi
fi

# 4d.5/4d.6 — validate + repair (LOCAL runner only) ---------------------------
# These need a local Android emulator. The cloud runner skips them — Flashlight
# Cloud executes the flow on its own devices, and every flow step is optional
# (optional:true), so a mis-guessed selector degrades to partial coverage
# rather than aborting.
if [[ "${RUNNER}" == "local" && ${ANDROID_OK} -eq 1 ]]; then
  echo "[device] 4d.5 validate flow on Android emulator…" >&2
  bash "${SCRIPT_DIR}/validate_flow.sh" "${AUDIT_ID}" || true

  VALIDATION_STATUS="$(python3 - "${FLOWS_DIR}/validation.json" <<'PY'
import json, sys
try:
    v = json.load(open(sys.argv[1]))
except Exception:
    print(""); sys.exit(0)
print(v.get("status", ""))
PY
)"
  if [[ "${VALIDATION_STATUS}" == "PARTIAL" || "${VALIDATION_STATUS}" == "ALL_FAIL" ]]; then
    echo "[device] 4d.6 repair flow (validation=${VALIDATION_STATUS})…" >&2
    ${PY} "${REPO_ROOT}/scripts/repair_flow_with_llm.py" "${AUDIT_ID}" --prepare || true
    if [[ -f "${FLOWS_DIR}/repaired_intent.json" ]]; then
      ${PY} "${REPO_ROOT}/scripts/repair_flow_with_llm.py" "${AUDIT_ID}" --render || true
    else
      emit_tooling "tooling.flow_partial_coverage" "low" \
        "Flow validation failed and no LLM repair was produced" \
        "Maestro will run the current main.yaml with optional=true on every step; coverage will be partial."
    fi
  fi
fi

# 4d.7 — Android measurement --------------------------------------------------
if [[ ${ANDROID_OK} -eq 1 ]]; then
  if [[ "${RUNNER}" == "cloud" ]]; then
    echo "[device] 4d.7 Android perf via Flashlight Cloud…" >&2
    CLOUD_ARGS=(--apk "${AUDIT_DIR}/artifacts/${AUDIT_ID}.apk" --duration-ms "${DURATION_MS}")
    [[ ${CONSENT} -eq 1 ]] && CLOUD_ARGS+=(--consent)
    bash "${SCRIPT_DIR}/run_flashlight_cloud.sh" "${AUDIT_ID}" "${CLOUD_ARGS[@]}" || true
  else
    echo "[device] 4d.7 Android perf via local emulator + Flashlight…" >&2
    bash "${SCRIPT_DIR}/run_android_perf.sh" "${AUDIT_ID}" --duration-ms "${DURATION_MS}" || true
  fi
fi

# 4d.8 — iOS measurement (local only; Flashlight Cloud is Android-only) -------
if [[ ${IOS_OK} -eq 1 ]]; then
  if [[ "${RUNNER}" == "cloud" ]]; then
    emit_tooling "tooling.ios_cloud_unsupported" "low" \
      "iOS runtime not measured — Flashlight Cloud is Android-only" \
      "iOS perf needs a local macOS + Simulator run. Re-run with --runner local on a Mac for iOS metrics."
  else
    echo "[device] 4d.8 iOS perf…" >&2
    bash "${SCRIPT_DIR}/run_ios_perf.sh" "${AUDIT_ID}" || true
  fi
fi

echo "[device] orchestrator complete." >&2
exit 0
