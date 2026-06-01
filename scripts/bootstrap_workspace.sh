#!/usr/bin/env bash
# Stage 3 — Bootstrap the workspace so subsequent stages can run.
#
# Usage:   bash scripts/bootstrap_workspace.sh <audit_id>
#
# Detects the package manager, installs dependencies, runs expo-doctor,
# and writes ${AUDIT_DIR}/facts/audit_meta.json with project metadata.
#
# Exits non-zero only if the audit directory is missing. Install failures
# are recorded as findings (tooling.project_install_failed) so downstream
# stages can degrade gracefully without aborting the audit.

set -uo pipefail

AUDIT_ID="${1:?audit_id required}"
BASE_DIR="${MOBILE_AUDIT_RUNS_DIR:-./.audit-runs}"
AUDIT_DIR="$BASE_DIR/$AUDIT_ID"
WORKSPACE="$AUDIT_DIR/workspace"
FACTS_DIR="$AUDIT_DIR/facts"
FINDINGS_DIR="$AUDIT_DIR/findings"

if [[ ! -d "$AUDIT_DIR" ]]; then
  echo "ERROR: audit directory not found: $AUDIT_DIR" >&2
  exit 1
fi
if [[ ! -d "$WORKSPACE" ]]; then
  echo "ERROR: workspace not populated. Run ingest (Stage 2) first." >&2
  exit 1
fi

mkdir -p "$FACTS_DIR" "$FINDINGS_DIR"

ISO_NOW() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ─────────────────────────────────────────────────────────────────────────────
# Detect package manager
# ─────────────────────────────────────────────────────────────────────────────
PACKAGE_MANAGER="unknown"
INSTALL_CMD=""
if [[ -f "$WORKSPACE/yarn.lock" ]]; then
  PACKAGE_MANAGER="yarn"
  INSTALL_CMD="yarn install --frozen-lockfile"
elif [[ -f "$WORKSPACE/pnpm-lock.yaml" ]]; then
  PACKAGE_MANAGER="pnpm"
  INSTALL_CMD="pnpm install --frozen-lockfile"
elif [[ -f "$WORKSPACE/package-lock.json" ]]; then
  PACKAGE_MANAGER="npm"
  INSTALL_CMD="npm ci"
elif [[ -f "$WORKSPACE/package.json" ]]; then
  PACKAGE_MANAGER="npm"
  INSTALL_CMD="npm install"
else
  echo "ERROR: no package.json in workspace; cannot bootstrap" >&2
  exit 1
fi

echo "Package manager: $PACKAGE_MANAGER" >&2
echo "Install command: $INSTALL_CMD" >&2

# ─────────────────────────────────────────────────────────────────────────────
# Install
# ─────────────────────────────────────────────────────────────────────────────
INSTALL_OK="true"
INSTALL_LOG="$AUDIT_DIR/bootstrap_install.log"
(
  cd "$WORKSPACE" || exit 1
  $INSTALL_CMD
) > "$INSTALL_LOG" 2>&1 || INSTALL_OK="false"

if [[ "$INSTALL_OK" != "true" ]]; then
  echo "WARN: dependency install failed. Writing tooling finding and continuing with raw-source stages only." >&2
  TAIL_OUTPUT="$(tail -n 40 "$INSTALL_LOG" 2>/dev/null | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  cat > "$FINDINGS_DIR/install_failure.json" <<EOF
[
  {
    "id": "tooling.project_install_failed",
    "layer": "tooling",
    "category": "tooling_error",
    "severity": "critical",
    "confidence": "high",
    "title": "Project dependency install failed",
    "description": "The ${PACKAGE_MANAGER} install command failed. Downstream stages that need node_modules (bundle scan, Reassure, device build) have been skipped. Static analysis still ran against raw source.",
    "evidence": {
      "metric_name": "install_exit_code",
      "code_snippet": ${TAIL_OUTPUT}
    },
    "suggested_fix": {
      "summary": "Resolve the ${PACKAGE_MANAGER} install error in the project before re-running the audit."
    }
  }
]
EOF
fi

