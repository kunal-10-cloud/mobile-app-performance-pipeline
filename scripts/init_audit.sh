#!/usr/bin/env bash
# Stage 1 — Initialize the per-audit working directory.
#
# Usage:   bash scripts/init_audit.sh <pod_id> [audit_id]
# Output:  prints the audit_id to stdout (last line).
#
# Honours MOBILE_AUDIT_RUNS_DIR env var; defaults to ./.audit-runs/.
# Fails loudly if the audit directory already exists (never overwrites).

set -euo pipefail

POD_ID="${1:?pod_id required as first argument}"
AUDIT_ID="${2:-}"

if [[ -z "$AUDIT_ID" ]]; then
  if command -v uuidgen >/dev/null 2>&1; then
    AUDIT_ID="$(uuidgen | tr 'A-Z' 'a-z')"
  else
    AUDIT_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  fi
fi

BASE_DIR="${MOBILE_AUDIT_RUNS_DIR:-./.audit-runs}"
AUDIT_DIR="$BASE_DIR/$AUDIT_ID"

if [[ -d "$AUDIT_DIR" ]]; then
  echo "ERROR: audit directory already exists: $AUDIT_DIR" >&2
  echo "Refusing to overwrite. Use a fresh audit_id." >&2
  exit 1
fi

mkdir -p \
  "$AUDIT_DIR/workspace" \
  "$AUDIT_DIR/evidence" \
  "$AUDIT_DIR/facts" \
  "$AUDIT_DIR/findings" \
  "$AUDIT_DIR/flows" \
  "$AUDIT_DIR/results" \
  "$AUDIT_DIR/artifacts" \
  "$AUDIT_DIR/report"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cat > "$AUDIT_DIR/audit.json" <<EOF
{
  "audit_id": "$AUDIT_ID",
  "pod_id": "$POD_ID",
  "started_at": "$STARTED_AT",
  "audit_dir": "$AUDIT_DIR",
  "config": {
    "quick": false,
    "platform": "both"
  }
}
EOF

touch "$AUDIT_DIR/decisions.log"

echo "Initialized audit at: $AUDIT_DIR" >&2
echo "Pod: $POD_ID" >&2
echo "Started: $STARTED_AT" >&2
echo "$AUDIT_ID"
