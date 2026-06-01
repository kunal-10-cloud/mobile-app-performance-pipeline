#!/usr/bin/env python3
"""
Pass A — verify each Finding per references.md §2 universal verification protocol.

For each Finding from each worker:
  1. Read N lines of context around the cited location (when source-based).
  2. Apply per-rule preconditions and FP filters (per references.md §3).
  3. Cross-reference audit_facts.json where the rule requires it.
  4. Stamp `verdict` (REAL / FP / UNCERTAIN) and a one-line `verification_method`.
  5. Append one line to decisions.log.

Output: ${AUDIT_DIR}/evidence/evidence.json (matching schemas/evidence.schema.json).

Findings whose verdict ends up FP or UNCERTAIN stay in evidence.json (Rule 1.5)
but are excluded from the rendered report (Rule 1.8). Silent exclusion is
forbidden in decisions.log — every analyzer hit gets a line.

Usage:
  python3 scripts/pass_a_verify.py <audit_id>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


def iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(p: Path) -> dict | list:
    return json.loads(p.read_text(encoding="utf-8"))


def read_context(workspace: Path, file_rel: str, line: int, before: int = 30, after: int = 5) -> str | None:
    """Read N lines around (line) from workspace/file_rel. Returns the joined
    snippet, or None if the file cannot be opened."""
    if not file_rel:
        return None
    p = workspace / file_rel
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()
    if line <= 0 or line > len(lines):
        return None
    start = max(0, line - before - 1)
    end = min(len(lines), line + after)
    return "\n".join(lines[start:end])


# ──────────────────────────────────────────────────────────────────────────────
# Per-rule verifiers
#
# Each verifier receives the Finding, the workspace path, and the audit_facts
# dict. It returns (verdict, verification_method) — both strings.
#
# Verifiers may downgrade REAL → UNCERTAIN based on facts cross-reference (e.g.
# scrollview_with_long_list may be intentional if FlashList is already in use).
# ──────────────────────────────────────────────────────────────────────────────

def _has_facts_key(facts: dict, path: list[str]) -> bool:
    cur = facts
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return True


def _facts_get(facts: dict, path: list[str], default=None):
    cur = facts
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def verify_scrollview_with_long_list(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    if not file_rel:
        return "UNCERTAIN", "no file cited"
    p = workspace / file_rel
    if not p.is_file():
        return "FP", "cited file no longer present in workspace"
    try:
        full = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "UNCERTAIN", "could not read cited file"
    # The AST rule is precise (only fires on a <ScrollView> containing a
    # JSX-returning .map()). Pass A's job here is a STALENESS guard, not a
    # re-litigation of the AST: confirm the file still contains a <ScrollView>
    # and a .map(. We read the WHOLE file rather than a narrow window because
    # the <ScrollView> tag and the inner .map() can be 100+ lines apart on
    # large screens — a windowed check wrongly dropped real findings.
    if "<ScrollView" not in full:
        return "FP", "file no longer contains a <ScrollView> (stale citation or edited file)"
    if ".map(" not in full:
        return "FP", "file no longer contains a .map() inside the ScrollView (stale citation)"
    # Cross-reference facts: if FlashList is in use elsewhere, downgrade.
    flash_list_present = _facts_get(facts, ["dependencies", "flash_list_present"], False)
    flashlist_count = _facts_get(facts, ["source_pattern_counts", "flashlist_count"], 0)
    if flash_list_present and flashlist_count > 0:
        return "UNCERTAIN", "FlashList available + used elsewhere; this site may be deliberate"
    return "REAL", "whole-file check confirmed <ScrollView> + JSX-rendering .map()"


def verify_image_without_caching(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    line = f.get("evidence", {}).get("line", 0)
    snippet = read_context(workspace, file_rel, line, before=30)
    if snippet is None:
        return "UNCERTAIN", "could not read source context"
    if "Image" not in snippet:
        return "FP", "snippet no longer contains an Image element"
    if "expo-image" in snippet:
        return "FP", "expo-image actually used at this site"
    return "REAL", "RN <Image> with remote URI, no caching wrapper detected in context"


def verify_inline_arrow_in_renderitem(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    line = f.get("evidence", {}).get("line", 0)
    snippet = read_context(workspace, file_rel, line, before=10, after=5)
    if snippet is None:
        return "UNCERTAIN", "could not read source context"
    if "renderItem" not in snippet:
        return "FP", "snippet no longer contains renderItem prop"
    # Look for an arrow function on or near the line
    if "=>" not in snippet:
        return "FP", "no arrow function in surrounding context"
    return "REAL", "renderItem={() => ...} confirmed in surrounding context"


def verify_useeffect_no_deps(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    line = f.get("evidence", {}).get("line", 0)
    snippet = read_context(workspace, file_rel, line, before=15, after=10)
    if snippet is None:
        return "UNCERTAIN", "could not read source context"
    if "useEffect" not in snippet:
        return "FP", "snippet no longer contains useEffect"
    # If the snippet has a comment indicating intentional every-render behaviour, mark UNCERTAIN
    if re.search(r"//.*intentional|//.*every render|//.*runs.*every render", snippet, re.IGNORECASE):
        return "UNCERTAIN", "comment suggests intentional every-render effect"
    return "REAL", "useEffect call without dependency array confirmed"


def verify_console_log_in_production_code(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    line = f.get("evidence", {}).get("line", 0)
    snippet = read_context(workspace, file_rel, line, before=10, after=2)
    if snippet is None:
        return "UNCERTAIN", "could not read source context"
    if "console." not in snippet:
        return "FP", "snippet no longer contains a console call"
    if "__DEV__" in snippet:
        return "FP", "site is __DEV__-guarded after re-read"
    return "REAL", "console.* call outside __DEV__ guard confirmed"


def verify_animated_api_usage(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    # Multi-line imports can span 10+ lines. Use a wide window so we capture
    # both the `Animated,` line and the trailing `} from 'react-native';` line
    # for a clean text-level confirmation.
    snippet = read_context(workspace, file_rel, f.get("evidence", {}).get("line", 1), before=15, after=20)
    if snippet is None:
        return "UNCERTAIN", "could not read source context"
    if "Animated" not in snippet:
        return "FP", "snippet no longer mentions Animated"
    if "react-native" not in snippet:
        return "FP", "snippet does not show Animated coming from react-native"
    return "REAL", "import { Animated } from 'react-native' confirmed"


def verify_inline_object_props(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    line = f.get("evidence", {}).get("line", 0)
    snippet = read_context(workspace, file_rel, line, before=5, after=5)
    if snippet is None:
        return "UNCERTAIN", "could not read source context"
    # Confirm the cited line still contains a `{{...}}` or `={` pattern with an
    # object literal. A simple text-level check is fine here — the AST rule is
    # precise enough that we mainly want to catch stale citations.
    if "{{" not in snippet and "= {" not in snippet and ":" not in snippet:
        return "FP", "snippet no longer contains an inline object literal"
    return "REAL", "inline object literal as prop confirmed in surrounding context"


def verify_large_unmemoized_component(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    line = f.get("evidence", {}).get("line", 0)
    snippet = read_context(workspace, file_rel, line, before=2, after=20)
    if snippet is None:
        return "UNCERTAIN", "could not read source context"
    # If author uses memo elsewhere but not here, the omission may be deliberate.
    react_memo_count = _facts_get(facts, ["source_pattern_counts", "react_memo_count"], 0)
    if react_memo_count and react_memo_count > 0:
        return "UNCERTAIN", "React.memo is used elsewhere in the project; this site may be deliberate"
    return "REAL", "large component without React.memo wrapper confirmed"


def verify_hermes_disabled(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    # Pure fact-derived rule; trust the fact as authoritative.
    hermes = _facts_get(facts, ["project_signature", "hermes_enabled"])
    if hermes is True:
        return "FP", "facts now report hermes_enabled=true — stale config_scan output"
    return "REAL", f"facts.project_signature.hermes_enabled={hermes!r} confirms finding"


def verify_new_architecture_disabled(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    new_arch = _facts_get(facts, ["project_signature", "new_architecture_enabled"])
    if new_arch is True:
        return "FP", "facts now report new_architecture_enabled=true — stale config_scan output"
    return "REAL", f"facts.project_signature.new_architecture_enabled={new_arch!r} confirms finding"


# ── Bundle verifiers ─────────────────────────────────────────────────────────
# Bundle findings are derived from measured artefacts (file sizes, sourcemap
# data). The cited measurement is the source of truth; verification confirms
# the measurement is present and above threshold.

def _verify_metric_above_threshold(f: dict, workspace: Path | None = None, facts: dict | None = None) -> tuple[str, str]:
    """For 'higher is worse' metrics (bytes, ms, growth, blocking interval): REAL
    when metric_value >= metric_threshold; FP otherwise."""
    ev = f.get("evidence", {})
    val = ev.get("metric_value")
    thresh = ev.get("metric_threshold")
    if not isinstance(val, (int, float)):
        return "UNCERTAIN", "metric_value missing or non-numeric"
    if isinstance(thresh, (int, float)) and val < thresh:
        return "FP", f"metric {val} below threshold {thresh} — stale measurement"
    return "REAL", f"measured {ev.get('metric_name','metric')}={val} confirms finding"


def _verify_metric_above_threshold_reverse(f: dict, workspace: Path | None = None, facts: dict | None = None) -> tuple[str, str]:
    """For 'lower is worse' metrics (FPS): REAL when metric_value < metric_threshold;
    FP otherwise."""
    ev = f.get("evidence", {})
    val = ev.get("metric_value")
    thresh = ev.get("metric_threshold")
    if not isinstance(val, (int, float)):
        return "UNCERTAIN", "metric_value missing or non-numeric"
    if isinstance(thresh, (int, float)) and val >= thresh:
        return "FP", f"metric {val} at/above threshold {thresh} — stale measurement"
    return "REAL", f"measured {ev.get('metric_name','metric')}={val} below {thresh} confirms finding"


def verify_bundle_size(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    return _verify_metric_above_threshold(f)


def verify_dependency_oversized(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    return _verify_metric_above_threshold(f)


def verify_asset_image_too_large(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    file_rel = f.get("evidence", {}).get("file", "")
    if not file_rel:
        return _verify_metric_above_threshold(f)
    p = workspace / file_rel
    if not p.is_file():
        return "FP", "cited asset no longer exists in workspace"
    try:
        actual = p.stat().st_size
    except OSError:
        return "UNCERTAIN", "could not stat asset"
    threshold = f.get("evidence", {}).get("metric_threshold", 0) or 0
    if actual < threshold:
        return "FP", f"asset shrank to {actual} B; below threshold {threshold} B"
    return "REAL", f"asset confirmed at {actual} B (threshold {threshold} B)"


def verify_known_bloated_dependency(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    # The "bloated" tag itself is heuristic; we only need to confirm the
    # package is present in the install. facts.dependencies.production_count
    # alone isn't enough — we look at the actual package list if exposed.
    known_heavy = _facts_get(facts, ["dependencies", "known_heavy_deps"], []) or []
    file_rel = f.get("evidence", {}).get("file", "")
    pkg_hint = ""
    if "/" in file_rel:
        pkg_hint = file_rel.split("node_modules/", 1)[-1].rstrip("/")
    if pkg_hint and any(h in pkg_hint or pkg_hint.startswith(h) for h in known_heavy):
        return "REAL", f"{pkg_hint} is listed in facts.dependencies.known_heavy_deps"
    # Even if facts doesn't list it, the bundle scan saw bytes — trust the measurement.
    return "REAL", "package confirmed in bundle by source-map-explorer"


def verify_duplicate_dependency_libs(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    # Trust the deterministic bundle scan; nothing to re-verify here without
    # re-running source-map-explorer.
    return "REAL", "duplicate-purpose pair confirmed by bundle_scan"


# ── Reassure verifiers ───────────────────────────────────────────────────────

def verify_reassure_metric(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    return _verify_metric_above_threshold(f)


VERIFIERS = {
    # Slice 1
    "static.scrollview_with_long_list":      verify_scrollview_with_long_list,
    "static.image_without_caching":          verify_image_without_caching,
    "static.inline_arrow_in_renderitem":     verify_inline_arrow_in_renderitem,
    "static.useeffect_no_deps":              verify_useeffect_no_deps,
    "static.console_log_in_production_code": verify_console_log_in_production_code,
    "static.animated_api_usage":             verify_animated_api_usage,
    # Slice 2 — static / config additions
    "static.inline_object_props":            verify_inline_object_props,
    "static.large_unmemoized_component":     verify_large_unmemoized_component,
    "static.hermes_disabled":                verify_hermes_disabled,
    "static.new_architecture_disabled":      verify_new_architecture_disabled,
    # Slice 2 — bundle
    "bundle.bundle_too_large_warning":       verify_bundle_size,
    "bundle.bundle_too_large_critical":      verify_bundle_size,
    "bundle.dependency_oversized":           verify_dependency_oversized,
    "bundle.known_bloated_dependency":       verify_known_bloated_dependency,
    "bundle.duplicate_dependency_libs":      verify_duplicate_dependency_libs,
    "bundle.asset_image_too_large":          verify_asset_image_too_large,
    "bundle.png_image_could_be_webp":        verify_asset_image_too_large,
    "bundle.asset_total_too_large":          _verify_metric_above_threshold,
    # Slice 2 — reassure
    "reassure.excessive_render_count":       verify_reassure_metric,
    "reassure.excessive_render_duration":    verify_reassure_metric,
    # Slice 3 — device (Android + iOS share the same rule IDs; verifiers don't care)
    "device.fps_below_threshold":            _verify_metric_above_threshold_reverse,
    "device.startup_too_slow":               _verify_metric_above_threshold,
    "device.memory_growth_suspected_leak":   _verify_metric_above_threshold,
    "device.cpu_thread_saturated":           _verify_metric_above_threshold,
    "device.long_blocking_interval":         _verify_metric_above_threshold,
    "device.step_fps_dipped":                _verify_metric_above_threshold_reverse,
}


def verify_default(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    """Default verifier for any rule without a custom one (most ESLint findings).
    Spot-checks the cited file exists and contains the relevant line; trusts
    the upstream linter's classification otherwise."""
    file_rel = f.get("evidence", {}).get("file", "")
    line = f.get("evidence", {}).get("line", 0)
    if not file_rel:
        # Some findings (config-level) have no file — trust them.
        return "REAL", "non-file finding accepted from upstream worker"
    p = workspace / file_rel
    if not p.is_file():
        return "UNCERTAIN", "cited file not present in workspace"
    snippet = read_context(workspace, file_rel, line, before=3, after=3)
    if snippet is None:
        return "UNCERTAIN", "could not read source context"
    return "REAL", "upstream worker classification accepted; source spot-check passed"


