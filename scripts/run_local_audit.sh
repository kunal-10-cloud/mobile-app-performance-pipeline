#!/usr/bin/env bash
# Local-source audit runner.
#
# Bypasses the envcore_gateway MCP path (which is meant for hosted pods) and
# audits a directory on the local filesystem instead. Useful for:
#   - testing the pipeline against the fixture under test-fixture/
#   - dry-running an audit on a local clone of a real project
#   - iterating on new rules without round-tripping through the host
#
# What it does:
#   1. Generate an audit_id and create .audit-runs/<id>/
#   2. Copy <source-dir> into <audit>/workspace/ (honouring the same denylist
#      ingest_pod.py applies — so secrets / node_modules / build dirs are skipped)
#   3. Run gather_facts + bootstrap_workspace (the latter only attempts install
#      if not --skip-install)
#   4. Run the analyzer workers per the --slice flag
#   5. Aggregate, Pass A verify, synthesize, render
#
# Slice gating:
#   --slice 1   static + config only (default — fastest, no native install needed)
#   --slice 2   add bundle + reassure (needs node_modules)
#   --slice 3   add device measurement (needs emulator + Maestro + Flashlight)
#
# Usage:
#   bash scripts/run_local_audit.sh <source-dir>
#   bash scripts/run_local_audit.sh test-fixture --slice 2
#   bash scripts/run_local_audit.sh ~/projects/my-expo-app --slice 1 --skip-install

set -uo pipefail

SOURCE_DIR="${1:-}"
shift || true

SLICE=1
SKIP_INSTALL=0
SKIP_LLM=1   # local runs default to no LLM round-trip; the deterministic stubs render fine
WAKE_JOB_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slice)        SLICE="$2"; shift 2 ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --with-llm)     SKIP_LLM=0; shift ;;
    --wake)         WAKE_JOB_ID="$2"; shift 2 ;;
    *) echo "WARN: unknown arg $1" >&2; shift ;;
  esac
done

if [[ -z "$SOURCE_DIR" ]]; then
  echo "ERROR: source-dir required" >&2
  echo "Usage: bash scripts/run_local_audit.sh <source-dir> [--slice 1|2|3] [--skip-install] [--wake <job_id>]" >&2
  exit 2
fi
if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "ERROR: source-dir not found: $SOURCE_DIR" >&2
  exit 2
fi

# Optional pre-flight: wake the pod (only useful when SOURCE_DIR was just
# rsync'd from an Emergent pod and we want to keep it warm for follow-up
# ingest calls). For purely local fixture testing, leave --wake unset.
if [[ -n "${WAKE_JOB_ID}" ]]; then
  if [[ -z "${EMERGENT_AUTH_TOKEN:-}" ]]; then
    echo "ERROR: --wake set but EMERGENT_AUTH_TOKEN is not in env. Aborting." >&2
    exit 2
  fi
  echo "[run-local] waking pod ${WAKE_JOB_ID}…" >&2
  py -3.12 "${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/wake_pod.py" "${WAKE_JOB_ID}" || {
    echo "ERROR: wake_pod.py failed for ${WAKE_JOB_ID}; downstream ingest will likely fail." >&2
    # Continue anyway — the user may have a stale workspace they still want to audit.
  }
fi

# Resolve absolute path so subsequent cd's don't break it.
SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY="${PYTHON:-python3}"
RUNS_DIR="${MOBILE_AUDIT_RUNS_DIR:-${REPO_ROOT}/.audit-runs}"
export MOBILE_AUDIT_RUNS_DIR="${RUNS_DIR}"

# 1) init -------------------------------------------------------------------
AUDIT_ID="local-$(date +%Y%m%d-%H%M%S)"
echo "[run-local] audit_id=${AUDIT_ID}" >&2
bash "${SCRIPT_DIR}/init_audit.sh" "local://${SOURCE_DIR}" "${AUDIT_ID}" || {
  echo "ERROR: init_audit.sh failed" >&2; exit 1;
}

AUDIT_DIR="${RUNS_DIR}/${AUDIT_ID}"
WORKSPACE="${AUDIT_DIR}/workspace"
mkdir -p "${WORKSPACE}"

# 2) copy source with the same denylist ingest_pod.py uses -----------------
echo "[run-local] copying source → ${WORKSPACE}" >&2
${PY} "${SCRIPT_DIR}/ingest_pod.py" --gather-local "${SOURCE_DIR}" "${WORKSPACE}" || {
  echo "ERROR: ingest_pod.py --gather-local failed" >&2; exit 1;
}

