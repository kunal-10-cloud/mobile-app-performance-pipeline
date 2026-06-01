#!/usr/bin/env bash
# Stage 4d.5 — Dry-run the generated Maestro flow on Android to find broken steps.
#
# This is NOT a measurement run; it's a fast pass to catch flow steps that
# can't find their targets (e.g. tab label "Profile" guessed wrong; the actual
# label is "Account"). Output validation.json + flows/debug/ UI dumps feed the
# repair_flow_with_llm.py step.
#
# Boots the preferred AVD (per configs/android-emulator.json) if no emulator
# is already running. Installs the APK. Runs maestro test once. Captures the
# step-by-step results into flows/validation.json.
#
# Fail-soft at every step: each problem produces a tooling.* finding instead of
# aborting; the audit prefers a partial-coverage report to no report.
#
# Usage:
#   bash scripts/validate_flow.sh <audit_id>
#   bash scripts/validate_flow.sh <audit_id> --device-id emulator-5554

set -uo pipefail

AUDIT_ID="${1:-}"
shift || true
DEVICE_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    *) echo "WARN: unknown arg $1" >&2; shift ;;
  esac
done

if [[ -z "$AUDIT_ID" ]]; then
  echo "ERROR: audit_id is required" >&2
  exit 2
fi

RUNS_DIR="${MOBILE_AUDIT_RUNS_DIR:-.audit-runs}"
AUDIT_DIR="${RUNS_DIR}/${AUDIT_ID}"
FLOWS_DIR="${AUDIT_DIR}/flows"
FINDINGS_DIR="${AUDIT_DIR}/findings"
ARTIFACTS_DIR="${AUDIT_DIR}/artifacts"
APK="${ARTIFACTS_DIR}/${AUDIT_ID}.apk"
FLOW_YAML="${FLOWS_DIR}/main.yaml"
DEBUG_DIR="${FLOWS_DIR}/debug"
VALIDATION_JSON="${FLOWS_DIR}/validation.json"

mkdir -p "${FINDINGS_DIR}" "${FLOWS_DIR}" "${DEBUG_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
EMU_CONFIG="${REPO_ROOT}/configs/android-emulator.json"

# Findings buffer (tsv → JSON at the end) ------------------------------------

FINDINGS_TSV="${AUDIT_DIR}/.validate_findings.tsv"
: > "${FINDINGS_TSV}"

push_finding() {
  printf '%s\t%s\t%s\t%s\n' \
    "$(printf '%s' "$1" | tr '\t\n' '  ')" \
    "$(printf '%s' "$2" | tr '\t\n' '  ')" \
    "$(printf '%s' "$3" | tr '\t\n' '  ')" \
    "$(printf '%s' "$4" | tr '\t\n' '  ')" \
    >> "${FINDINGS_TSV}"
}

write_findings() {
  python3 - "${FINDINGS_TSV}" "${FINDINGS_DIR}/flow_validate.json" <<'PY'
import json, sys
arr = []
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        rid, sev, title, desc = parts[0], parts[1], parts[2], "\t".join(parts[3:])
        arr.append({
            "id": rid, "layer": "tooling", "category": "tooling_error",
            "severity": sev, "confidence": "high",
            "title": title, "description": desc,
            "evidence": {"file": "flows/main.yaml"},
        })
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(arr, fh, indent=2)
PY
}

bail_with_finding() {
  push_finding "$1" "$2" "$3" "$4"
  write_findings
  # Write an empty validation.json so downstream stages know the validator ran.
  python3 - "${VALIDATION_JSON}" <<'PY'
import json, sys
json.dump({"steps": [], "status": "could_not_run"}, open(sys.argv[1], "w"), indent=2)
PY
  exit 0
}

# Preconditions --------------------------------------------------------------

