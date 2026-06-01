#!/usr/bin/env bash
# Stage 4d.1 — Build APK / IPA for device measurement.
#
# Builds locally via `eas build --local`. Cloud builds are far slower and burn
# EAS quota; local builds reuse the workspace's installed deps and emit
# directly to ${AUDIT_DIR}/artifacts/.
#
# Per-platform behaviour:
#   - Android: `eas build --platform android --profile preview --local --output ../artifacts/<id>.apk`
#   - iOS:     `eas build --platform ios     --profile preview --local --output ../artifacts/<id>.ipa`
#
# Each platform is independent. Failure on one does NOT abort the other.
# Each failure mode produces one tooling.* finding in findings/build.json and
# the script exits 0 so downstream stages decide whether to proceed.
#
# Usage:
#   bash scripts/build_app.sh <audit_id>                  # both platforms
#   bash scripts/build_app.sh <audit_id> --platform android
#   bash scripts/build_app.sh <audit_id> --platform ios
#   bash scripts/build_app.sh <audit_id> --skip-if-exists  # reuse pre-existing builds

set -uo pipefail

AUDIT_ID="${1:-}"
shift || true
PLATFORM="both"
SKIP_IF_EXISTS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --skip-if-exists) SKIP_IF_EXISTS=1; shift ;;
    *) echo "WARN: unknown arg $1" >&2; shift ;;
  esac
done

if [[ -z "$AUDIT_ID" ]]; then
  echo "ERROR: audit_id is required" >&2
  echo "Usage: bash scripts/build_app.sh <audit_id> [--platform android|ios|both] [--skip-if-exists]" >&2
  exit 2
fi

RUNS_DIR="${MOBILE_AUDIT_RUNS_DIR:-.audit-runs}"
AUDIT_DIR="${RUNS_DIR}/${AUDIT_ID}"
WORKSPACE="${AUDIT_DIR}/workspace"
ARTIFACTS_DIR="${AUDIT_DIR}/artifacts"
FINDINGS_DIR="${AUDIT_DIR}/findings"
mkdir -p "${ARTIFACTS_DIR}" "${FINDINGS_DIR}"

ANDROID_OUT="${ARTIFACTS_DIR}/${AUDIT_ID}.apk"
IOS_OUT="${ARTIFACTS_DIR}/${AUDIT_ID}.ipa"

# Findings accumulator: we append tab-separated lines to a temp file and let
# Python turn them into a JSON array at the very end. This keeps shell-escaping
# trivial (no jq, no heredoc-in-heredoc tricks).
FINDINGS_TSV="${AUDIT_DIR}/.build_findings.tsv"
: > "${FINDINGS_TSV}"

push_finding() {
  # push_finding <id> <severity> <title> <description>
  # Strip embedded tabs/newlines from inputs so the TSV stays well-formed.
  printf '%s\t%s\t%s\t%s\n' \
    "$(printf '%s' "$1" | tr '\t\n' '  ')" \
    "$(printf '%s' "$2" | tr '\t\n' '  ')" \
    "$(printf '%s' "$3" | tr '\t\n' '  ')" \
    "$(printf '%s' "$4" | tr '\t\n' '  ')" \
    >> "${FINDINGS_TSV}"
}

write_findings() {
  python3 - "${FINDINGS_TSV}" "${FINDINGS_DIR}/build.json" <<'PY'
import json, sys
tsv_path, out_path = sys.argv[1], sys.argv[2]
arr = []
with open(tsv_path, encoding="utf-8") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        rid, sev, title, desc = parts[0], parts[1], parts[2], "\t".join(parts[3:])
        arr.append({
            "id": rid,
            "layer": "tooling",
            "category": "tooling_error",
            "severity": sev,
            "confidence": "high",
            "title": title,
            "description": desc,
            "evidence": {"file": "build"},
        })
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(arr, fh, indent=2)
PY
}

if [[ ! -d "${WORKSPACE}" ]]; then
  push_finding "tooling.build_workspace_missing" "low" \
    "Workspace missing — build skipped" \
    "Expected ${WORKSPACE}; ingest must have failed earlier."
  write_findings
  exit 0