# 3) bootstrap + facts -----------------------------------------------------
if [[ "${SKIP_INSTALL}" -eq 1 ]]; then
  echo "[run-local] --skip-install: skipping bootstrap_workspace.sh" >&2
  # Minimal audit_meta.json so config_scan / synthesize have something to read.
  mkdir -p "${AUDIT_DIR}/facts"
  if [[ ! -f "${AUDIT_DIR}/facts/audit_meta.json" ]]; then
    echo '{"package_manager":"unknown","install_skipped":true}' > "${AUDIT_DIR}/facts/audit_meta.json"
  fi
else
  bash "${SCRIPT_DIR}/bootstrap_workspace.sh" "${AUDIT_ID}" || \
    echo "[run-local] bootstrap_workspace.sh returned non-zero (continuing)" >&2
fi

echo "[run-local] gather_facts…" >&2
${PY} "${SCRIPT_DIR}/gather_facts.py" "${AUDIT_ID}" || \
  echo "[run-local] gather_facts.py returned non-zero (continuing)" >&2

# 4) analyzers per slice ---------------------------------------------------
echo "[run-local] static_scan…" >&2
${PY} "${SCRIPT_DIR}/static_scan.py" "${AUDIT_ID}" || \
  echo "[run-local] static_scan.py returned non-zero (continuing)" >&2

echo "[run-local] config_scan…" >&2
${PY} "${SCRIPT_DIR}/config_scan.py" "${AUDIT_ID}" || \
  echo "[run-local] config_scan.py returned non-zero (continuing)" >&2

if [[ "${SLICE}" -ge 2 ]]; then
  if [[ "${SKIP_INSTALL}" -eq 1 ]]; then
    echo "[run-local] WARN: --slice 2 with --skip-install — bundle_scan will likely fail at 'expo export'." >&2
  fi
  echo "[run-local] bundle_scan…" >&2
  ${PY} "${SCRIPT_DIR}/bundle_scan.py" "${AUDIT_ID}" || \
    echo "[run-local] bundle_scan.py returned non-zero (continuing)" >&2

  echo "[run-local] run_reassure…" >&2
  bash "${SCRIPT_DIR}/run_reassure.sh" "${AUDIT_ID}" || \
    echo "[run-local] run_reassure.sh returned non-zero (continuing)" >&2
fi

if [[ "${SLICE}" -ge 3 ]]; then
  echo "[run-local] device_perf orchestrator…" >&2
  bash "${SCRIPT_DIR}/device_perf.sh" "${AUDIT_ID}" || \
    echo "[run-local] device_perf.sh returned non-zero (continuing)" >&2
fi

# 5) aggregate + Pass A + synthesize + render ------------------------------
echo "[run-local] aggregate_findings…" >&2
${PY} "${SCRIPT_DIR}/aggregate_findings.py" "${AUDIT_ID}" || {
  echo "ERROR: aggregate_findings.py failed — see above. Aborting." >&2; exit 1;
}

echo "[run-local] pass_a_verify…" >&2
${PY} "${SCRIPT_DIR}/pass_a_verify.py" "${AUDIT_ID}" || {
  echo "ERROR: pass_a_verify.py failed. Aborting." >&2; exit 1;
}

echo "[run-local] synthesize…" >&2
${PY} "${SCRIPT_DIR}/synthesize.py" "${AUDIT_ID}" || {
  echo "ERROR: synthesize.py failed. Aborting." >&2; exit 1;
}

if [[ "${SKIP_LLM}" -eq 0 ]]; then
  echo "[run-local] --with-llm specified, but the local runner does not invoke an LLM directly." >&2
  echo "[run-local]   Write your LLM's prose_fills.json to ${AUDIT_DIR}/report/prose_fills.json before render." >&2
fi

echo "[run-local] render_report…" >&2
${PY} "${SCRIPT_DIR}/render_report.py" "${AUDIT_ID}" || {
  echo "ERROR: render_report.py failed. Aborting." >&2; exit 1;
}

echo "" >&2
echo "[run-local] DONE — artefacts at: ${AUDIT_DIR}" >&2
echo "[run-local] open: ${AUDIT_DIR}/report/report.md" >&2
