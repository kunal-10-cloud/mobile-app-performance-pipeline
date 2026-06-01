#!/usr/bin/env python3
"""
Stage 6 — Synthesize: deterministic dedupe + rank + score + slot-fill prep.

This script does ALL the numerical / ranking / categorization work. The LLM's
role at this stage is exclusively to fill the <<PROSE>> regions in the report
skeleton; counts, citations, ranking, and scores are computed here in Python
and never narrated by the LLM. (Per references.md Rule 1.2 + ranking_heuristics.md.)

Inputs:
  - ${AUDIT_DIR}/evidence/evidence.json      (per-rule findings with verdicts)
  - ${AUDIT_DIR}/facts/audit_facts.json      (pinned codebase facts)
  - references/ranking_heuristics.md         (weights — read for reference; the
                                              actual constants are mirrored in
                                              this script so changes here and
                                              there stay in sync)

Outputs:
  - ${AUDIT_DIR}/report/report.json          (structured report — the source
                                              from which render_report.py
                                              produces the final markdown)
  - ${AUDIT_DIR}/report/synthesis_input.json (the slot-filled + skeleton hand-off
                                              to the LLM for <<PROSE>> filling;
                                              consumed by render_report.py)

Note: this script does NOT call the LLM directly. It prepares the synthesis
input. SKILL.md Step 6 instructs the LLM (Claude, in the calling session) to
read prompts/synthesize.md + synthesis_input.json and return the prose-fills,
which are then written to ${AUDIT_DIR}/report/prose_fills.json before
render_report.py runs.

This separation keeps the pipeline LLM-agnostic — any LLM that can produce
schema-valid JSON can fill the prose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ──────────────────────────────────────────────────────────────────────────────
# Ranking constants — mirror references/ranking_heuristics.md
# Update both this file and that file in tandem.
# ──────────────────────────────────────────────────────────────────────────────

SEVERITY_PENALTY = {"critical": 30, "high": 15, "medium": 6, "low": 2, "info": 0}
CONFIDENCE_MULTIPLIER = {"high": 1.0, "medium": 0.6, "low": 0.3}

SEVERITY_WEIGHT = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
CONFIDENCE_WEIGHT = {"high": 3, "medium": 2, "low": 1}
LAYER_WEIGHT = {
    "device_android": 4, "device_ios": 4,
    "reassure": 3, "bundle": 2, "static": 1, "tooling": 0,
    "backend": 3,   # backend findings often gate scale; weight between bundle and device
}

CATEGORY_SCORE_WEIGHTS = {
    "startup":      0.22,
    "runtime_jank": 0.18,
    "bundle_size":  0.14,
    "memory":       0.10,
    "code_quality": 0.06,
    # Stage 4f (backend / DB / algorithms) — same weighting class as runtime_jank;
    # backend perf issues affect every request, so they're load-bearing.
    "backend_perf": 0.14,
    "database":     0.10,
    "algorithms":   0.06,
}

CATEGORY_DISPLAY_ORDER = [
    "startup", "runtime_jank", "bundle_size", "memory", "code_quality",
    "backend_perf", "database", "algorithms",
    "config", "tooling_error",
]
SEVERITY_DISPLAY_ORDER = ["critical", "high", "medium", "low", "info"]

GRADE_BREAKPOINTS = [
    (90, "EXCELLENT", "🟢"),
    (75, "GOOD",      "🟢"),
    (50, "NEEDS WORK", "🟡"),
    (0,  "POOR",      "🔴"),
]

TOP_N = 5
SEVERITY_TO_PRIORITY = {
    "critical": "CRITICAL", "high": "HIGH", "medium": "HIGH",
    "low": "LOW", "info": "LOW",
}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def grade_for(score: int) -> tuple[str, str]:
    for threshold, label, emoji in GRADE_BREAKPOINTS:
        if score >= threshold:
            return label, emoji
    return "POOR", "🔴"


def category_score(category_counts: dict[str, dict[str, int]], category: str) -> int:
    """Compute per-category score = max(0, 100 - sum(severity_penalty * confidence_multiplier))."""
    # category_counts[category] = {critical: N, high: N, ...}; we need confidence-multiplied,
    # but Pass A doesn't carry confidence in category_counts — fetch from raw findings instead.
    # The caller passes the multiplied total via a different path; here we compute from severity
    # counts only and multiply by a category-average confidence of 1.0 as a conservative default.
    raw = category_counts.get(category, {})
    penalty_total = 0.0
    for sev, count in raw.items():
        penalty_total += SEVERITY_PENALTY.get(sev, 0) * count
    return max(0, int(round(100 - penalty_total)))


def category_score_with_confidence(real_findings_by_cat: dict[str, list[dict]], category: str) -> int:
    """More accurate per-category score: walks real findings in the category and
    applies severity_penalty * confidence_multiplier."""
    findings = real_findings_by_cat.get(category, [])
    penalty_total = 0.0
    for f in findings:
        sev = f.get("severity", "low")
        conf = f.get("confidence", "medium")
        penalty_total += SEVERITY_PENALTY.get(sev, 0) * CONFIDENCE_MULTIPLIER.get(conf, 0.6)
    return max(0, int(round(100 - penalty_total)))


def compute_overall_score(leads: list[dict], distinct_files_for_rule: dict[str, int]) -> int:
    """Penalty-summation score in [0, 100].

    overall = 100 - Σ(per-lead penalty), where each lead's penalty is
        severity_penalty × confidence_multiplier × coverage_factor
    and coverage_factor = 1 + COVERAGE_STEP × (distinct_files − 1), capped at
    COVERAGE_CAP. So a `medium` rule firing across 6 files hurts noticeably more
    than the same rule at 1 site, but a single rule can't sink the whole score
    on its own. Clamped to [0, 100]."""
    COVERAGE_STEP = 0.15
    COVERAGE_CAP = 2.0
    total_penalty = 0.0
    for f in leads:
        sev = f.get("severity", "low")
        conf = f.get("confidence", "medium")
        base = SEVERITY_PENALTY.get(sev, 0) * CONFIDENCE_MULTIPLIER.get(conf, 0.6)
        files = distinct_files_for_rule.get(f.get("id", ""), 1) or 1
        coverage_factor = min(COVERAGE_CAP, 1.0 + COVERAGE_STEP * max(0, files - 1))
        total_penalty += base * coverage_factor
    return max(0, min(100, int(round(100 - total_penalty))))


def rank_weight(f: dict, distinct_files_for_rule: dict[str, int]) -> int:
    sev = f.get("severity", "low")
    conf = f.get("confidence", "medium")
    layer = f.get("layer", "static")
    coverage = min(5, distinct_files_for_rule.get(f.get("id", ""), 1))
    return (
        SEVERITY_WEIGHT.get(sev, 1) * 100
        + CONFIDENCE_WEIGHT.get(conf, 2) * 10
        + coverage
        + LAYER_WEIGHT.get(layer, 0)
    )


# ──────────────────────────────────────────────────────────────────────────────
# Dedup heuristics — simple cross-layer collapse
# ──────────────────────────────────────────────────────────────────────────────

def dedupe_lead_findings(real_findings: list[dict]) -> list[dict]:
    """Two-pass dedupe:

    Pass 1 — per-rule consolidation: each rule_id becomes ONE lead, regardless
    of how many sites it fires on. The lead carries the highest-severity instance;
    the count_label (built elsewhere) tells the reader "N sites across M files".

    Pass 2 — cross-layer cleanup: when a device-layer lead and a non-device lead
    cite the same (file, function), the device one wins and the static one is
    folded in as `related_finding_ids`. This is the only place we collapse by
    site, and only across layers (different rules in the same layer always stay
    separate cards).
    """
    # Pass 1 — one lead per rule_id, picking the highest-severity instance.
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for f in real_findings:
        by_rule[f.get("id", "unknown")].append(f)

    leads_by_rule: dict[str, dict] = {}
    for rule_id, instances in by_rule.items():
        lead = max(instances, key=lambda f: SEVERITY_WEIGHT.get(f.get("severity", "low"), 1))
        leads_by_rule[rule_id] = lead

    # Pass 2 — cross-layer site collapse (device beats static / bundle / reassure).
    # We do NOT collapse within the same layer-class; different rules at the same
    # site stay separate leads.
    DEVICE_LAYERS = {"device_android", "device_ios"}
    leads = list(leads_by_rule.values())
    site_to_device_lead: dict[tuple[str, str], dict] = {}
    for f in leads:
        if f.get("layer") in DEVICE_LAYERS:
            ev = f.get("evidence", {}) or {}
            site = (ev.get("file", ""), ev.get("function", ""))
            if site != ("", ""):
                site_to_device_lead[site] = f

    final_leads: list[dict] = []
    for f in leads:
        ev = f.get("evidence", {}) or {}
        site = (ev.get("file", ""), ev.get("function", ""))
        if f.get("layer") in DEVICE_LAYERS:
            final_leads.append(f)
            continue
        device_lead = site_to_device_lead.get(site)
        if device_lead is not None and id(device_lead) != id(f):
            # Fold this finding's rule_id into the device lead's related list,
            # don't surface it as its own card.
            related = device_lead.get("related_finding_ids", []) or []
            device_lead["related_finding_ids"] = sorted(set(related + [f.get("id", "")]))
            continue
        final_leads.append(f)
    return final_leads


# ──────────────────────────────────────────────────────────────────────────────
# Slot building
# ──────────────────────────────────────────────────────────────────────────────

def build_slot_map(audit_id: str, audit_meta: dict, facts: dict, evidence: dict, leads: list[dict]) -> dict:
    """Build the {{slot}} substitution map consumed by render_report.py."""
    sig = facts.get("project_signature", {}) or {}
    cat_counts = evidence.get("category_counts", {}) or {}

    # Real findings by category for the more-accurate confidence-weighted score
    real_by_cat: dict[str, list[dict]] = defaultdict(list)
    for f in leads:
        real_by_cat[f.get("category", "code_quality")].append(f)

    # Per-category scores (still shown per-area in the breakdown table).
    per_category_scores = {
        cat: category_score_with_confidence(real_by_cat, cat)
        for cat in CATEGORY_SCORE_WEIGHTS
    }
    # Overall score: penalty-SUMMATION across all leads, NOT a per-category
    # weighted average. The old average let empty categories (which sit at 100)
    # wash out the categories that actually had findings — 28 real findings
    # scored 90/EXCELLENT. The summation model makes total finding burden drive
    # the number, with a coverage multiplier so a rule firing across many files
    # hurts more than a single site.
    distinct_files_for_rule = {rid: s.get("distinct_files", 0) for rid, s in evidence.get("summary", {}).items()}
    overall_score = compute_overall_score(leads, distinct_files_for_rule)
    verdict_label, verdict_emoji = grade_for(overall_score)

    # Severity totals (CRITICAL/HIGH/LOW grouping)
    severity_totals = {"CRITICAL": 0, "HIGH": 0, "LOW": 0}
    for f in leads:
        bucket = SEVERITY_TO_PRIORITY.get(f.get("severity", "low"), "LOW")
        severity_totals[bucket] += 1

    # Per-category breakdown table rows
    distinct_files_for_rule = {rid: s.get("distinct_files", 0) for rid, s in evidence.get("summary", {}).items()}
    per_category_rows = []
    for cat in CATEGORY_DISPLAY_ORDER:
        if cat not in cat_counts:
            continue
        sev_breakdown = cat_counts[cat]
        if not any(sev_breakdown.values()):
            continue
        score = per_category_scores.get(cat, 100) if cat in CATEGORY_SCORE_WEIGHTS else "—"
        per_category_rows.append({
            "category": cat,
            "critical": sev_breakdown.get("critical", 0),
            "high":     sev_breakdown.get("high", 0),
            "medium":   sev_breakdown.get("medium", 0),
            "low":      sev_breakdown.get("low", 0),
            "score":    score,
        })

    # Top-N
    leads_ranked = sorted(leads, key=lambda f: -rank_weight(f, distinct_files_for_rule))
    top_n = leads_ranked[:TOP_N]
    top_n_ids = {f.get("id") for f in top_n}
    top_n_list = []
    for i, f in enumerate(top_n, start=1):
        ev = f.get("evidence", {})
        loc = f"`{ev.get('file','?')} — {ev.get('function','?')}`" if ev.get("file") else "(project-wide)"
        title = f.get("title", f.get("id", "unknown"))
        top_n_list.append({"rank": i, "title": title, "location": loc, "id": f.get("id")})

    # Mark each lead with `top_n` and (where applicable) attach `source_for_fix`
    # so the LLM can emit a unified diff. We do not include source for layers
    # whose findings are not file-anchored (bundle totals, device metrics) or
    # for rules whose fix lives in app.json / package.json (already documented
    # in their `description`).
    source_layers_for_fix = {"static", "reassure"}
    skip_fix_rule_ids = {
        "static.hermes_disabled",
        "static.new_architecture_disabled",
        # bundle / device findings — fix isn't a source diff
    }
    for f in leads:
        is_top = f.get("id") in top_n_ids
        f["__synth_meta"] = {"top_n": is_top}
        if not is_top:
            continue
        layer = f.get("layer", "")
        rule_id = f.get("id", "")
        if layer not in source_layers_for_fix or rule_id in skip_fix_rule_ids:
            continue
        ev = f.get("evidence", {}) or {}
        file_rel = ev.get("file")
        line = ev.get("line", 0)
        if not file_rel:
            continue
        snippet = _read_source_window(audit_id, file_rel, line, before=15, after=15)
        if snippet is None:
            continue
        f["__synth_meta"]["source_for_fix"] = {
            "file": file_rel,
            "function": ev.get("function") or "<module>",
            "anchor_line": line,
            "before_lines": 15,
            "after_lines": 15,
            "source_window": snippet,
        }

    # Per-severity sections
    by_priority: dict[str, list[dict]] = {"CRITICAL": [], "HIGH": [], "LOW": []}
    for f in leads_ranked:
        bucket = SEVERITY_TO_PRIORITY.get(f.get("severity", "low"), "LOW")
        by_priority[bucket].append(f)

    # Count-label per rule (e.g. "3 sites across 2 files")
    count_labels: dict[str, str] = {}
    for rule_id, s in evidence.get("summary", {}).items():
        real = s.get("real", 0)
        files = s.get("distinct_files", 0)
        if real == 0:
            continue
        if real == 1 and files == 1:
            count_labels[rule_id] = "1 site"
        else:
            count_labels[rule_id] = f"{real} sites across {files} file{'s' if files != 1 else ''}"

    # Working-well rows derived from facts
    working_well: list[dict] = []
    if sig.get("hermes_enabled") is True:
        working_well.append({"check": "Hermes JS engine", "status": "PASS", "notes": "Hermes is enabled."})
    if sig.get("new_architecture_enabled") is True:
        working_well.append({"check": "New Architecture (Fabric + TurboModules)", "status": "PASS", "notes": "newArchEnabled is set."})
    deps = facts.get("dependencies", {}) or {}
    if deps.get("reanimated_present"):
        working_well.append({"check": "react-native-reanimated installed", "status": "PASS", "notes": "Animations can run on the UI thread."})
    if deps.get("expo_image_present"):
        working_well.append({"check": "expo-image installed", "status": "PASS", "notes": "Image caching library available."})
    if deps.get("flash_list_present"):
        working_well.append({"check": "@shopify/flash-list installed", "status": "PASS", "notes": "High-performance list library available."})
    if deps.get("screens_present"):
        working_well.append({"check": "react-native-screens installed", "status": "PASS", "notes": "Native screen optimisations enabled."})
    if sig.get("typescript_present"):
        working_well.append({"check": "TypeScript configured", "status": "PASS", "notes": "Type checking available for refactor safety."})

    # Bundle composition table (Slice 2) — derive from the bundle-layer findings
    # in `evidence` (not from leads, which may be deduped) so the platform sizes
    # and heavy-dep list always reflect raw measurement.
    bundle_table = _build_bundle_table(evidence)

    # Device metrics table (Slice 3) — read directly from the per-platform
    # perf_result.json files so we surface the raw measurements alongside the
    # finding-derived score, not just the threshold breaches.
    device_metrics_table = _build_device_metrics_table(audit_id)

    # Lighthouse-style per-metric device breakdown (Mean FPS, worst-frame FPS,
    # jank/frozen ratios, peak memory, memory growth, CPU). This is the
    # human-readable device surface; the single composite score is intentionally
    # not shown (FPS+CPU-only, masks memory + freezes).
    device_lighthouse = _load_device_lighthouse(audit_id)
    device_lighthouse_ios = _load_device_lighthouse_ios(audit_id)

    # Stage 4e: store publishing readiness. Computed off the same evidence dict
    # (no separate file read) — returns None when the stage didn't run.
    publishing_verdict = _compute_publishing_verdict(evidence)
    store_readiness_table = _build_store_readiness_table(evidence)

    return {
        "audit_id": audit_id,
        "audit_date": evidence.get("pass_a_completed_at", ""),
        "verdict": verdict_label,
        "verdict_emoji": verdict_emoji,
        "overall_score": overall_score,
        "expo_sdk_version": sig.get("expo_sdk_version") or "unknown",
        "typescript_or_javascript": "TypeScript" if sig.get("typescript_present") else "JavaScript",
        "router_label": "Expo Router" if sig.get("expo_router_present") else ("React Navigation" if sig.get("react_navigation_present") else "(navigation library unidentified)"),
        "critical_count": severity_totals["CRITICAL"],
        "high_count": severity_totals["HIGH"],
        "low_count": severity_totals["LOW"],
        "per_category_rows": per_category_rows,
        "per_category_scores": per_category_scores,
        "top_n_list": top_n_list,
        "by_priority": by_priority,
        "count_labels": count_labels,
        "working_well_rows": working_well,
        "metrics_dashboard": _build_metrics_dashboard(device_metrics_table),
        "bundle_table": bundle_table,
        "device_metrics_table": device_metrics_table,
        "device_lighthouse": device_lighthouse,
        "device_lighthouse_ios": device_lighthouse_ios,
        "publishing_verdict": publishing_verdict,
        "store_readiness_table": store_readiness_table,
        # issue *types* = number of dedup'd leads; *sites* = total REAL findings
        # across all rules. Reported separately so "5 HIGH" doesn't look
        # contradictory next to "28 findings".
        "total_real_findings_count": sum(severity_totals.values()),
        "total_issue_types": len(leads),
        "total_sites_count": sum(s.get("real", 0) for s in evidence.get("summary", {}).values()),
    }


def _compute_coverage(audit_dir: Path, evidence: dict, facts: dict) -> list[dict]:
    """Build the report's Coverage & Limitations rows — what was actually
    exercised vs skipped/failed, so a 'test every corner' report states its own
    coverage honestly. Driven by which per-worker finding files exist + which
    tooling.* errors landed in evidence."""
    findings_dir = audit_dir / "findings"
    present = {p.name for p in findings_dir.glob("*.json")} if findings_dir.is_dir() else set()

    # tooling errors by id (signals a stage tried but failed/was unavailable)
    tooling_ids = set()
    for items in evidence.get("findings_by_rule", {}).values():
        for f in items:
            rid = f.get("id", "")
            if rid.startswith("tooling."):
                tooling_ids.add(rid)

    def status(ran: bool, failed_markers: tuple[str, ...] = ()) -> tuple[str, str]:
        if any(m in t for t in tooling_ids for m in failed_markers):
            return "⚠️ partial", "tool error — see notes"
        return ("✅ analysed", "") if ran else ("— skipped", "stage did not run in this environment")

    rows = []
    s, n = status("static.json" in present)
    rows.append({"aspect": "Static code analysis (AST + ESLint)", "status": s, "note": n})
    s, n = status("config.json" in present)
    rows.append({"aspect": "Config (Hermes / New Architecture)", "status": s, "note": n})
    s, n = status(bool(facts.get("source_pattern_counts")))
    rows.append({"aspect": "Codebase fact-gathering", "status": s, "note": n})

    bundle_ran = "bundle.json" in present or "apk.json" in present
    s, n = status(bundle_ran, ("bundle_export_failed",))
    rows.append({"aspect": "Bundle size & composition", "status": s, "note": n or "per-dependency via source-maps"})

    reassure_ran = "reassure.json" in present
    s, n = status(reassure_ran, ("reassure_",))
    rows.append({"aspect": "Component render perf (Reassure)", "status": s, "note": n})

    android_ran = "android_perf.json" in present
    rows.append({"aspect": "Android runtime (FPS / startup / memory)",
                 "status": "✅ measured" if android_ran else "— not measured",
                 "note": "" if android_ran else "needs APK + device/Flashlight-Cloud"})

    # iOS coverage — three states. The reliability label depends on whether the
    # run was on Apple Silicon (CPU-class device estimate) or Intel
    # (regression-relative only). Read it from the lighthouse JSON's measurement_env.
    ios_ran = "ios_run.json" in present or "ios_perf.json" in present
    ios_lh_path = audit_dir / "results" / "device_lighthouse_ios.json"
    ios_on_apple_silicon = False
    if ios_lh_path.is_file():
        try:
            ios_env = (json.loads(ios_lh_path.read_text(encoding="utf-8")).get("measurement_environment") or {})
            ios_on_apple_silicon = bool(ios_env.get("on_apple_silicon"))
        except Exception:
            pass
    if ios_ran:
        note = ("Apple Silicon Mac Simulator — memory growth reliable, cold start + peak memory device-class estimates, FPS by-design omitted"
                if ios_on_apple_silicon
                else "Intel Mac Simulator — memory growth reliable, cold start regression-relative, FPS by-design omitted")
        rows.append({"aspect": "iOS runtime (cold start / memory)",
                     "status": "✅ measured", "note": note})
    else:
        rows.append({"aspect": "iOS runtime (cold start / memory)",
                     "status": "— not measured",
                     "note": "needs macOS + Simulator (Flashlight Cloud is Android-only)"})

    ts = facts.get("tooling_status") or {}
    dep_ran = bool(ts)
    rows.append({"aspect": "Dependency hygiene (unused / circular / outdated)",
                 "status": "✅ analysed" if dep_ran else "— skipped",
                 "note": "" if dep_ran else "run depcheck/madge/ncu on the pod for full coverage"})

    store_ran = "store.json" in present
    s, n = status(store_ran, ("store_rule_failed",))
    rows.append({"aspect": "Store publishing readiness (Android / iOS)",
                 "status": s,
                 "note": n or ("config + source cross-check; network checks deferred to Phase B")})

    # Stage 4f — backend perf. Three states: not ingested / ingested-empty /
    # actually scanned. Distinguish them so the report tells the truth.
    backend_present = bool((facts.get("backend") or {}).get("present"))
    backend_skipped = any("backend_source_missing" in t or "backend_source_empty" in t for t in tooling_ids)
    if backend_present:
        rows.append({"aspect": "Backend / DB / algorithm perf (FastAPI)",
                     "status": "✅ analysed",
                     "note": "ported from web pipeline; runs against ingested backend/"})
    elif backend_skipped:
        rows.append({"aspect": "Backend / DB / algorithm perf (FastAPI)",
                     "status": "— skipped",
                     "note": "no backend tree ingested — re-ingest with backend/ to enable"})
    else:
        rows.append({"aspect": "Backend / DB / algorithm perf (FastAPI)",
                     "status": "— skipped",
                     "note": "Stage 4f did not run"})
    return rows


def _project_snapshot_from_facts(facts: dict) -> dict:
    """Lift the project_signature + a few high-signal flags into a render-ready dict."""
    sig = facts.get("project_signature") or {}
    deps = facts.get("dependencies") or {}
    return {
        "expo_sdk_version":        sig.get("expo_sdk_version") or "unknown",
        "react_native_version":    sig.get("react_native_version"),
        "react_version":           sig.get("react_version"),
        "package_manager":         sig.get("package_manager"),
        "typescript_present":      sig.get("typescript_present"),
        "expo_router_present":     sig.get("expo_router_present"),
        "react_navigation_present": sig.get("react_navigation_present"),
        "hermes_enabled":          sig.get("hermes_enabled"),
        "new_architecture_enabled": sig.get("new_architecture_enabled"),
        "android_package":         sig.get("bundle_identifier_android") or sig.get("android_package"),
        "ios_bundle_identifier":   sig.get("bundle_identifier_ios") or sig.get("ios_bundle_identifier"),
        "production_dependency_count": deps.get("production_count"),
        "dev_dependency_count":    deps.get("dev_count"),
    }


def _codebase_snapshot_from_facts(facts: dict) -> dict:
    """Surface the source_pattern_counts in a render-ready dict. The renderer
    turns this into a "Codebase snapshot" table so the reader sees what the
    audit observed across the codebase, not just the threshold breaches."""
    counts = facts.get("source_pattern_counts") or {}
    if not counts:
        return {}
    return {
        "react_memo_usages":          counts.get("react_memo_count", 0),
        "use_memo_usages":            counts.get("use_memo_count", 0),
        "use_callback_usages":        counts.get("use_callback_count", 0),
        "use_effect_calls":           counts.get("use_effect_count", 0),
        "use_effect_with_deps":       counts.get("use_effect_with_deps_count", 0),
        "use_effect_empty_deps":      counts.get("use_effect_empty_deps_count", 0),
        "scrollview_instances":       counts.get("scrollview_count", 0),
        "flatlist_instances":         counts.get("flatlist_count", 0),
        "sectionlist_instances":      counts.get("sectionlist_count", 0),
        "flashlist_instances":        counts.get("flashlist_count", 0),
        "rn_image_usages":            counts.get("rn_image_usage_count", 0),
        "expo_image_usages":          counts.get("expo_image_usage_count", 0),
        "console_log_calls":          counts.get("console_log_count", 0),
        "console_log_dev_guarded":    counts.get("console_log_dev_guarded_count", 0),
        "animated_rn_imports":        counts.get("animated_rn_import_count", 0),
        "reanimated_imports":         counts.get("reanimated_import_count", 0),
        "inline_arrow_renderitems":   counts.get("inline_arrow_renderitem_count", 0),
        "inline_object_jsx_props":    counts.get("inline_object_jsx_props_count", 0),
    }


def _dependencies_snapshot_from_facts(facts: dict) -> dict:
    deps = facts.get("dependencies") or {}
    return {
        "expo_image_present":        deps.get("expo_image_present"),
        "flash_list_present":        deps.get("flash_list_present"),
        "reanimated_present":        deps.get("reanimated_present"),
        "gesture_handler_present":   deps.get("gesture_handler_present"),
        "screens_present":           deps.get("screens_present"),
        "known_heavy_deps":          deps.get("known_heavy_deps") or [],
    }


def _read_source_window(audit_id: str, file_rel: str, line: int,
                        *, before: int = 15, after: int = 15) -> str | None:
    """Read N lines around (line) from workspace/file_rel for fix-diff context."""
    if not file_rel or line <= 0:
        return None
    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    p = base / audit_id / "workspace" / file_rel
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()
    if line > len(lines):
        return None
    start = max(0, line - before - 1)
    end = min(len(lines), line + after)
    # Number lines (1-indexed) so the LLM can emit accurate @@ -X,Y +X,Y @@ ranges.
    numbered = [f"{n + 1:5d}  {lines[n]}" for n in range(start, end)]
    return "\n".join(numbered)


def _build_device_metrics_table(audit_id: str) -> dict | None:
    """Read results/{android,ios}.json from this audit and return a render-ready
    summary. Returns None when neither file exists (so the report omits the section).
    Always read raw measurements — DO NOT derive these from findings; findings only
    surface threshold breaches, but the report shows actual numbers."""
    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / audit_id
    results_dir = audit_dir / "results"

    out: dict = {"platforms": {}}
    any_data = False
    for platform in ("android", "ios"):
        p = results_dir / f"{platform}.json"
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not data or data == {}:
            continue
        any_data = True
        iters = data.get("iterations") or []
        fps_avgs = [(it.get("fps") or {}).get("average") for it in iters]
        fps_avgs = [v for v in fps_avgs if isinstance(v, (int, float))]
        mem_peaks = [(it.get("memory_mb") or {}).get("peak") for it in iters]
        mem_peaks = [v for v in mem_peaks if isinstance(v, (int, float))]
        mem_growths = [(it.get("memory_mb") or {}).get("growth") for it in iters]
        mem_growths = [v for v in mem_growths if isinstance(v, (int, float))]

        out["platforms"][platform] = {
            "device_profile": data.get("device_profile") or "unknown",
            "score":          data.get("score"),
            "startup_time_ms": data.get("startup_time_ms"),
            "fps_avg_mean":   round(sum(fps_avgs) / len(fps_avgs), 1) if fps_avgs else None,
            "memory_peak_mb_max": round(max(mem_peaks), 1) if mem_peaks else None,
            "memory_growth_mb_total": round(sum(mem_growths), 1) if mem_growths else None,
            "iterations_count": len(iters),
            "warnings": data.get("tool_warnings") or [],
        }
    return out if any_data else None


def _load_device_lighthouse(audit_id: str) -> dict | None:
    """Load results/device_lighthouse.json (per-metric, individually-rated breakdown
    produced by compute_device_metrics.py). This is the primary device-runtime
    surface in the report: Flashlight's single 0-100 composite is FPS+CPU-only and
    hides memory + worst-frame problems, so we show the named metrics instead.
    Returns None when the file is absent (section omitted)."""
    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    p = base / audit_id / "results" / "device_lighthouse.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data or not data.get("metrics"):
        return None
    return data


def _load_device_lighthouse_ios(audit_id: str) -> dict | None:
    """Load the iOS Lighthouse-style breakdown (results/device_lighthouse_ios.json).
    Separate from the Android one because the metric set is smaller (no FPS by
    design — Mac GPU is not iPhone-comparable) and each row carries a
    reliability label the renderer surfaces."""
    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    p = base / audit_id / "results" / "device_lighthouse_ios.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data or not data.get("metrics"):
        return None
    return data


# ── Stage 4e: store publishing-readiness ─────────────────────────────────────
# Publishing findings (layer == "store") drive a separate READY/AT-RISK/BLOCKED
# verdict per store — they intentionally do NOT contribute to the overall perf
# score (the perf score answers "is the app fast"; the publishing verdict
# answers "can I ship it"; they are independent questions).

_PUBLISHING_BLOCKER_SEVERITIES = {"critical"}
_PUBLISHING_RISK_SEVERITIES = {"high"}


def _real_store_findings(evidence: dict) -> list[dict]:
    out: list[dict] = []
    for items in (evidence.get("findings_by_rule") or {}).values():
        for f in items:
            if f.get("layer") != "store":
                continue
            if f.get("verdict") and f["verdict"] != "REAL":
                continue
            out.append(f)
    return out


def _platform_of_store_finding(f: dict) -> str:
    """Map a store finding to 'apple', 'google', or 'cross'."""
    rid = f.get("id", "")
    if rid.startswith("store.ios.") or ".ios_" in rid:
        return "apple"
    if rid.startswith("store.android."):
        return "google"
    if rid.startswith("store.process."):
        # Per-platform process items
        if "google_services" in rid or "android" in rid:
            return "google"
        if "googleservice_info" in rid or "ios" in rid:
            return "apple"
        return "cross"
    return "cross"


def _verdict_for(critical: int, high: int) -> str:
    if critical > 0:
        return "BLOCKED"
    if high > 0:
        return "AT_RISK"
    return "READY"


def _worse_verdict(a: str, b: str) -> str:
    order = {"READY": 0, "AT_RISK": 1, "BLOCKED": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _compute_publishing_verdict(evidence: dict) -> dict | None:
    """Per-store + combined READY / AT_RISK / BLOCKED + severity counts.
    Returns None when no store findings exist (stage did not run)."""
    findings = _real_store_findings(evidence)
    if not findings:
        return None
    counts = {
        "apple_critical": 0, "apple_high": 0, "apple_medium": 0, "apple_low": 0, "apple_info": 0,
        "google_critical": 0, "google_high": 0, "google_medium": 0, "google_low": 0, "google_info": 0,
        "cross_critical": 0, "cross_high": 0, "cross_medium": 0, "cross_low": 0, "cross_info": 0,
    }
    for f in findings:
        plat = _platform_of_store_finding(f)
        sev = (f.get("severity") or "").lower()
        key = f"{plat}_{sev}"
        if key in counts:
            counts[key] += 1
    apple_verdict = _verdict_for(
        counts["apple_critical"] + counts["cross_critical"],
        counts["apple_high"] + counts["cross_high"],
    )
    google_verdict = _verdict_for(
        counts["google_critical"] + counts["cross_critical"],
        counts["google_high"] + counts["cross_high"],
    )
    combined = _worse_verdict(apple_verdict, google_verdict)
    return {
        "apple": apple_verdict,
        "google": google_verdict,
        "combined": combined,
        "counts": counts,
    }


def _build_store_readiness_table(evidence: dict) -> dict | None:
    """Group store findings into the render-ready dict.
      apple_blockers / google_blockers — sorted by severity (critical first)
      auto_assessed_process — process.* items (icon_unverified, google-services unverified, push, etc.)
      iap_skus              — extracted SKU list (from store.process.iap_skus_required)
      nutrition_labels      — per-SDK disclosure rows (from store.process.nutrition_label_categories)
      manual_checklist      — static items rendered as a checkbox list
    """
    findings = _real_store_findings(evidence)
    if not findings:
        return None

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

    apple: list[dict] = []
    google: list[dict] = []
    auto_assessed: list[dict] = []
    iap_skus: list[dict] = []
    nutrition_labels: list[dict] = []

    for f in findings:
        rid = f.get("id", "")
        row = {
            "id": rid,
            "severity": f.get("severity"),
            "title": f.get("title"),
            "description": f.get("description"),
            "evidence": f.get("evidence") or {},
        }
        if rid == "store.process.iap_skus_required":
            iap_skus.append(row)
            continue
        if rid == "store.process.nutrition_label_categories":
            nutrition_labels.append(row)
            continue
        if rid.startswith("store.process.") or rid == "store.cross.icon_unverified":
            auto_assessed.append(row)
            continue
        if rid.startswith("store.ios.") or rid == "store.ios.usage_description_uses_default":
            apple.append(row)
            continue
        if rid.startswith("store.android."):
            google.append(row)
            continue
        # cross-cutting code/config findings: surface on both lists
        apple.append(row)
        google.append(row)

    apple.sort(key=lambda r: sev_order.get(r["severity"], 99))
    google.sort(key=lambda r: sev_order.get(r["severity"], 99))
    auto_assessed.sort(key=lambda r: sev_order.get(r["severity"], 99))

    return {
        "apple_blockers": apple,
        "google_blockers": google,
        "auto_assessed_process": auto_assessed,
        "iap_skus": iap_skus,
        "nutrition_labels": nutrition_labels,
        "manual_checklist": [
            "Screenshots for: iPhone 6.9\", iPhone 6.7\", iPad 13\" (Apple); Phone, 7\" tablet, 10\" tablet (Play)",
            "App Store Connect listing copy: description, keywords, support URL, marketing URL",
            "Play Console listing copy: short description, full description, feature graphic (1024×500)",
            "Content rating questionnaire completed in both consoles",
            "TestFlight beta exercised by 1+ real-device tester",
            "Play Internal/Closed testing track exercised",
            "In-App Purchase products created in App Store Connect AND Play Console matching the SKUs above",
            "Privacy policy text reviewed by legal (we only verify the URL is reachable, not the content)",
        ],
    }


def _build_metrics_dashboard(device_metrics_table: dict | None) -> list[dict]:
    """Lighthouse-style row format consumed by render_report.py's "Measured metrics"
    section: each row has {name, android, ios, threshold, status}."""
    if not device_metrics_table:
        return []
    platforms = device_metrics_table.get("platforms") or {}
    ax = platforms.get("android") or {}
    ix = platforms.get("ios") or {}

    def _fmt(v, unit=""):
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.1f}{unit}"
        return f"{v}{unit}"

    rows = [
        {"name": "Cold start", "android": _fmt(ax.get("startup_time_ms"), " ms"),
         "ios": _fmt(ix.get("startup_time_ms"), " ms"),
         "threshold": "< 1500 ms",
         "status": _status_for_startup(ax.get("startup_time_ms"), ix.get("startup_time_ms"))},
        {"name": "Mean FPS", "android": _fmt(ax.get("fps_avg_mean")),
         "ios": _fmt(ix.get("fps_avg_mean")),
         "threshold": "≥ 55",
         "status": _status_for_fps(ax.get("fps_avg_mean"), ix.get("fps_avg_mean"))},
        {"name": "Peak memory", "android": _fmt(ax.get("memory_peak_mb_max"), " MB"),
         "ios": _fmt(ix.get("memory_peak_mb_max"), " MB"),
         "threshold": "—",
         "status": ""},
        {"name": "Memory growth (sum)", "android": _fmt(ax.get("memory_growth_mb_total"), " MB"),
         "ios": _fmt(ix.get("memory_growth_mb_total"), " MB"),
         "threshold": "< 10 MB",
         "status": _status_for_growth(ax.get("memory_growth_mb_total"), ix.get("memory_growth_mb_total"))},
    ]
    return rows


def _status_for_startup(a, i):
    worst = max([v for v in (a, i) if isinstance(v, (int, float))] or [0])
    if worst == 0: return ""
    if worst > 4000: return "🔴"
    if worst > 2500: return "🟡"
    if worst > 1500: return "🟡"
    return "🟢"


def _status_for_fps(a, i):
    worst = min([v for v in (a, i) if isinstance(v, (int, float))] or [60])
    if worst == 60 and a is None and i is None: return ""
    if worst < 30: return "🔴"
    if worst < 45: return "🟡"
    if worst < 55: return "🟡"
    return "🟢"


def _status_for_growth(a, i):
    worst = max([v for v in (a, i) if isinstance(v, (int, float))] or [0])
    if worst == 0: return ""
    if worst > 30: return "🔴"
    if worst > 10: return "🟡"
    return "🟢"


def _build_bundle_table(evidence: dict) -> dict | None:
    """Aggregate bundle-layer findings into a render-ready table dict.
    Returns None when no bundle stage ran (so the report omits the section)."""
    bundle_findings: list[dict] = []
    for items in evidence.get("findings_by_rule", {}).values():
        for f in items:
            if f.get("verdict") != "REAL":
                continue
            if f.get("layer") == "bundle":
                bundle_findings.append(f)
    if not bundle_findings:
        return None

    platforms: dict[str, int] = {}
    heavy_deps: list[tuple[str, int]] = []
    duplicate_pairs: list[str] = []
    large_assets: list[tuple[str, int]] = []
    png_candidates: list[tuple[str, int]] = []
    non_image_total: int | None = None

    for f in bundle_findings:
        rid = f.get("id", "")
        ev = f.get("evidence", {}) or {}
        val = ev.get("metric_value")
        if rid in ("bundle.bundle_too_large_warning", "bundle.bundle_too_large_critical"):
            file_hint = ev.get("file", "")
            for plat in ("android", "ios", "web"):
                if f"/{plat}/" in file_hint or file_hint.endswith(f"{plat}/"):
                    if isinstance(val, (int, float)):
                        platforms[plat] = max(platforms.get(plat, 0), int(val))
                    break
        elif rid in ("bundle.dependency_oversized", "bundle.known_bloated_dependency"):
            pkg = (ev.get("file", "") or "").replace("node_modules/", "").strip("/")
            if pkg and isinstance(val, (int, float)):
                heavy_deps.append((pkg, int(val)))
        elif rid == "bundle.duplicate_dependency_libs":
            snip = ev.get("code_snippet") or ev.get("metric_name") or ""
            if snip:
                duplicate_pairs.append(snip)
        elif rid == "bundle.asset_image_too_large":
            if isinstance(val, (int, float)):
                large_assets.append((ev.get("file", "(unknown)"), int(val)))
        elif rid == "bundle.png_image_could_be_webp":
            if isinstance(val, (int, float)):
                png_candidates.append((ev.get("file", "(unknown)"), int(val)))
        elif rid == "bundle.asset_total_too_large":
            if isinstance(val, (int, float)):
                non_image_total = int(val)

    # Deduplicate heavy_deps by (pkg, size), keep max-sized entry per pkg
    pkg_max: dict[str, int] = {}
    for pkg, size in heavy_deps:
        pkg_max[pkg] = max(pkg_max.get(pkg, 0), size)
    heavy_deps_sorted = sorted(pkg_max.items(), key=lambda kv: -kv[1])[:10]

    large_assets_sorted = sorted(large_assets, key=lambda kv: -kv[1])[:10]
    png_candidates_sorted = sorted(png_candidates, key=lambda kv: -kv[1])[:10]

    return {
        "platforms": platforms,
        "heavy_dependencies": [{"package": p, "bytes": s} for p, s in heavy_deps_sorted],
        "duplicate_pairs": duplicate_pairs,
        "large_assets": [{"path": p, "bytes": s} for p, s in large_assets_sorted],
        "png_candidates_for_webp": [{"path": p, "bytes": s} for p, s in png_candidates_sorted],
        "non_image_asset_total_bytes": non_image_total,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Build PROSE-fill schema (for the LLM)
# ──────────────────────────────────────────────────────────────────────────────

def build_prose_regions_spec(slots: dict) -> dict:
    """Describe each <<PROSE id="...">> region the LLM must fill.
    Returns a JSON-schema-ish object the LLM's response is validated against."""
    regions = []

    # Executive summary
    regions.append({
        "id": "exec_summary",
        "instruction": (
            "Two paragraphs. First: what's working architecturally — cite facts from "
            "audit_facts.json in natural language (e.g. 'Hermes is enabled', 'TypeScript "
            "is configured'). Second: top concerns from the report. Reference counts "
            "via slot-filled numbers, never narrate."
        ),
        "max_chars": 1500,
    })

    # Metric-bullets are conditional on metrics_dashboard being non-empty
    if slots.get("metrics_dashboard"):
        regions.append({
            "id": "metrics_plainterms",
            "instruction": "3-5 short bullets explaining what each metric in the dashboard means in plain terms. Only for metrics with values.",
            "max_chars": 800,
        })

    # Per-finding prose regions
    for priority in ("CRITICAL", "HIGH", "LOW"):
        for f in slots["by_priority"].get(priority, []):
            fid = f.get("id", "unknown")
            ev = f.get("evidence", {})
            meta = f.get("__synth_meta", {}) or {}
            regions.append({
                "id": f"{fid}__evidence_prose",
                "instruction": f"1-3 sentences describing what the analyzer detected for {fid} at {ev.get('file','?')} — {ev.get('function','?')}. Cite file:function in natural language.",
                "max_chars": 400,
            })
            regions.append({
                "id": f"{fid}__impact_prose",
                "instruction": f"2-3 sentences on the technical + business impact of {fid}.",
                "max_chars": 500,
            })
            regions.append({
                "id": f"{fid}__plain_terms",
                "instruction": f"1-2 sentence analogy that a non-developer understands. For {fid}.",
                "max_chars": 300,
            })
            regions.append({
                "id": f"{fid}__actionables",
                "instruction": f"3-5 bullet recommendations specific to {fid}. Each bullet starts with a verb. Markdown bullet list.",
                "max_chars": 800,
            })
            regions.append({
                "id": f"{fid}__after_fixing",
                "instruction": f"1 sentence on user-visible improvement after fixing {fid}.",
                "max_chars": 300,
            })
            # Fix-diff region — only for top-N findings whose source we've bundled.
            if meta.get("top_n") and meta.get("source_for_fix"):
                src = meta["source_for_fix"]
                regions.append({
                    "id": f"{fid}__fix_diff",
                    "instruction": (
                        f"Unified diff that fixes {fid} at {src['file']} — {src['function']}. "
                        f"Anchor line ~{src['anchor_line']}. Source window (line-numbered) is in "
                        f"`synthesis_input.slots.by_priority[*].__synth_meta.source_for_fix.source_window` "
                        f"for this finding. Emit standard `--- a/<path>` / `+++ b/<path>` headers and minimal "
                        f"`@@` hunks. Return an empty string if no source-level fix applies."
                    ),
                    "max_chars": 2000,
                })

    # Highest-impact action sentence
    regions.append({
        "id": "highest_impact_action",
        "instruction": "1 sentence on the single biggest lever the team should pull.",
        "max_chars": 400,
    })

    return {
        "regions": regions,
        "response_schema": {
            "type": "object",
            "required": ["prose_fills"],
            "properties": {
                "prose_fills": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Synthesize: rank, score, slot-fill prep.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    evidence_path = audit_dir / "evidence" / "evidence.json"
    facts_path = audit_dir / "facts" / "audit_facts.json"
    audit_meta_path = audit_dir / "facts" / "audit_meta.json"
    report_dir = audit_dir / "report"

    if not evidence_path.is_file():
        print(f"ERROR: evidence.json not found: {evidence_path}", file=sys.stderr)
        return 2
    report_dir.mkdir(parents=True, exist_ok=True)

    evidence = load_json(evidence_path)
    facts = load_json(facts_path) if facts_path.is_file() else {}
    audit_meta = load_json(audit_meta_path) if audit_meta_path.is_file() else {}

    # Collect REAL findings only. Exclude store-layer findings from the
    # perf-side lead lists — they drive a separate publishing verdict and
    # render in their own section (see _build_store_readiness_table).
    real_findings: list[dict] = []
    for items in evidence.get("findings_by_rule", {}).values():
        for f in items:
            if f.get("verdict") != "REAL":
                continue
            if f.get("layer") == "store":
                continue
            real_findings.append(f)

    # Dedupe across layers
    leads = dedupe_lead_findings(real_findings)

    coverage = _compute_coverage(audit_dir, evidence, facts)

    slots = build_slot_map(args.audit_id, audit_meta, facts, evidence, leads)
    prose_spec = build_prose_regions_spec(slots)

    # Persist the slot-filled structure and the prose spec for the LLM to consume
    synthesis_input = {
        "slots": slots,
        "prose_spec": prose_spec,
        "facts": facts,
    }
    (report_dir / "synthesis_input.json").write_text(json.dumps(synthesis_input, indent=2), encoding="utf-8")

    # Also produce a partially-populated report.json with the deterministic
    # fields so render_report.py can pick up immediately and produce a stub
    # report even without an LLM round-trip (useful for CI / smoke tests).
    report_json = {
        "audit_id": args.audit_id,
        "audit_date": slots["audit_date"],
        "overall_score": slots["overall_score"],
        "verdict": slots["verdict"],
        "verdict_emoji": slots["verdict_emoji"],
        "project_signature": (facts.get("project_signature") or {}),
        "severity_totals": {
            "critical": slots["critical_count"],
            "high":     slots["high_count"],
            "low":      slots["low_count"],
        },
        "per_category_rows": slots["per_category_rows"],
        "per_category_scores": slots["per_category_scores"],
        "top_n_list": slots["top_n_list"],
        "lead_findings_by_priority": {
            priority: [
                {
                    "id": f.get("id"),
                    "title": f.get("title"),
                    "description": f.get("description"),
                    "category": f.get("category"),
                    "severity": f.get("severity"),
                    "confidence": f.get("confidence"),
                    "layer": f.get("layer"),
                    "location": {
                        "file": (f.get("evidence") or {}).get("file"),
                        "function": (f.get("evidence") or {}).get("function"),
                        "line": (f.get("evidence") or {}).get("line"),
                    },
                    "code_snippet": (f.get("evidence") or {}).get("code_snippet") or "",
                    "metric_name":      (f.get("evidence") or {}).get("metric_name"),
                    "metric_value":     (f.get("evidence") or {}).get("metric_value"),
                    "metric_threshold": (f.get("evidence") or {}).get("metric_threshold"),
                    "count_label": slots["count_labels"].get(f.get("id"), ""),
                    "related_finding_ids": f.get("related_finding_ids", []),
                    "top_n": (f.get("__synth_meta") or {}).get("top_n", False),
                    "has_fix_diff_slot": bool((f.get("__synth_meta") or {}).get("source_for_fix")),
                }
                for f in slots["by_priority"].get(priority, [])
            ]
            for priority in ("CRITICAL", "HIGH", "LOW")
        },
        "working_well_rows": slots["working_well_rows"],
        "metrics_dashboard": slots["metrics_dashboard"],
        "bundle_table": slots.get("bundle_table"),
        "device_metrics_table": slots.get("device_metrics_table"),
        "device_lighthouse": slots.get("device_lighthouse"),
        "device_lighthouse_ios": slots.get("device_lighthouse_ios"),
        "publishing_verdict": slots.get("publishing_verdict"),
        "store_readiness_table": slots.get("store_readiness_table"),
        "total_real_findings_count": slots["total_real_findings_count"],
        "total_issue_types": slots["total_issue_types"],
        "total_sites_count": slots["total_sites_count"],
        # Snapshot blocks fed by audit_facts.json so the renderer doesn't have
        # to open a second file. project_snapshot drives the new "Project
        # snapshot" section; codebase_snapshot drives the new "Codebase
        # snapshot" table; dependencies_snapshot drives a small extras block.
        "project_snapshot":  _project_snapshot_from_facts(facts),
        "codebase_snapshot": _codebase_snapshot_from_facts(facts),
        "dependencies_snapshot": _dependencies_snapshot_from_facts(facts),
        "coverage": coverage,
    }
    (report_dir / "report.json").write_text(json.dumps(report_json, indent=2), encoding="utf-8")

    print(f"Synthesis complete:", file=sys.stderr)
    print(f"  overall_score = {slots['overall_score']} ({slots['verdict']})", file=sys.stderr)
    print(f"  severity totals — critical={slots['critical_count']} high={slots['high_count']} low={slots['low_count']}", file=sys.stderr)
    print(f"  lead findings = {sum(len(v) for v in slots['by_priority'].values())}", file=sys.stderr)
    print(f"  synthesis_input → {report_dir / 'synthesis_input.json'}", file=sys.stderr)
    print(f"  report.json    → {report_dir / 'report.json'}", file=sys.stderr)
    print(f"  next step: LLM reads synthesis_input.json + prompts/synthesize.md → writes prose_fills.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