if [[ ! -f "${FLOW_YAML}" ]]; then
  bail_with_finding "tooling.validate_flow_yaml_missing" "low" \
    "Main flow YAML missing — validate skipped" \
    "Expected ${FLOW_YAML}; generate_draft_flow.py / refine_flow_with_llm.py must run first."
fi
if [[ ! -f "${APK}" ]]; then
  bail_with_finding "tooling.validate_apk_missing" "low" \
    "APK missing — validate skipped" \
    "Expected ${APK}; build_app.sh did not produce an Android APK."
fi
if ! command -v maestro > /dev/null 2>&1; then
  bail_with_finding "tooling.maestro_unavailable" "low" \
    "Maestro CLI not on PATH" \
    "Install Maestro from maestro.mobile.dev; validate_flow.sh skipped."
fi
if ! command -v adb > /dev/null 2>&1; then
  bail_with_finding "tooling.adb_unavailable" "low" \
    "adb not on PATH" \
    "Android platform-tools / SDK not installed. validate_flow.sh skipped."
fi

# Boot emulator if needed ----------------------------------------------------

ensure_emulator() {
  # Use the user-provided device first.
  if [[ -n "${DEVICE_ID}" ]]; then
    echo "[validate] using user-specified device ${DEVICE_ID}" >&2
    DEV="${DEVICE_ID}"
    return 0
  fi
  # Already-booted?
  if adb devices | awk '/^emulator-[0-9]+/ && $2 == "device" {print; exit}' | grep -q .; then
    DEV="$(adb devices | awk '/^emulator-[0-9]+/ && $2 == "device" {print $1; exit}')"
    echo "[validate] reusing running emulator ${DEV}" >&2
    return 0
  fi
  if ! command -v emulator > /dev/null 2>&1; then
    push_finding "tooling.android_emulator_unavailable" "low" \
      "Android emulator binary not found" \
      "\`emulator\` not on PATH and no device pre-booted. Validation can't run; the flow will be skipped."
    return 1
  fi
  # Pick first available preferred or fallback AVD.
  AVAILABLE_AVDS="$(emulator -list-avds 2>/dev/null || true)"
  PREFERRED_AVD="$(python3 - "${EMU_CONFIG}" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else {}
print(cfg.get("preferred_avd_name", ""))
PY
)"
  CHOSEN=""
  for cand in ${PREFERRED_AVD} $(python3 - "${EMU_CONFIG}" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
for n in cfg.get("fallback_avd_names", []):
    print(n)
PY
); do
    if echo "${AVAILABLE_AVDS}" | grep -Fxq "${cand}"; then
      CHOSEN="${cand}"
      break
    fi
  done
  if [[ -z "${CHOSEN}" ]]; then
    push_finding "tooling.android_avd_unavailable" "low" \
      "No suitable Android AVD found" \
      "Looked for the preferred + fallback AVDs in configs/android-emulator.json; none were installed. Create one or pass --device-id."
    return 1
  fi
  echo "[validate] booting AVD ${CHOSEN} in background…" >&2
  emulator -avd "${CHOSEN}" \
    -no-snapshot-save -no-boot-anim -no-audio \
    -gpu swiftshader_indirect -memory 2048 -cores 2 \
    > "${AUDIT_DIR}/emulator.log" 2>&1 &
  # Wait up to 3 minutes for boot
  for _ in $(seq 1 90); do
    if adb shell getprop sys.boot_completed 2>/dev/null | grep -q '^1'; then
      break
    fi
    sleep 2
  done
  if ! adb shell getprop sys.boot_completed 2>/dev/null | grep -q '^1'; then
    push_finding "tooling.android_emulator_boot_timeout" "low" \
      "Android emulator did not finish booting in time" \
      "See ${AUDIT_DIR}/emulator.log. Subsequent device stages will be skipped."
    return 1
  fi
  DEV="$(adb devices | awk '/^emulator-[0-9]+/ && $2 == "device" {print $1; exit}')"
  # Disable animations for stable measurement (per android-emulator.json post_boot_settings).
  adb -s "${DEV}" shell settings put global window_animation_scale 0 || true
  adb -s "${DEV}" shell settings put global transition_animation_scale 0 || true
  adb -s "${DEV}" shell settings put global animator_duration_scale 0 || true
  echo "[validate] emulator ready: ${DEV}" >&2
  return 0
}