# ─────────────────────────────────────────────────────────────────────────────
# expo-doctor (best-effort; not fatal if missing)
# ─────────────────────────────────────────────────────────────────────────────
EXPO_DOCTOR_PASS=0
EXPO_DOCTOR_FAIL=0
EXPO_DOCTOR_FILE="$FACTS_DIR/expo_doctor.json"
if [[ "$INSTALL_OK" == "true" ]]; then
  if (cd "$WORKSPACE" && npx --no-install expo-doctor --json) > "$EXPO_DOCTOR_FILE" 2> "$AUDIT_DIR/expo_doctor.log"; then
    EXPO_DOCTOR_PASS="$(python3 -c "import json; d=json.load(open('$EXPO_DOCTOR_FILE')); print(sum(1 for c in d.get('checks',[]) if c.get('status')=='passed'))" 2>/dev/null || echo 0)"
    EXPO_DOCTOR_FAIL="$(python3 -c "import json; d=json.load(open('$EXPO_DOCTOR_FILE')); print(sum(1 for c in d.get('checks',[]) if c.get('status')=='failed'))" 2>/dev/null || echo 0)"
  else
    echo "WARN: expo-doctor unavailable or failed; continuing." >&2
    echo '{}' > "$EXPO_DOCTOR_FILE"
  fi
else
  echo '{}' > "$EXPO_DOCTOR_FILE"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Write audit_meta.json — pure JSON / file presence; gather_facts.py
# does the heavy AST + full facts. This file is a quick-look summary.
# ─────────────────────────────────────────────────────────────────────────────
python3 - "$WORKSPACE" "$FACTS_DIR/audit_meta.json" "$PACKAGE_MANAGER" "$INSTALL_OK" "$EXPO_DOCTOR_PASS" "$EXPO_DOCTOR_FAIL" <<'PYEOF'
import json, os, sys
from pathlib import Path

workspace = Path(sys.argv[1])
out_path = Path(sys.argv[2])
pm = sys.argv[3]
install_ok = sys.argv[4] == "true"
ed_pass = int(sys.argv[5]) if sys.argv[5].isdigit() else 0
ed_fail = int(sys.argv[6]) if sys.argv[6].isdigit() else 0

def load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

pkg = load_json(workspace / "package.json") or {}
deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}

app_json = load_json(workspace / "app.json") or {}
expo_block = app_json.get("expo", {}) if isinstance(app_json, dict) else {}

def coerce_bool(v, default=None):
    if isinstance(v, bool): return v
    if isinstance(v, str): return v.lower() == "true"
    return default

js_engine = expo_block.get("jsEngine")
hermes_enabled = None
if js_engine == "hermes": hermes_enabled = True
elif js_engine == "jsc": hermes_enabled = False

new_arch = expo_block.get("newArchEnabled")
new_arch_enabled = coerce_bool(new_arch, None)

android_block = expo_block.get("android", {}) if isinstance(expo_block.get("android"), dict) else {}
ios_block = expo_block.get("ios", {}) if isinstance(expo_block.get("ios"), dict) else {}

def first_segment_of_dep(name):
    val = deps.get(name)
    if not val: return None
    # strip semver prefix / range
    return val.lstrip("^~>=< ").split(" ")[0]

meta = {
    "package_manager": pm,
    "install_ok": install_ok,
    "expo_doctor": {"passed": ed_pass, "failed": ed_fail},
    "project_signature_preview": {
        "expo_sdk_version": first_segment_of_dep("expo"),
        "react_native_version": first_segment_of_dep("react-native"),
        "react_version": first_segment_of_dep("react"),
        "typescript_present": "typescript" in deps,
        "expo_router_present": "expo-router" in deps,
        "react_navigation_present": any(d.startswith("@react-navigation/") for d in deps),
        "hermes_enabled": hermes_enabled,
        "new_architecture_enabled": new_arch_enabled,
        "bundle_identifier_android": android_block.get("package"),
        "bundle_identifier_ios": ios_block.get("bundleIdentifier"),
    },
    "deps_preview": {
        "production_count": len(pkg.get("dependencies") or {}),
        "dev_count": len(pkg.get("devDependencies") or {}),
        "expo_image_present": "expo-image" in deps,
        "flash_list_present": "@shopify/flash-list" in deps,
        "reanimated_present": "react-native-reanimated" in deps,
        "gesture_handler_present": "react-native-gesture-handler" in deps,
        "screens_present": "react-native-screens" in deps,
    },
}
out_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
print(f"wrote {out_path}", file=sys.stderr)
PYEOF

echo "Bootstrap completed at $(ISO_NOW)" >&2
echo "audit_meta.json: $FACTS_DIR/audit_meta.json" >&2
[[ "$INSTALL_OK" == "true" ]] || echo "(install failed — see $INSTALL_LOG; tooling finding written)" >&2
exit 0
