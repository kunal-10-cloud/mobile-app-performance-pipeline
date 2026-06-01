#!/usr/bin/env python3
"""
Stage 5 — Aggregate findings.

Loads every ${AUDIT_DIR}/findings/*.json file (except the aggregate output
itself), validates each Finding against schemas/finding.schema.json,
concatenates them into findings/all_findings.json.

Fails LOUDLY on schema mismatch — that's a worker bug we want to catch
before Pass A runs.

Usage:
  python3 scripts/aggregate_findings.py <audit_id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "finding.schema.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="Aggregate per-worker finding files into one validated array.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    findings_dir = base / args.audit_id / "findings"
    if not findings_dir.is_dir():
        print(f"ERROR: findings dir not found: {findings_dir}", file=sys.stderr)
        return 2

    # Schema validation is optional — if jsonschema isn't installed, we still
    # aggregate but skip validation with a warning.
    validator = None
    try:
        from jsonschema import Draft7Validator
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft7Validator(schema)
    except ImportError:
        print("WARN: jsonschema not installed — skipping schema validation.", file=sys.stderr)
    except Exception as e:
        print(f"WARN: could not load schema ({e}); skipping schema validation.", file=sys.stderr)

    all_findings: list[dict] = []
    file_count = 0
    validation_errors: list[str] = []

    for path in sorted(findings_dir.glob("*.json")):
        if path.name == "all_findings.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"ERROR: invalid JSON in {path}: {e}", file=sys.stderr)
            return 1
        if not isinstance(data, list):
            print(f"ERROR: {path} must contain a JSON array of Findings; got {type(data).__name__}", file=sys.stderr)
            return 1

        file_count += 1
        for idx, item in enumerate(data):
            if validator is not None:
                for err in validator.iter_errors(item):
                    validation_errors.append(f"{path.name}[{idx}] id={item.get('id','?')}: {err.message} at {list(err.path)}")
            all_findings.append(item)

    if validation_errors:
        print(f"ERROR: {len(validation_errors)} schema violation(s):", file=sys.stderr)
        for e in validation_errors[:25]:
            print(f"  - {e}", file=sys.stderr)
        if len(validation_errors) > 25:
            print(f"  ... and {len(validation_errors) - 25} more", file=sys.stderr)
        return 1

    out = findings_dir / "all_findings.json"
    out.write_text(json.dumps(all_findings, indent=2), encoding="utf-8")
    print(f"Aggregated {len(all_findings)} findings from {file_count} worker file(s) into {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
