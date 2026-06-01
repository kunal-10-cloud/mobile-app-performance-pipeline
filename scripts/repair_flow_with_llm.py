#!/usr/bin/env python3
"""
Stage 4d.6 — Repair a failing Maestro flow.

Triggered when validate_flow.sh reports any non-optional step that failed.
The repair runs ONCE. If validation still fails after, we accept partial
coverage and log a flow_partial_coverage finding instead of looping.

Like refine_flow_with_llm.py, the LLM operates on the structured
flow_intent JSON, not raw YAML — see schemas/flow_intent.schema.json.

Inputs the LLM sees:
  - flows/refined_intent_validated.json (or draft_intent.json if no refine ran)
  - flows/validation.json               (per-step results from Maestro)
  - flows/debug/                        (UI dumps captured at failure points)
  - prompts/repair_flow.md

LLM response: a patched flow_intent JSON saved to flows/repaired_intent.json.

Modes:
  --prepare    bundle inputs for the LLM
  --render     consume repaired_intent.json → main.yaml

Usage:
  python3 scripts/repair_flow_with_llm.py <audit_id> --prepare
  python3 scripts/repair_flow_with_llm.py <audit_id> --render
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from render_flow_yaml import render_flow, _validate as validate_intent  # noqa: E402

PROMPT_PATH = REPO_ROOT / "prompts" / "repair_flow.md"


def _read_validation(flows_dir: Path) -> dict:
    p = flows_dir / "validation.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _bundle_debug_dumps(flows_dir: Path, max_bytes_per_file: int = 4000, max_files: int = 6) -> dict[str, str]:
    debug_dir = flows_dir / "debug"
    if not debug_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    count = 0
    for p in sorted(debug_dir.rglob("*.xml")):
        if count >= max_files:
            break
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(text) > max_bytes_per_file:
            text = text[:max_bytes_per_file] + f"\n<!-- truncated {len(text) - max_bytes_per_file} bytes -->"
        out[str(p.relative_to(flows_dir))] = text
        count += 1
    return out


def prepare(audit_id: str) -> int:
    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / audit_id
    flows_dir = audit_dir / "flows"
    refined = flows_dir / "refined_intent_validated.json"
    draft = flows_dir / "draft_intent.json"
    current_intent_path = refined if refined.is_file() else draft

    if not current_intent_path.is_file():
        print("ERROR: no flow intent to repair (neither refined nor draft).", file=sys.stderr)
        return 2

    validation = _read_validation(flows_dir)
    if not validation:
        print("ERROR: flows/validation.json missing or empty — repair requires validator output.", file=sys.stderr)
        return 2

    inputs = {
        "audit_id": audit_id,
        "prompt_path": str(PROMPT_PATH),
        "current_intent_path": str(current_intent_path),
        "current_intent": json.loads(current_intent_path.read_text(encoding="utf-8")),
        "validation_results": validation,
        "ui_dumps": _bundle_debug_dumps(flows_dir),
        "expected_response_schema_path": "schemas/flow_intent.schema.json",
        "response_target_path": str(flows_dir / "repaired_intent.json"),
        "post_action": "python3 scripts/repair_flow_with_llm.py " + audit_id + " --render",
    }
    out = flows_dir / "repair_inputs.json"
    out.write_text(json.dumps(inputs, indent=2), encoding="utf-8")
    print(f"Repair inputs prepared → {out}", file=sys.stderr)
    return 0


def render(audit_id: str) -> int:
    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / audit_id
    flows_dir = audit_dir / "flows"
    findings_dir = audit_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    repaired = flows_dir / "repaired_intent.json"
    if not repaired.is_file():
        _write_finding(
            findings_dir / "flow_repair.json",
            id="tooling.flow_repair_missing",
            title="LLM-repaired flow intent missing",
            description=(
                "Validation reported failures and the repair step was triggered, but the LLM did not produce "
                "flows/repaired_intent.json. Continuing with the previous intent — Maestro steps that failed will "
                "remain optional and the run will yield partial coverage."
            ),
        )
        return 0

    try:
        intent = json.loads(repaired.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _write_finding(
            findings_dir / "flow_repair.json",
            id="tooling.flow_repair_json_parse",
            title="LLM-repaired flow intent is not valid JSON",
            description=f"JSONDecodeError: {e}. Continuing with the previous main.yaml.",
        )
        return 0

    errs = validate_intent(intent)
    if errs:
        _write_finding(
            findings_dir / "flow_repair.json",
            id="tooling.flow_repair_schema_violation",
            title="LLM-repaired flow intent failed schema validation",
            description=("Schema violations: " + "; ".join(errs[:6]) + ("; ..." if len(errs) > 6 else "")),
        )
        return 0

    intent.setdefault("metadata", {})["source"] = "llm_repaired"
    intent["metadata"]["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (flows_dir / "main.yaml").write_text(render_flow(intent, platform="android"), encoding="utf-8")
    (flows_dir / "repaired_intent_validated.json").write_text(json.dumps(intent, indent=2), encoding="utf-8")

    # Empty findings file = no diagnostics from the repair step itself.
    (findings_dir / "flow_repair.json").write_text("[]", encoding="utf-8")
    print(f"Repaired flow rendered → {flows_dir / 'main.yaml'}", file=sys.stderr)
    return 0


def _write_finding(path: Path, *, id: str, title: str, description: str) -> None:
    arr = [{
        "id": id,
        "layer": "tooling",
        "category": "tooling_error",
        "severity": "low",
        "confidence": "high",
        "title": title,
        "description": description,
        "evidence": {"file": str(path.parent.parent / "flows")},
    }]
    path.write_text(json.dumps(arr, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare LLM inputs for / render LLM-repaired Maestro flow.")
    ap.add_argument("audit_id")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--render", action="store_true")
    args = ap.parse_args()

    if args.prepare:
        return prepare(args.audit_id)
    return render(args.audit_id)


if __name__ == "__main__":
    sys.exit(main())
