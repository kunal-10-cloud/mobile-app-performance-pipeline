#!/usr/bin/env python3
"""
Stage 4d.4 — Refine the draft Maestro flow into the audit's main flow.

The LLM does NOT emit YAML. It fills the structured `flow_intent` contract
(schemas/flow_intent.schema.json); this script then validates and renders YAML
deterministically. This split kills two failure modes the legacy approach had:
  - LLM-emitted YAML with invented element selectors that don't exist on screen.
  - LLM-emitted YAML with subtle indentation bugs that Maestro silently mis-parses.

Inputs the LLM sees:
  - flows/screen_map.json
  - flows/draft_intent.json
  - sources of: the detected login screen (if any), and the first tab's screen
  - prompts/refine_flow.md

LLM response: a single JSON object matching flow_intent schema. Validation is
strict; on schema mismatch we keep the draft intent and write a tooling.* finding.

This script is INVOKED VIA THE CALLING SESSION'S LLM. It does not itself open
network connections. SKILL.md Step 4d.4 hands the inputs to the calling LLM
(e.g. Claude in the host session), which then writes its response to
`flows/refined_intent.json` and re-invokes this script in `--render` mode to
produce the final YAML.

Two modes:
  --prepare   bundle the LLM inputs as flows/refine_inputs.json
              (the SKILL prompts the LLM to consume these + the prompt file)
  --render    consume flows/refined_intent.json (written by the LLM) and
              produce flows/main.yaml + flows/refined_intent_validated.json
              (and finding diagnostics)

Usage:
  python3 scripts/refine_flow_with_llm.py <audit_id> --prepare
  python3 scripts/refine_flow_with_llm.py <audit_id> --render
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from render_flow_yaml import render_flow, _validate as validate_intent  # noqa: E402

PROMPT_PATH = REPO_ROOT / "prompts" / "refine_flow.md"


def prepare(audit_id: str) -> int:
    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / audit_id
    workspace = audit_dir / "workspace"
    flows_dir = audit_dir / "flows"
    screen_map_path = flows_dir / "screen_map.json"
    draft_intent_path = flows_dir / "draft_intent.json"

    if not screen_map_path.is_file() or not draft_intent_path.is_file():
        print("ERROR: screen_map.json or draft_intent.json missing — run prior 4d sub-stages first.", file=sys.stderr)
        return 2

    screen_map = json.loads(screen_map_path.read_text(encoding="utf-8"))
    draft_intent = json.loads(draft_intent_path.read_text(encoding="utf-8"))

    # Selective source bundling: login screen source + first tab source.
    bundled_sources: dict[str, str] = {}

    auth_path = (screen_map.get("auth") or {}).get("login_screen")
    if auth_path:
        p = workspace / auth_path
        if p.is_file():
            bundled_sources[auth_path] = _truncated_source(p)

    tabs = (screen_map.get("navigation") or {}).get("tabs") or []
    if tabs:
        first_tab_file = tabs[0].get("file")
        if first_tab_file:
            p = workspace / first_tab_file
            if p.is_file():
                bundled_sources[first_tab_file] = _truncated_source(p)

    prompt = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.is_file() else ""

    inputs = {
        "audit_id": audit_id,
        "prompt_path": str(PROMPT_PATH),
        "prompt_text_preview_first_lines": "\n".join(prompt.splitlines()[:40]),
        "screen_map": screen_map,
        "draft_intent": draft_intent,
        "screen_sources": bundled_sources,
        "expected_response_schema_path": "schemas/flow_intent.schema.json",
        "response_target_path": str(flows_dir / "refined_intent.json"),
        "post_action": "python3 scripts/refine_flow_with_llm.py " + audit_id + " --render",
    }
    out = flows_dir / "refine_inputs.json"
    out.write_text(json.dumps(inputs, indent=2), encoding="utf-8")
    print(f"Refine inputs prepared → {out}", file=sys.stderr)
    print(f"  next: LLM reads this + prompts/refine_flow.md, writes {flows_dir / 'refined_intent.json'}, then re-runs with --render", file=sys.stderr)
    return 0


def render(audit_id: str) -> int:
    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / audit_id
    flows_dir = audit_dir / "flows"
    findings_dir = audit_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    refined_path = flows_dir / "refined_intent.json"
    draft_intent_path = flows_dir / "draft_intent.json"

    if not refined_path.is_file():
        _write_finding(
            findings_dir / "flow_refine.json",
            id="tooling.flow_refine_missing",
            title="LLM-refined flow intent missing",
            description=(
                "Expected flows/refined_intent.json from the LLM refinement step. "
                "Falling back to draft_intent.json without any login or per-screen interaction polish."
            ),
        )
        # Fallback — re-render the draft as main.yaml so subsequent stages have something to run.
        if draft_intent_path.is_file():
            intent = json.loads(draft_intent_path.read_text(encoding="utf-8"))
            (flows_dir / "main.yaml").write_text(render_flow(intent, platform="android"), encoding="utf-8")
        return 0

    try:
        intent = json.loads(refined_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _write_finding(
            findings_dir / "flow_refine.json",
            id="tooling.flow_refine_json_parse",
            title="LLM-refined flow intent is not valid JSON",
            description=f"JSONDecodeError: {e}. Falling back to draft_intent.json.",
        )
        if draft_intent_path.is_file():
            intent = json.loads(draft_intent_path.read_text(encoding="utf-8"))
            (flows_dir / "main.yaml").write_text(render_flow(intent, platform="android"), encoding="utf-8")
        return 0

    errs = validate_intent(intent)
    if errs:
        _write_finding(
            findings_dir / "flow_refine.json",
            id="tooling.flow_refine_schema_violation",
            title="LLM-refined flow intent failed schema validation",
            description=("Schema violations: " + "; ".join(errs[:6]) + ("; ..." if len(errs) > 6 else "")),
            severity="low",
        )
        if draft_intent_path.is_file():
            intent = json.loads(draft_intent_path.read_text(encoding="utf-8"))

    # Stamp metadata if missing.
    md = intent.setdefault("metadata", {})
    md.setdefault("source", "llm_refined")
    md["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Persist the validated intent + render Android YAML as the canonical flow.
    (flows_dir / "refined_intent_validated.json").write_text(json.dumps(intent, indent=2), encoding="utf-8")
    yaml = render_flow(intent, platform="android")
    (flows_dir / "main.yaml").write_text(yaml, encoding="utf-8")

    # Empty / OK findings file so aggregate_findings.py picks up zero diagnostics.
    if not (findings_dir / "flow_refine.json").is_file():
        (findings_dir / "flow_refine.json").write_text("[]", encoding="utf-8")
    print(f"Refined flow rendered → {flows_dir / 'main.yaml'}", file=sys.stderr)
    return 0


def _truncated_source(p: Path, max_bytes: int = 8000) -> str:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"<read failed: {e}>"
    if len(text) <= max_bytes:
        return text
    return text[:max_bytes] + f"\n/* ... truncated {len(text) - max_bytes} bytes ... */"


def _write_finding(path: Path, *, id: str, title: str, description: str, severity: str = "low") -> None:
    arr = [{
        "id": id,
        "layer": "tooling",
        "category": "tooling_error",
        "severity": severity,
        "confidence": "high",
        "title": title,
        "description": description,
        "evidence": {"file": str(path.parent.parent / "flows")},
    }]
    path.write_text(json.dumps(arr, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare LLM inputs for / render LLM-refined Maestro flow.")
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