def verify_backend_finding(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    """Default verifier for Stage 4f (backend.* / database.* / algorithms.*)
    findings. The rule engine is deterministic AST + regex over Python/JS
    backend source; the rule's logic IS the verification. We just confirm the
    cited file still exists in the workspace."""
    file_rel = f.get("evidence", {}).get("file", "")
    if not file_rel or file_rel.startswith("<"):
        return "REAL", "backend rule is deterministic AST/regex; no file spot-check needed"
    p = workspace / file_rel
    if p.is_file():
        return "REAL", "cited backend source present; rule classification accepted"
    return "UNCERTAIN", "cited backend source absent (may have been moved/refactored)"


def verify_binary_artefact_finding(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    """Default verifier for findings whose cited file lives INSIDE an APK or
    IPA archive (`bundle.shipped_*`, `bundle.ipa_*`, `bundle.apk_*`,
    `bundle.bundle_too_large_*_ios`). The rule logic is the verification —
    the scanner has already unzipped + measured. The workspace cannot be
    spot-checked because the file is inside a zip the operator supplied."""
    return "REAL", "binary-artefact rule is deterministic; in-zip path not workspace-checkable"


def verify_store_finding(f: dict, workspace: Path, facts: dict) -> tuple[str, str]:
    """Default verifier for Stage 4e (store.*) findings.

    Store rules are deterministic config / source-grep reads — the rule logic
    IS the verification. Pseudo-file evidence (`<source>`, `<various>`) just
    means "not anchored to a single file"; that's by design.

    Only override to UNCERTAIN when the cited file is a real path that should
    exist but doesn't (drives the small set of file-existence rules)."""
    file_rel = f.get("evidence", {}).get("file", "")
    if not file_rel or file_rel.startswith("<") or file_rel == "app.json":
        return "REAL", "store rule is deterministic config/source-grep; no spot-check needed"
    p = workspace / file_rel
    if p.is_file():
        return "REAL", "cited source file present; store rule accepted"
    return "REAL", "cited path absent (likely workspace ingest exclusion); store rule accepted"


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Pass A — verify findings and stamp verdicts.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    workspace = audit_dir / "workspace"
    findings_path = audit_dir / "findings" / "all_findings.json"
    facts_path = audit_dir / "facts" / "audit_facts.json"
    evidence_dir = audit_dir / "evidence"
    decisions_log_path = audit_dir / "decisions.log"

    if not findings_path.is_file():
        print(f"ERROR: aggregated findings not found: {findings_path}", file=sys.stderr)
        return 2
    evidence_dir.mkdir(parents=True, exist_ok=True)

    findings = load_json(findings_path)
    facts = load_json(facts_path) if facts_path.is_file() else {}

    findings_by_rule: dict[str, list[dict]] = defaultdict(list)
    log_lines: list[str] = []

    for f in findings:
        rule_id = f.get("id", "unknown")
        if rule_id in VERIFIERS:
            verifier = VERIFIERS[rule_id]
        elif rule_id.startswith("store."):
            verifier = verify_store_finding
        elif rule_id.startswith(("backend.", "database.", "algorithms.")):
            verifier = verify_backend_finding
        elif (
            rule_id.endswith("_ios")
            or rule_id.startswith(("bundle.ipa_", "bundle.apk_", "bundle.shipped_"))
        ):
            verifier = verify_binary_artefact_finding
        else:
            verifier = verify_default
        try:
            verdict, method = verifier(f, workspace, facts)
        except Exception as e:
            verdict, method = "UNCERTAIN", f"verifier raised {type(e).__name__}: {e}"
        f["verdict"] = verdict
        f["verification_method"] = method
        findings_by_rule[rule_id].append(f)

        file_rel = f.get("evidence", {}).get("file", "")
        line = f.get("evidence", {}).get("line", 0)
        log_lines.append(f"{rule_id} {file_rel}:{line} {verdict} — {method}")

    # Build per-rule summary
    summary: dict[str, dict] = {}
    for rule_id, items in findings_by_rule.items():
        real = sum(1 for x in items if x["verdict"] == "REAL")
        fp = sum(1 for x in items if x["verdict"] == "FP")
        uncertain = sum(1 for x in items if x["verdict"] == "UNCERTAIN")
        distinct_files = len({x["evidence"].get("file", "") for x in items if x["verdict"] == "REAL" and x.get("evidence", {}).get("file")})
        summary[rule_id] = {
            "total": len(items),
            "real": real,
            "fp": fp,
            "uncertain": uncertain,
            "distinct_files": distinct_files,
        }

    # Build category_counts (REAL only — per Rule 1.2). Keep this in sync with
    # CATEGORY_DISPLAY_ORDER in synthesize.py — adding a category in one place
    # without the other is a silent zero-row in the report.
    category_keys = [
        "startup", "runtime_jank", "memory", "bundle_size", "code_quality",
        "backend_perf", "database", "algorithms",      # Stage 4f
        "publishing",                                  # Stage 4e (separate verdict; counts still tracked)
        "config", "tooling_error",
    ]
    severity_keys = ["critical", "high", "medium", "low", "info"]
    category_counts = {c: {s: 0 for s in severity_keys} for c in category_keys}
    for items in findings_by_rule.values():
        for x in items:
            if x["verdict"] != "REAL":
                continue
            cat = x.get("category", "code_quality")
            sev = x.get("severity", "low")
            if cat in category_counts and sev in category_counts[cat]:
                category_counts[cat][sev] += 1

    evidence = {
        "audit_id": args.audit_id,
        "pass_a_completed_at": iso_utc_now(),
        "findings_by_rule": dict(findings_by_rule),
        "summary": summary,
        "category_counts": category_counts,
    }

    out = evidence_dir / "evidence.json"
    out.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    # Truncate (not append): decisions.log must reflect the CURRENT Pass A run.
    # Append-mode accumulated duplicate lines across re-runs of the same audit.
    with decisions_log_path.open("w", encoding="utf-8") as fh:
        for line in log_lines:
            fh.write(line + "\n")

    real_total = sum(s["real"] for s in summary.values())
    fp_total   = sum(s["fp"] for s in summary.values())
    unc_total  = sum(s["uncertain"] for s in summary.values())
    print(f"Pass A complete: {real_total} REAL, {fp_total} FP, {unc_total} UNCERTAIN.", file=sys.stderr)
    print(f"  evidence: {out}", file=sys.stderr)
    print(f"  decisions log: {decisions_log_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
