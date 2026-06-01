#!/usr/bin/env python3
"""
Stage 4a-bis — Config scan.

Consumes `audit_facts.json` and emits Findings for project-configuration
issues that aren't AST-detectable in source. These are deterministic, single-
shot checks (no source iteration); each one corresponds to a boolean fact.

Today this script emits:
  - static.hermes_disabled        — Hermes engine off (or unset on old SDKs)
  - static.new_architecture_disabled — Fabric/TurboModules off on SDK ≥ 51

Output:
  ${AUDIT_DIR}/findings/config.json — array of Findings matching
                                      schemas/finding.schema.json.

The script is fail-soft: if facts are missing, it emits one tooling.* finding
and exits 0 so the pipeline keeps moving.

Usage:
  python3 scripts/config_scan.py <audit_id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _facts_get(facts: dict, path: list[str], default=None):
    cur = facts
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _expo_sdk_int(facts: dict) -> int | None:
    raw = _facts_get(facts, ["project_signature", "expo_sdk_version"])
    if raw is None:
        return None
    s = str(raw).strip()
    # tolerate "51", "51.0.0", "~51.0.0"
    for prefix in ("~", "^", ">=", "<=", ">", "<", "="):
        if s.startswith(prefix):
            s = s[len(prefix):]
    try:
        return int(s.split(".")[0])
    except (ValueError, IndexError):
        return None


def check_hermes(facts: dict) -> list[dict]:
    hermes = _facts_get(facts, ["project_signature", "hermes_enabled"])
    sdk = _expo_sdk_int(facts)

    # On SDK 50+, Hermes is the default. `hermes_enabled == True` (default or
    # explicit) → no finding. `False` means explicitly opted into JSC.
    # On SDK ≤ 49, Hermes was opt-in. `null` / missing = default JSC.
    explicit_off = hermes is False
    implicit_off_on_old_sdk = hermes is None and sdk is not None and sdk <= 49

    if not (explicit_off or implicit_off_on_old_sdk):
        return []

    severity = "critical" if explicit_off else "high"
    sdk_str = str(sdk) if sdk is not None else "unknown"
    return [{
        "id": "static.hermes_disabled",
        "layer": "static",
        "category": "startup",
        "severity": severity,
        "confidence": "high",
        "title": "Hermes engine is disabled",
        "description": (
            "Hermes is React Native's optimised JS engine. It precompiles to bytecode and "
            "shaves 30–50% off cold-start time relative to JavaScriptCore. "
            f"This project is on SDK {sdk_str} with Hermes "
            f"{'explicitly disabled (jsEngine: jsc)' if explicit_off else 'not opted in (default JSC on this SDK)'}. "
            "Enable in app.json: `\"jsEngine\": \"hermes\"`."
        ),
        "evidence": {
            "file": "app.json",
            "function": "<config>",
            "metric_name": "hermes_enabled",
            "metric_value": 0,
            "metric_threshold": 1,
        },
    }]


def check_new_architecture(facts: dict) -> list[dict]:
    new_arch = _facts_get(facts, ["project_signature", "new_architecture_enabled"])
    sdk = _expo_sdk_int(facts)

    # New Architecture is recommended on SDK 51+. Below that, opting in is
    # risky (library compatibility); we don't flag.
    if sdk is None or sdk < 51:
        return []
    if new_arch is True:
        return []

    return [{
        "id": "static.new_architecture_disabled",
        "layer": "static",
        "category": "runtime_jank",
        "severity": "medium",
        "confidence": "high",
        "title": "New Architecture (Fabric + TurboModules) is not enabled",
        "description": (
            f"Project is on Expo SDK {sdk}, which supports the New Architecture stably. "
            "Fabric removes the JS↔native bridge for component updates, and TurboModules "
            "enable synchronous JS↔native calls. The change is opt-in via "
            "`expo.newArchEnabled: true` in app.json. "
            "Verify each third-party native module is New-Arch-compatible before flipping."
        ),
        "evidence": {
            "file": "app.json",
            "function": "<config>",
            "metric_name": "new_architecture_enabled",
            "metric_value": 0,
            "metric_threshold": 1,
        },
    }]


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan project config for perf-relevant settings.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    facts_path = audit_dir / "facts" / "audit_facts.json"
    findings_dir = audit_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    out: list[dict] = []

    if not facts_path.is_file():
        out.append({
            "id": "tooling.facts_unavailable",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "Facts file missing — config checks skipped",
            "description": f"Expected {facts_path}; gather_facts.py likely did not run successfully.",
            "evidence": {"file": str(facts_path)},
        })
    else:
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
        except Exception as e:
            facts = {}
            out.append({
                "id": "tooling.facts_parse_failed",
                "layer": "tooling",
                "category": "tooling_error",
                "severity": "low",
                "confidence": "high",
                "title": "Facts file could not be parsed",
                "description": f"{type(e).__name__}: {e}",
                "evidence": {"file": str(facts_path)},
            })
        out.extend(check_hermes(facts))
        out.extend(check_new_architecture(facts))

    target = findings_dir / "config.json"
    target.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Config scan: {len(out)} findings → {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
