#!/usr/bin/env bash
# Stage 4c (part 2) — run Reassure perf tests.
#
# Generates tests via gen_reassure_tests.py (if not already done), then runs
# `npx reassure` against the workspace. Reassure runs each `*.perf-test.tsx`
# under jest-expo, measuring render count and duration. Output JSON is parsed
# by the companion script `scripts/transform_reassure.py` into Findings.
#
# Failure handling:
#   - If reassure is not installed and `npm i -D` fails → emit
#     tooling.reassure_unavailable Finding, exit 0.
#   - If a single test file fails to render → captured in the Reassure JSON,
#     transformed into a `reassure.render_failure` Finding by the transformer.
#   - Total Reassure failure → emit tooling.reassure_run_failed Finding, exit 0.
#
# Usage: bash scripts/run_reassure.sh <audit_id>

set -uo pipefail

AUDIT_ID="${1:-}"
if [[ -z "$AUDIT_ID" ]]; then
  echo "ERROR: audit_id is required" >&2
  echo "Usage: bash scripts/run_reassure.sh <audit_id>" >&2
  exit 2
fi

RUNS_DIR="${MOBILE_AUDIT_RUNS_DIR:-.audit-runs}"
AUDIT_DIR="${RUNS_DIR}/${AUDIT_ID}"
WORKSPACE="${AUDIT_DIR}/workspace"
FINDINGS_DIR="${AUDIT_DIR}/findings"
mkdir -p "${FINDINGS_DIR}"

# Repo root (location of this script's parent's parent)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY="${PYTHON:-python3}"

# Helpers ---------------------------------------------------------------------

emit_tooling_finding() {
  local id="$1"
  local title="$2"
  local description="$3"
  cat > "${FINDINGS_DIR}/reassure.json" <<EOF
[
  {
    "id": "${id}",
    "layer": "tooling",
    "category": "tooling_error",
    "severity": "low",
    "confidence": "high",
    "title": "${title}",
    "description": ${description},
    "evidence": {"file": "${WORKSPACE}"}
  }
]
EOF
}

if [[ ! -d "${WORKSPACE}" ]]; then
  echo "ERROR: workspace missing: ${WORKSPACE}" >&2
  emit_tooling_finding "tooling.reassure_workspace_missing" \
    "Workspace missing — Reassure skipped" \
    "\"Expected ${WORKSPACE}; ingest / bootstrap stages may have failed.\""
  exit 0
fi

# Step 1: generate tests ------------------------------------------------------
echo "[reassure] generating per-screen perf tests…" >&2
"${PY}" "${REPO_ROOT}/scripts/gen_reassure_tests.py" "${AUDIT_ID}" || {
  emit_tooling_finding "tooling.reassure_gen_failed" \
    "Test generator failed" \
    "\"gen_reassure_tests.py returned non-zero; see stderr for details.\""
  exit 0
}

TESTS_DIR="${WORKSPACE}/__reassure_tests__"
if [[ ! -d "${TESTS_DIR}" ]] || [[ -z "$(ls -A "${TESTS_DIR}" 2>/dev/null)" ]]; then
  emit_tooling_finding "tooling.reassure_no_tests" \
    "No perf tests generated" \
    "\"No component-shaped screens were detected. See findings/reassure_gen.json for the diagnostic.\""
  exit 0
fi

# Step 2: ensure reassure is installed in the workspace -----------------------
pushd "${WORKSPACE}" > /dev/null

if ! npx --no-install reassure --version > /dev/null 2>&1; then
  echo "[reassure] installing reassure + jest-expo (devDependencies)…" >&2
  npm i -D reassure jest-expo @testing-library/react-native --no-audit --no-fund > /dev/null 2>&1 || {
    popd > /dev/null
    emit_tooling_finding "tooling.reassure_unavailable" \
      "Could not install reassure" \
      "\"npm i -D reassure failed. Reassure stage skipped; install manually inside the workspace to re-enable.\""
    exit 0
  }
fi

# Ensure jest preset exists. If the project already configures jest, leave it
# alone; otherwise drop a minimal config that points jest at jest-expo and the
# generated tests dir.
if [[ ! -f "jest.config.js" && ! -f "jest.config.ts" && ! -f "jest.config.json" ]]; then
  cat > "jest.audit.config.js" <<'EOF'
module.exports = {
  preset: 'jest-expo',
  testMatch: ['**/__reassure_tests__/**/*.perf-test.tsx'],
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native|react-clone-referenced-element|@react-native-community|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@unimodules/.*|unimodules|sentry-expo|native-base|react-native-svg|react-native-reanimated|@shopify/.*|moti|@gorhom/.*))',
  ],
};
EOF
  REASSURE_CONFIG="--testRunnerOptions='{\"config\":\"jest.audit.config.js\"}'"
else
  REASSURE_CONFIG=""
fi

# Step 3: run reassure --------------------------------------------------------
mkdir -p .reassure
echo "[reassure] running perf measurement…" >&2
if [[ -n "${REASSURE_CONFIG}" ]]; then
  npx --no-install reassure --output json --output-file .reassure/current.perf > "${AUDIT_DIR}/reassure_stdout.log" 2>&1 \
    --testRunnerOptions='{"config":"jest.audit.config.js"}' || true
else
  npx --no-install reassure --output json --output-file .reassure/current.perf > "${AUDIT_DIR}/reassure_stdout.log" 2>&1 || true
fi
popd > /dev/null

REASSURE_OUTPUT="${WORKSPACE}/.reassure/current.perf"
if [[ ! -s "${REASSURE_OUTPUT}" ]]; then
  emit_tooling_finding "tooling.reassure_run_failed" \
    "Reassure produced no output" \
    "\"See ${AUDIT_DIR}/reassure_stdout.log for the test runner output.\""
  exit 0
fi

# Step 4: transform results into Findings ------------------------------------
"${PY}" "${REPO_ROOT}/scripts/transform_reassure.py" "${AUDIT_ID}" "${REASSURE_OUTPUT}" \
  > "${FINDINGS_DIR}/reassure.json" || {
  emit_tooling_finding "tooling.reassure_transform_failed" \
    "Could not parse Reassure output" \
    "\"transform_reassure.py raised. Raw output preserved at ${REASSURE_OUTPUT}.\""
  exit 0
}

echo "[reassure] complete — findings in ${FINDINGS_DIR}/reassure.json" >&2
exit 0