DEV=""
ensure_emulator || {
  write_findings
  python3 - "${VALIDATION_JSON}" <<'PY'
import json, sys
json.dump({"steps": [], "status": "no_device"}, open(sys.argv[1], "w"), indent=2)
PY
  exit 0
}

# Install APK ----------------------------------------------------------------

echo "[validate] installing APK…" >&2
adb -s "${DEV}" install -r -t "${APK}" > "${AUDIT_DIR}/adb_install.log" 2>&1 || {
  TAIL="$(tail -n 40 "${AUDIT_DIR}/adb_install.log" | tr '\n' ' ' | head -c 1000)"
  bail_with_finding "tooling.android_install_failed" "low" \
    "adb install failed" \
    "Last log: ${TAIL}"
}

# Run maestro test (dry-run) -------------------------------------------------

rm -rf "${DEBUG_DIR}"
mkdir -p "${DEBUG_DIR}"

echo "[validate] running maestro test (dry run)…" >&2
maestro test "${FLOW_YAML}" \
  --debug-output "${DEBUG_DIR}" \
  --format junit \
  --output "${FLOWS_DIR}/validation.junit.xml" \
  > "${AUDIT_DIR}/maestro_validate.log" 2>&1
MAESTRO_RC=$?

# Maestro returns non-zero whenever any step failed; we don't treat that as a
# fatal error — we want the per-step results.
echo "[validate] maestro exit ${MAESTRO_RC}" >&2

# Transform the JUnit output into our internal validation.json shape so the
# repair step has a uniform input regardless of the maestro version.
python3 - "${FLOWS_DIR}/validation.junit.xml" "${VALIDATION_JSON}" <<'PY'
import json, sys, xml.etree.ElementTree as ET
junit, out = sys.argv[1], sys.argv[2]
try:
    tree = ET.parse(junit)
except (FileNotFoundError, ET.ParseError):
    json.dump({"steps": [], "status": "no_junit"}, open(out, "w"), indent=2)
    sys.exit(0)
root = tree.getroot()
steps = []
failed = 0
total = 0
for tc in root.iter("testcase"):
    total += 1
    label = tc.get("name") or "<unnamed step>"
    failure = tc.find("failure")
    error = tc.find("error")
    if failure is not None or error is not None:
        failed += 1
        msg = (failure.get("message") if failure is not None else error.get("message")) or ""
        steps.append({"label": label, "status": "FAIL", "message": msg[:600]})
    else:
        steps.append({"label": label, "status": "PASS", "message": ""})
status = "ALL_PASS" if failed == 0 else ("PARTIAL" if failed < total else "ALL_FAIL")
json.dump({"steps": steps, "status": status, "total": total, "failed": failed}, open(out, "w"), indent=2)
PY

# Findings for non-optional failures the repair step may want to address.
python3 - "${VALIDATION_JSON}" "${FINDINGS_TSV}" <<'PY'
import json, sys
val = json.load(open(sys.argv[1]))
tsv = sys.argv[2]
fails = [s for s in val.get("steps", []) if s.get("status") == "FAIL"]
if not fails:
    sys.exit(0)
with open(tsv, "a", encoding="utf-8") as fh:
    fh.write("\t".join([
        "tooling.flow_validation_step_failed",
        "low",
        f"{len(fails)} Maestro step(s) failed during validation",
        "Repair step will attempt to patch the flow with UI dumps from these failures.",
    ]) + "\n")
PY

write_findings
echo "[validate] complete → ${VALIDATION_JSON}" >&2
exit 0
