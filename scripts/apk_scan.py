#!/usr/bin/env python3
"""
APK scan — exact shipped-artifact sizes from a real build.

When infra provides a real (EAS / cloud) APK, this extracts the *exact* bytes
that ship, which the pod-side `expo export` can't give us (the pod can't run
the correct-arch Hermes compiler). An APK is just a zip; we read it without
installing anything.

What it measures:
  - assets/index.android.bundle   — the shipped JS bundle (Hermes bytecode on a
                                    Hermes build). This is the real bundle size.
  - lib/<abi>/*.so                — native libraries (Hermes engine, Reanimated,
                                    SVG, etc.) — usually the biggest slice of an
                                    RN APK's install footprint.
  - classes*.dex                  — compiled Java/Kotlin.
  - res/ + resources.arsc         — Android resources.
  - assets/ (fonts, images, json) — other static assets.
  - total APK size                — the download/install footprint.

What it deliberately does NOT do:
  - per-dependency JS attribution — the shipped bundle is bytecode, which
    source-map-explorer can't map back to npm modules. That breakdown comes
    from `bundle_scan.py` (JS bundle + sourcemap). The two are complementary.

Findings emitted (layer = "bundle"):
  - bundle.shipped_bundle_size      — informational, always (the real number)
  - bundle.bundle_too_large_warning / _critical — if the shipped JS bundle is
    over threshold (same thresholds as bundle_scan, applied to the real artifact)
  - bundle.apk_install_footprint    — informational total + breakdown

Usage:
  python3 scripts/apk_scan.py <audit_id> <path-to.apk>
  python3 scripts/apk_scan.py <audit_id> <path.apk> --platform android
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

KIB = 1024
MIB = 1024 * 1024
BUNDLE_WARN_BYTES = 2 * MIB
BUNDLE_CRITICAL_BYTES = 4 * MIB


def human_bytes(n: int) -> str:
    if n >= MIB:
        return f"{n / MIB:.2f} MiB"
    if n >= KIB:
        return f"{n / KIB:.1f} KiB"
    return f"{n} B"


def _finding(rule_id, *, category, severity, confidence, title, description,
             file_path="", metric_name="", metric_value=None, metric_threshold=None,
             code_snippet="") -> dict:
    ev: dict = {"file": file_path or "<apk>", "function": "<apk>"}
    if metric_name:
        ev["metric_name"] = metric_name
    if metric_value is not None:
        ev["metric_value"] = metric_value
    if metric_threshold is not None:
        ev["metric_threshold"] = metric_threshold
    if code_snippet:
        ev["code_snippet"] = code_snippet
    return {
        "id": rule_id, "layer": "bundle", "category": category,
        "severity": severity, "confidence": confidence,
        "title": title, "description": description, "evidence": ev,
    }


def categorize(name: str) -> str:
    n = name.lower()
    if n.endswith(".bundle") or "index.android.bundle" in n:
        return "js_bundle"
    if n.startswith("lib/") and n.endswith(".so"):
        return "native_libs"
    if n.endswith(".dex"):
        return "dex"
    if n.startswith("res/") or n.endswith("resources.arsc"):
        return "resources"
    if n.startswith("assets/"):
        return "assets"
    if n.startswith("meta-inf/"):
        return "meta_inf"
    return "other"


def scan_apk(apk_path: Path) -> dict:
    """Return per-category uncompressed sizes + the JS bundle size + total."""
    by_category: dict[str, int] = defaultdict(int)
    js_bundle_bytes = 0
    js_bundle_name = ""
    native_libs: list[tuple[str, int]] = []
    total_uncompressed = 0
    total_compressed = 0
    with zipfile.ZipFile(apk_path) as z:
        for info in z.infolist():
            size = info.file_size
            csize = info.compress_size
            total_uncompressed += size
            total_compressed += csize
            cat = categorize(info.filename)
            by_category[cat] += size
            if cat == "js_bundle":
                js_bundle_bytes += size
                js_bundle_name = info.filename
            elif cat == "native_libs":
                native_libs.append((info.filename, size))
    native_libs.sort(key=lambda kv: -kv[1])
    return {
        "apk_total_on_disk": apk_path.stat().st_size,
        "apk_total_compressed_entries": total_compressed,
        "apk_total_uncompressed": total_uncompressed,
        "js_bundle_bytes": js_bundle_bytes,
        "js_bundle_name": js_bundle_name,
        "by_category": dict(by_category),
        "top_native_libs": native_libs[:12],
    }


def build_findings(scan: dict, platform: str) -> list[dict]:
    out: list[dict] = []
    apk_total = scan["apk_total_on_disk"]
    js = scan["js_bundle_bytes"]
    cats = scan["by_category"]

    # 1) Exact shipped JS bundle size — informational + threshold check.
    if js > 0:
        breakdown = " · ".join(
            f"{k}: {human_bytes(v)}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])
        )
        out.append(_finding(
            "bundle.shipped_bundle_size",
            category="bundle_size", severity="info", confidence="high",
            title=f"Shipped {platform} JS bundle is {human_bytes(js)} (from APK)",
            description=(
                f"The actual shipped JS bundle (`{scan['js_bundle_name']}`, Hermes bytecode on a Hermes build) "
                f"is {human_bytes(js)} — measured directly from the provided APK, so this is the real shipped size, "
                f"not a pre-Hermes proxy. Full install breakdown: {breakdown}."
            ),
            file_path=scan["js_bundle_name"],
            metric_name="shipped_js_bundle_bytes", metric_value=js,
        ))
        if js >= BUNDLE_CRITICAL_BYTES:
            out.append(_finding(
                "bundle.bundle_too_large_critical",
                category="bundle_size", severity="critical", confidence="high",
                title=f"Shipped {platform} bundle is {human_bytes(js)}",
                description=(
                    f"The shipped {platform} JS bundle is {human_bytes(js)}, over the 4 MiB critical threshold. "
                    "On a Hermes app this drives memory + bytecode-load time + OTA-update payload. "
                    "Audit dependencies, remove unused libraries, lazy-load non-first screens."
                ),
                file_path=scan["js_bundle_name"],
                metric_name="shipped_js_bundle_bytes", metric_value=js,
                metric_threshold=BUNDLE_CRITICAL_BYTES,
            ))
        elif js >= BUNDLE_WARN_BYTES:
            out.append(_finding(
                "bundle.bundle_too_large_warning",
                category="bundle_size", severity="medium", confidence="high",
                title=f"Shipped {platform} bundle is {human_bytes(js)}",
                description=(
                    f"The shipped {platform} JS bundle is {human_bytes(js)}, over the 2 MiB recommended ceiling. "
                    "On a Hermes app this drives memory + bytecode-load time + OTA-update payload; trim before 4 MiB."
                ),
                file_path=scan["js_bundle_name"],
                metric_name="shipped_js_bundle_bytes", metric_value=js,
                metric_threshold=BUNDLE_WARN_BYTES,
            ))

    # 2) Install footprint — informational. Native libs often dominate.
    lib_lines = "; ".join(f"{Path(n).name} {human_bytes(s)}" for n, s in scan["top_native_libs"][:6])
    out.append(_finding(
        "bundle.apk_install_footprint",
        category="bundle_size", severity="info", confidence="high",
        title=f"{platform} APK is {human_bytes(apk_total)} on disk",
        description=(
            f"Total APK (download/install footprint) is {human_bytes(apk_total)}. "
            f"Native libraries total {human_bytes(cats.get('native_libs', 0))}"
            + (f" — largest: {lib_lines}." if lib_lines else ".")
            + " Native libs are often the biggest slice of an RN install; "
            "per-ABI splits (App Bundles / `expo-build-properties` abiFilters) cut the user's download."
        ),
        file_path="<apk>",
        metric_name="apk_total_bytes", metric_value=apk_total,
    ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure exact shipped sizes from a real APK.")
    ap.add_argument("audit_id")
    ap.add_argument("apk", type=Path)
    ap.add_argument("--platform", default="android")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    findings_dir = audit_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    if not args.apk.is_file():
        out = [_finding(
            "tooling.apk_missing", category="tooling_error", severity="low", confidence="high",
            title="APK not found — shipped-size scan skipped",
            description=f"Expected an APK at {args.apk}.", file_path=str(args.apk),
        )]
        (findings_dir / "apk.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"apk_scan: APK missing at {args.apk}", file=sys.stderr)
        return 0

    try:
        scan = scan_apk(args.apk)
    except zipfile.BadZipFile:
        out = [_finding(
            "tooling.apk_invalid", category="tooling_error", severity="low", confidence="high",
            title="Provided APK is not a valid zip",
            description=f"{args.apk} could not be opened as an APK/zip.", file_path=str(args.apk),
        )]
        (findings_dir / "apk.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        print("apk_scan: bad APK", file=sys.stderr)
        return 0

    findings = build_findings(scan, args.platform)
    (findings_dir / "apk.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    # Also persist the raw scan for diagnostics / the report's bundle table.
    (audit_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (audit_dir / "artifacts" / "apk_scan.json").write_text(json.dumps(scan, indent=2, default=str), encoding="utf-8")

    print(f"apk_scan: shipped JS bundle {human_bytes(scan['js_bundle_bytes'])}, "
          f"APK total {human_bytes(scan['apk_total_on_disk'])} → {len(findings)} findings", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