fi

# Detect EAS availability ----------------------------------------------------

EAS_OK=1
if ! command -v eas > /dev/null 2>&1; then
  if ! npx --no-install eas --version > /dev/null 2>&1; then
    EAS_OK=0
  fi
fi

if [[ ${EAS_OK} -eq 0 ]]; then
  push_finding "tooling.eas_unavailable" "low" \
    "EAS CLI not available — device builds skipped" \
    "eas-cli is not on PATH and is not installed in the workspace. Install with 'npm i -g eas-cli' and log in once via 'eas login' on the host machine."
  write_findings
  exit 0
fi

# Detect eas.json -------------------------------------------------------------

if [[ ! -f "${WORKSPACE}/eas.json" ]]; then
  push_finding "tooling.eas_config_missing" "low" \
    "eas.json missing — device builds skipped" \
    "Stage 4d expects an eas.json with at minimum a 'preview' profile. Add one via 'eas build:configure' inside the project."
  write_findings
  exit 0
fi

# Build Android --------------------------------------------------------------

build_android() {
  if [[ ${SKIP_IF_EXISTS} -eq 1 && -f "${ANDROID_OUT}" ]]; then
    echo "[build] reusing existing ${ANDROID_OUT}" >&2
    return 0
  fi
  echo "[build] android — eas build --platform android --profile preview --local" >&2
  local log="${AUDIT_DIR}/build_android.log"
  (
    cd "${WORKSPACE}"
    eas build --platform android --profile preview --local \
      --non-interactive \
      --output "${ANDROID_OUT}"
  ) > "${log}" 2>&1
  local rc=$?
  if [[ $rc -ne 0 || ! -f "${ANDROID_OUT}" ]]; then
    local tail
    tail="$(tail -n 80 "${log}" 2>/dev/null | tr '\n' ' ' | head -c 1500)"
    push_finding "tooling.android_build_failed" "low" \
      "Android local build failed" \
      "eas build exited ${rc}. Last log: ${tail}"
    return 1
  fi
  echo "[build] android OK → ${ANDROID_OUT}" >&2
  return 0
}

# Build iOS ------------------------------------------------------------------

build_ios() {
  if [[ "$(uname)" != "Darwin" ]]; then
    push_finding "tooling.ios_build_unsupported_host" "low" \
      "iOS build skipped — non-macOS host" \
      "Local iOS builds require macOS with Xcode. The host appears to be $(uname); iOS measurement will be skipped."
    return 1
  fi
  if [[ ${SKIP_IF_EXISTS} -eq 1 && -f "${IOS_OUT}" ]]; then
    echo "[build] reusing existing ${IOS_OUT}" >&2
    return 0
  fi
  echo "[build] ios — eas build --platform ios --profile preview --local" >&2
  local log="${AUDIT_DIR}/build_ios.log"
  (
    cd "${WORKSPACE}"
    eas build --platform ios --profile preview --local \
      --non-interactive \
      --output "${IOS_OUT}"
  ) > "${log}" 2>&1
  local rc=$?
  if [[ $rc -ne 0 || ! -f "${IOS_OUT}" ]]; then
    local tail
    tail="$(tail -n 80 "${log}" 2>/dev/null | tr '\n' ' ' | head -c 1500)"
    push_finding "tooling.ios_build_failed" "low" \
      "iOS local build failed" \
      "eas build exited ${rc}. Last log: ${tail}"
    return 1
  fi
  echo "[build] ios OK → ${IOS_OUT}" >&2
  return 0
}

case "${PLATFORM}" in
  android) build_android || true ;;
  ios)     build_ios     || true ;;
  both)    build_android || true; build_ios || true ;;
  *) echo "WARN: unknown platform '${PLATFORM}', defaulting to both" >&2
     build_android || true; build_ios || true ;;
esac

write_findings
echo "[build] done — see ${FINDINGS_DIR}/build.json" >&2
exit 0
