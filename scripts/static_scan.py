#!/usr/bin/env python3
"""
Stage 4a — Static scan.

Runs two static-analysis engines against the workspace:

  1. ESLint with configs/eslint.perf.config.js  (perf-focused rules from
     react-hooks, react-perf, react-native plugins)
  2. configs/ast_rules.py                       (custom tree-sitter rules
     for patterns ESLint can't catch — ScrollView+map, RN Image with URI,
     etc.)

Output:
  ${AUDIT_DIR}/findings/static.json  — array of Findings matching
                                       schemas/finding.schema.json.

Each ESLint hit is transformed into a Finding with id "static.eslint.<rule-id>".
Each custom rule emits Findings with its own id (e.g. "static.scrollview_with_long_list").

Per-file parse failures or per-rule exceptions are captured as tooling-error
Findings; the scan does not abort.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "configs"))
import ast_rules  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# AST-rule pass — walk source files, apply registered rules
# ──────────────────────────────────────────────────────────────────────────────

SKIP_DIRS = {"node_modules", ".expo", ".git", "build", "dist", "ios", "android", "__tests__", ".turbo"}
SOURCE_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")


def iter_source_files(workspace: Path):
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(SOURCE_EXTENSIONS):
                yield Path(root) / f


def run_ast_pass(workspace: Path) -> list[dict]:
    findings: list[dict] = []
    files_scanned = 0
    for path in iter_source_files(workspace):
        rel = str(path.relative_to(workspace))
        try:
            source = path.read_bytes()
        except Exception as e:
            findings.append({
                "id": "tooling.file_read_failed",
                "layer": "tooling",
                "category": "tooling_error",
                "severity": "low",
                "confidence": "high",
                "title": f"Failed to read {rel}",
                "description": f"{type(e).__name__}: {e}",
                "evidence": {"file": rel},
            })
            continue
        files_scanned += 1
        findings.extend(ast_rules.run_all_rules(rel, source))
    print(f"AST pass: scanned {files_scanned} source files, emitted {len(findings)} findings.", file=sys.stderr)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# ESLint pass
# ──────────────────────────────────────────────────────────────────────────────

# Map ESLint severity → finding severity (rough but reasonable)
ESLINT_SEVERITY_MAP = {
    1: "low",       # warning
    2: "medium",    # error
}

# Map ESLint rule IDs to (category, default_severity_override, default_confidence)
# When unmapped: category defaults to "code_quality", severity to ESLINT_SEVERITY_MAP.
ESLINT_RULE_META: dict[str, tuple[str, str | None, str]] = {
    "react-hooks/exhaustive-deps":      ("runtime_jank", "high", "high"),
    "react-hooks/rules-of-hooks":       ("runtime_jank", "critical", "high"),
    "react-perf/jsx-no-new-object-as-prop":   ("runtime_jank", "medium", "high"),
    "react-perf/jsx-no-new-array-as-prop":    ("runtime_jank", "medium", "high"),
    "react-perf/jsx-no-new-function-as-prop": ("runtime_jank", "high", "high"),
    "react-perf/jsx-no-jsx-as-prop":          ("runtime_jank", "low", "medium"),
    "react-native/no-inline-styles":          ("runtime_jank", "low", "medium"),
    "react-native/no-unused-styles":          ("bundle_size", "low", "medium"),
    "react/jsx-key":                          ("runtime_jank", "high", "high"),
    "react/no-array-index-key":               ("runtime_jank", "medium", "medium"),
}


def run_eslint_pass(workspace: Path, config_path: Path) -> list[dict]:
    """Run ESLint via npx. Returns Findings list. On total failure (no install,
    no plugins) returns a single tooling-error finding instead of an exception."""
    if not config_path.exists():
        return [{
            "id": "tooling.eslint_config_missing",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "ESLint config not found",
            "description": f"Expected {config_path}; ESLint pass was skipped.",
            "evidence": {"file": str(config_path)},
        }]

    cmd = [
        "npx", "--no-install", "eslint",
        "--config", str(config_path),
        "--format", "json",
        "--resolve-plugins-relative-to", str(workspace),
        # ESLint exits non-zero when issues found; we capture stdout regardless.
        str(workspace),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return [{
            "id": "tooling.eslint_unavailable",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "ESLint not available",
            "description": "npx eslint could not run. Static AST rules still ran via tree-sitter.",
            "evidence": {"file": str(workspace)},
        }]
    except subprocess.TimeoutExpired:
        return [{
            "id": "tooling.eslint_timeout",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "ESLint timed out",
            "description": "ESLint did not finish within 10 minutes.",
            "evidence": {"file": str(workspace)},
        }]

    if not r.stdout.strip():
        return [{
            "id": "tooling.eslint_no_output",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "ESLint produced no output",
            "description": (r.stderr or "")[:400],
            "evidence": {"file": str(workspace)},
        }]

    try:
        eslint_data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return [{
            "id": "tooling.eslint_json_parse",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "Could not parse ESLint output",
            "description": f"JSONDecodeError: {e}. First 400 chars: {r.stdout[:400]}",
            "evidence": {"file": str(workspace)},
        }]

    findings: list[dict] = []
    for file_result in eslint_data:
        rel = os.path.relpath(file_result.get("filePath", ""), workspace)
        for msg in file_result.get("messages", []):
            rule_id = msg.get("ruleId") or "unknown"
            es_severity = msg.get("severity", 1)
            cat, sev_override, conf = ESLINT_RULE_META.get(rule_id, ("code_quality", None, "medium"))
            severity = sev_override or ESLINT_SEVERITY_MAP.get(es_severity, "low")
            findings.append({
                "id": f"static.eslint.{rule_id.replace('/', '__')}",
                "layer": "static",
                "category": cat,
                "severity": severity,
                "confidence": conf,
                "title": msg.get("message", rule_id)[:120],
                "description": f"ESLint rule `{rule_id}` reported: {msg.get('message','')}",
                "evidence": {
                    "file": rel,
                    "line": msg.get("line", 0),
                    "code_snippet": msg.get("source") or "",
                },
            })
    print(f"ESLint pass: {len(findings)} findings.", file=sys.stderr)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Run static analysis (ESLint + custom AST rules).")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    workspace = audit_dir / "workspace"
    findings_dir = audit_dir / "findings"
    config = REPO_ROOT / "configs" / "eslint.perf.config.js"

    if not workspace.is_dir():
        print(f"ERROR: workspace not found: {workspace}", file=sys.stderr)
        return 2
    findings_dir.mkdir(parents=True, exist_ok=True)

    all_findings: list[dict] = []
    all_findings.extend(run_ast_pass(workspace))
    all_findings.extend(run_eslint_pass(workspace, config))

    out = findings_dir / "static.json"
    out.write_text(json.dumps(all_findings, indent=2), encoding="utf-8")
    print(f"wrote {out} ({len(all_findings)} findings)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
