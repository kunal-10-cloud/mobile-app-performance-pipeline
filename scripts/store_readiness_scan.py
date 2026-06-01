#!/usr/bin/env python3
"""
Stage 4e — Store publishing-readiness worker.

Reads the workspace's app config + package.json + audit facts, builds an
in-memory source index, runs every rule in `configs/store_rules.py`, and writes
`findings/store.json`.

Phase A only — config + source + file-existence checks. Phase B (HTTP fetches
for AASA / assetlinks / privacy-policy URL) is not yet implemented; the
`--allow-network` flag exists as a no-op placeholder so callers can opt in
without us breaking when it lands.

Emergent customer apps run on Expo SDK 54. Rules treat missing config as
"uses SDK 54 default" rather than "absent" — see EXPO_SDK_54_DEFAULTS in
configs/store_rules.py.

Usage:
  python3 scripts/store_readiness_scan.py <audit_id> [--allow-network]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "configs"))

import store_rules  # noqa: E402
from sdk_disclosure_matrix import detect_disclosures  # noqa: E402


# Files we read into the source index (any file whose body might match a rule
# regex). Keep this scoped — we don't need the whole workspace.
SOURCE_DIRS = ("app", "src", "components", "hooks", "lib", "services", "utils", "screens", "store")
SOURCE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".json")
MAX_FILE_BYTES = 256 * 1024  # skip absurdly large source files


def load_app_config(workspace: Path) -> dict:
    """Load `expo` block from app.json. app.config.{js,ts} evaluation is
    deferred — for Emergent customer apps (SDK 54), app.json is the truth."""
    app_json = workspace / "app.json"
    if app_json.is_file():
        try:
            data = json.loads(app_json.read_text(encoding="utf-8"))
            return data.get("expo") or {}
        except json.JSONDecodeError as e:
            print(f"WARN: app.json parse failed: {e}", file=sys.stderr)
            return {}

    # Best-effort warning when only app.config.* is present — handle in a
    # follow-up slice via `node -e "console.log(JSON.stringify(require('./app.config.js').default))"`.
    for candidate in ("app.config.js", "app.config.ts"):
        if (workspace / candidate).is_file():
            print(
                f"WARN: {candidate} present but evaluation not yet supported; "
                "store readiness will run against an empty config.",
                file=sys.stderr,
            )
            break
    return {}


def load_package_json(workspace: Path) -> dict:
    pj = workspace / "package.json"
    if not pj.is_file():
        return {}
    try:
        return json.loads(pj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def merged_deps(package_json: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = package_json.get(key) or {}
        if isinstance(block, dict):
            out.update({k: v for k, v in block.items() if isinstance(k, str)})
    return out


def load_facts(audit_dir: Path) -> dict:
    candidates: Iterable[Path] = (
        audit_dir / "artifacts" / "audit_facts.json",
        audit_dir / "audit_facts.json",
    )
    for p in candidates:
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    return {}


def load_apk_scan(audit_dir: Path) -> dict:
    p = audit_dir / "artifacts" / "apk_scan.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def build_source_index(workspace: Path) -> dict[str, str]:
    """Build {relative_path: file_text} for SOURCE_DIRS / SOURCE_EXTS files."""
    index: dict[str, str] = {}
    for sub in SOURCE_DIRS:
        root = workspace / sub
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in SOURCE_EXTS:
                    continue
                full = Path(dirpath) / fname
                try:
                    if full.stat().st_size > MAX_FILE_BYTES:
                        continue
                    text = full.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeError):
                    continue
                rel = str(full.relative_to(workspace))
                index[rel] = text
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 4e — store publishing-readiness scan.")
    ap.add_argument("audit_id")
    ap.add_argument(
        "--allow-network",
        action="store_true",
        help="Reserved for Phase B (HTTP fetches for AASA / assetlinks / privacy URL). No-op today.",
    )
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    workspace = audit_dir / "workspace"
    findings_dir = audit_dir / "findings"

    if not workspace.is_dir():
        print(f"ERROR: workspace not found: {workspace}", file=sys.stderr)
        return 2
    findings_dir.mkdir(parents=True, exist_ok=True)

    app_config = load_app_config(workspace)
    package_json = load_package_json(workspace)
    deps = merged_deps(package_json)
    facts = load_facts(audit_dir)
    apk_scan = load_apk_scan(audit_dir)
    source_index = build_source_index(workspace)
    disclosures = detect_disclosures(deps)

    ctx = store_rules.StoreCtx(
        workspace=workspace,
        app_config=app_config,
        package_json=package_json,
        dependencies=deps,
        facts=facts,
        apk_scan=apk_scan,
        source_index=source_index,
        disclosures=disclosures,
    )

    all_findings: list[dict] = []
    rule_errors: list[dict] = []
    for rule_fn in store_rules.ALL_RULES:
        try:
            findings = rule_fn(ctx) or []
        except Exception as e:  # noqa: BLE001 — never fail-closed; log + move on
            rule_errors.append({
                "id": "tooling.store_rule_failed",
                "layer": "tooling",
                "category": "tooling_error",
                "severity": "low",
                "confidence": "high",
                "title": f"Store rule `{rule_fn.__name__}` raised",
                "description": f"{type(e).__name__}: {e}",
                "evidence": {"file": "configs/store_rules.py", "function": rule_fn.__name__},
            })
            continue
        for f in findings:
            f.setdefault("confidence", "high")
        all_findings.extend(findings)

    # Write the store-layer findings file (auto-discovered by aggregate_findings.py).
    out = findings_dir / "store.json"
    out.write_text(json.dumps(all_findings, indent=2), encoding="utf-8")
    print(
        f"store readiness: {len(all_findings)} findings "
        f"({sum(1 for f in all_findings if f['severity'] == 'critical')} critical, "
        f"{sum(1 for f in all_findings if f['severity'] == 'high')} high, "
        f"{sum(1 for f in all_findings if f['severity'] == 'medium')} medium, "
        f"{sum(1 for f in all_findings if f['severity'] == 'low')} low, "
        f"{sum(1 for f in all_findings if f['severity'] == 'info')} info)",
        file=sys.stderr,
    )

    if rule_errors:
        err_out = findings_dir / "store_errors.json"
        err_out.write_text(json.dumps(rule_errors, indent=2), encoding="utf-8")
        print(f"  {len(rule_errors)} rule(s) raised — see {err_out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
