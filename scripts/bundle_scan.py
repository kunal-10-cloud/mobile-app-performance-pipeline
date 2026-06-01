#!/usr/bin/env python3
"""
Stage 4b — Bundle scan.

Builds the production JS bundle via `npx expo export`, then runs
`source-map-explorer` over the result to attribute bytes to modules. Also
walks `workspace/assets/` to size up image / font / other static assets.

Rules applied (deterministic thresholds — verbatim from architecture.md §4b):

  Bundle-level:
    - bundle_too_large_warning   total JS > 2 MiB (per platform)
    - bundle_too_large_critical  total JS > 4 MiB (per platform)
  Dependency-level (via source-map-explorer):
    - dependency_oversized       one module > 100 KiB
    - duplicate_dependency_libs  two libraries with overlapping APIs (lodash + lodash-es, etc.)
    - known_bloated_dependency   moment, jquery, full-lodash, etc. — with lighter alternatives
  Asset-level:
    - asset_image_too_large      any image > 500 KiB
    - png_image_could_be_webp    PNG > 100 KiB that WebP would shrink ≥ 50 %
    - asset_total_too_large      cumulative non-image assets > 5 MiB

Output:
  ${AUDIT_DIR}/findings/bundle.json
  ${AUDIT_DIR}/artifacts/bundle/   — the raw export, kept for diagnostics

Fail-soft: if `expo export` fails, the script writes a single
tooling.bundle_export_failed finding and exits 0 so the rest of the pipeline
continues.

Usage:
  python3 scripts/bundle_scan.py <audit_id>
  python3 scripts/bundle_scan.py <audit_id> --skip-export  # use existing bundle
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Thresholds (bytes) ───────────────────────────────────────────────────────
KIB = 1024
MIB = 1024 * 1024

BUNDLE_WARN_BYTES = 2 * MIB
BUNDLE_CRITICAL_BYTES = 4 * MIB
DEPENDENCY_OVERSIZED_BYTES = 100 * KIB
ASSET_IMAGE_LARGE_BYTES = 500 * KIB
PNG_WEBP_CANDIDATE_BYTES = 100 * KIB
ASSET_TOTAL_BUDGET_BYTES = 5 * MIB

# ── Known-bloated dependency map ─────────────────────────────────────────────
# module-name-fragment -> (severity, lighter alternative, why it matters)
KNOWN_BLOATED = {
    "moment":                  ("medium", "dayjs / date-fns", "Moment is 70+ KB gz; dayjs ships in ~2 KB with the same API surface."),
    "lodash":                  ("medium", "lodash-es / per-method imports", "Importing the full lodash bundle ships 70+ KB. Pull only what you use, or migrate to lodash-es with tree-shaking."),
    "axios":                   ("low",    "fetch", "Axios adds ~15 KB gz; React Native's built-in fetch covers most use cases."),
    "rxjs":                    ("medium", "AbortController / hand-rolled streams", "RxJS bundles ~40+ KB; on mobile, custom event emitters are usually enough."),
    "@babel/runtime":          ("info",   None, "Auto-included by Babel; flagged informationally if oversized."),
    "react-native-vector-icons": ("medium", "@expo/vector-icons", "When inside Expo, @expo/vector-icons is already bundled — double-loading icon sets bloats the JS bundle."),
    "jquery":                  ("high",   "DOM is unused on RN", "jQuery has no business in a React Native bundle. Remove the dep."),
    "underscore":              ("medium", "lodash-es / native methods", "Modern JS covers most underscore methods natively."),
}

# Pairs that signal duplicate-purpose installations.
DUPLICATE_PAIRS = [
    ({"lodash", "lodash-es"},                "Both full lodash variants are installed; ship only one."),
    ({"moment", "dayjs"},                    "Both date libraries installed; converge on one."),
    ({"moment", "date-fns"},                 "Both date libraries installed; converge on one."),
    ({"react-native-vector-icons", "@expo/vector-icons"}, "Two icon-set libraries installed; @expo/vector-icons supersedes vector-icons inside Expo."),
    ({"axios", "ky"},                        "Two HTTP client libraries installed; pick one."),
]

# ── File-system walks ────────────────────────────────────────────────────────

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp"}


def make_finding(
    rule_id: str,
    *,
    category: str,
    severity: str,
    confidence: str,
    title: str,
    description: str,
    file_path: str = "",
    function: str = "<bundle>",
    metric_name: str = "",
    metric_value: float | int | None = None,
    metric_threshold: float | int | None = None,
    code_snippet: str = "",
) -> dict:
    evidence: dict = {"file": file_path, "function": function}
    if metric_name:
        evidence["metric_name"] = metric_name
    if metric_value is not None:
        evidence["metric_value"] = metric_value
    if metric_threshold is not None:
        evidence["metric_threshold"] = metric_threshold
    if code_snippet:
        evidence["code_snippet"] = code_snippet
    return {
        "id": rule_id,
        "layer": "bundle",
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "description": description,
        "evidence": evidence,
    }


def human_bytes(n: int) -> str:
    if n >= MIB:
        return f"{n / MIB:.2f} MiB"
    if n >= KIB:
        return f"{n / KIB:.1f} KiB"
    return f"{n} B"


# ── Stage 1: run expo export ─────────────────────────────────────────────────

_HERMES_FAILURE_MARKERS = (
    "hermesc",
    "Failed to generate Hermes bytecode",
    "exited with non-zero code",
    "ELF",  # wrong-arch hermesc: "ELF...: not found"
)


def _run_export_once(workspace: Path, output_dir: Path, *, no_bytecode: bool) -> tuple[bool, str]:
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["npx", "--no-install", "expo", "export",
           "--platform", "all",
           "--output-dir", str(output_dir),
           "--source-maps"]
    if no_bytecode:
        cmd.append("--no-bytecode")
    try:
        r = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, timeout=1200)
    except FileNotFoundError:
        return False, "npx not on PATH"
    except subprocess.TimeoutExpired:
        return False, "expo export timed out after 20 minutes"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "")[-2000:]
    return True, ""


def run_expo_export(workspace: Path, output_dir: Path) -> tuple[bool, str, bool]:
    """Run `npx expo export`. Returns (ok, stderr_tail, used_no_bytecode).

    First tries a normal export (produces the real Hermes bytecode bundle).
    If that fails specifically at the Hermes step — which happens when the
    pod's bundled `hermesc` is the wrong CPU arch (x86-64 binary on an ARM
    pod) — retries with `--no-bytecode`. The no-bytecode bundle is the
    pre-Hermes minified JS: the correct, actionable proxy for bundle WEIGHT
    and the right input for per-dependency source-map attribution. The
    `used_no_bytecode` flag is propagated so findings can be labelled
    'pre-Hermes JS' rather than implying it's the shipped bytecode size."""
    ok, tail = _run_export_once(workspace, output_dir, no_bytecode=False)
    if ok:
        return True, "", False
    if any(m in tail for m in _HERMES_FAILURE_MARKERS):
        ok2, tail2 = _run_export_once(workspace, output_dir, no_bytecode=True)
        if ok2:
            return True, "fell back to --no-bytecode after hermesc failure", True
        return False, f"export failed with hermes, --no-bytecode also failed: {tail2}", True
    return False, tail, False


# ── Stage 2: inventory bundle output ─────────────────────────────────────────

def find_bundle_js_files(bundle_dir: Path) -> list[Path]:
    """Locate the platform JS bundles emitted by expo export.

    Expo export lays them out as:
      _expo/static/js/{android,ios}/index-<hash>.{hbc,js}
    or sometimes a flat `bundles/` directory on older SDKs.
    We accept any .js / .hbc directly under bundle_dir.
    """
    out: list[Path] = []
    for ext in (".js", ".hbc"):
        out.extend(bundle_dir.rglob(f"*{ext}"))
    # Filter out source maps & vendor noise
    return [p for p in out if not p.name.endswith(".map")]


def find_source_maps(bundle_dir: Path) -> dict[str, Path]:
    """Map JS-bundle-path -> sourcemap-path, when present."""
    out: dict[str, Path] = {}
    for p in bundle_dir.rglob("*.map"):
        # convention: foo.js + foo.js.map (or foo.hbc.map)
        target = str(p)[:-4]
        out[target] = p
    return out


# ── Stage 3: source-map-explorer ─────────────────────────────────────────────

def run_source_map_explorer(js_file: Path, map_file: Path) -> dict | None:
    """Run `npx source-map-explorer --json`. Returns parsed JSON or None on failure."""
    cmd = ["npx", "--no-install", "source-map-explorer",
           "--json", str(js_file), str(map_file)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    if not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def aggregate_modules_by_package(sme_output: dict) -> list[tuple[str, int]]:
    """Group source-map-explorer's per-file sizes by top-level npm package.

    Files inside `node_modules/<pkg>/...` are attributed to `<pkg>`; files
    inside `node_modules/@scope/<pkg>/...` are attributed to `@scope/<pkg>`.
    Application source files (anything not under node_modules) are bucketed
    under `__app__`.
    """
    # source-map-explorer's --json output: { "results": [{ "files": { "<path>": {"size":N,...}}}] }
    sizes: dict[str, int] = {}
    results = sme_output.get("results") or []
    for entry in results:
        files = entry.get("files", {}) or {}
        for path, info in files.items():
            size = info.get("size", 0) if isinstance(info, dict) else 0
            if not isinstance(size, (int, float)):
                continue
            sizes[_attribute_package(path)] = sizes.get(_attribute_package(path), 0) + int(size)
    return sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)


def _attribute_package(path: str) -> str:
    norm = path.replace("\\", "/")
    if "node_modules/" not in norm:
        return "__app__"
    after = norm.split("node_modules/", 1)[1]
    parts = after.split("/")
    if not parts:
        return "__app__"
    if parts[0].startswith("@") and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


# ── Stage 4: asset audit ─────────────────────────────────────────────────────

def scan_assets(workspace: Path) -> list[dict]:
    """Walk assets/ recursively, return [{rel_path, bytes, ext}]."""
    assets_dir = workspace / "assets"
    if not assets_dir.is_dir():
        return []
    out: list[dict] = []
    for p in assets_dir.rglob("*"):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append({
            "rel_path": str(p.relative_to(workspace)).replace("\\", "/"),
            "bytes": size,
            "ext": p.suffix.lower(),
        })
    return out


# ── Stage 5: apply rules ─────────────────────────────────────────────────────

def apply_bundle_rules(per_platform_sizes: dict[str, int], *, used_no_bytecode: bool = False) -> list[dict]:
    # When the size was measured from a --no-bytecode export, the number is the
    # pre-Hermes minified JS, not the shipped bytecode. Label it honestly and
    # frame impact for Hermes apps as memory/load/OTA weight, NOT raw parse cost
    # (Hermes precompiles, so the JS isn't re-parsed each launch).
    measure_note = (
        " (measured as pre-Hermes minified JS via --no-bytecode; a proxy for code/"
        "dependency weight, not the shipped bytecode or download size)"
        if used_no_bytecode else ""
    )
    impact_clause = (
        "On a Hermes app this weight costs memory, bytecode-load time, and OTA-update payload "
        "(not raw JS parse — Hermes precompiles). Audit the dependency list, remove unused "
        "libraries, and lazy-load non-first screens."
        if used_no_bytecode else
        "Bundle size dominates cold-start: every additional 100 KiB adds tens of ms of parse "
        "on low-end devices. Audit the dependency list and lazy-load screens via dynamic imports."
    )
    out: list[dict] = []
    for platform, size in per_platform_sizes.items():
        if size >= BUNDLE_CRITICAL_BYTES:
            out.append(make_finding(
                "bundle.bundle_too_large_critical",
                category="bundle_size",
                severity="critical",
                confidence="high",
                title=f"{platform} JS bundle is {human_bytes(size)}",
                description=(
                    f"The {platform} JS bundle is {human_bytes(size)}{measure_note}, exceeding the 4 MiB critical threshold. "
                    + impact_clause
                ),
                file_path=f"_expo/static/js/{platform}/",
                metric_name="bundle_bytes",
                metric_value=size,
                metric_threshold=BUNDLE_CRITICAL_BYTES,
            ))
        elif size >= BUNDLE_WARN_BYTES:
            out.append(make_finding(
                "bundle.bundle_too_large_warning",
                category="bundle_size",
                severity="medium",
                confidence="high",
                title=f"{platform} JS bundle is {human_bytes(size)}",
                description=(
                    f"The {platform} JS bundle is {human_bytes(size)}{measure_note}, over the 2 MiB recommended ceiling. "
                    + impact_clause
                ),
                file_path=f"_expo/static/js/{platform}/",
                metric_name="bundle_bytes",
                metric_value=size,
                metric_threshold=BUNDLE_WARN_BYTES,
            ))
    return out


# Mandatory framework / runtime packages — you can't remove these, so flagging
# them as "oversized" is noise. Per references.md §3 (bundle.dependency_oversized).
_FRAMEWORK_EXCLUDE = {
    "react", "react-dom", "react-native", "scheduler", "regenerator-runtime",
    "invariant", "nullthrows", "promise", "@babel/runtime", "@expo/metro-runtime",
    "metro-runtime", "expo", "expo-modules-core",
    "@react-native/virtualized-lists", "@react-native/normalize-colors",
    "@react-native/assets-registry", "@react-native/js-polyfills",
}


def _is_framework(pkg: str) -> bool:
    return pkg in _FRAMEWORK_EXCLUDE or pkg.startswith("@react-native/")


def apply_dependency_rules(per_package_sizes: list[tuple[str, int]]) -> list[dict]:
    out: list[dict] = []
    seen_pkgs: set[str] = set()
    for pkg, size in per_package_sizes:
        seen_pkgs.add(pkg)
        if pkg == "__app__" or _is_framework(pkg):
            continue
        # known-bloated
        for needle, (severity, alternative, why) in KNOWN_BLOATED.items():
            if needle in pkg:
                alt_clause = f" Consider {alternative}." if alternative else ""
                out.append(make_finding(
                    "bundle.known_bloated_dependency",
                    category="bundle_size",
                    severity=severity,
                    confidence="high",
                    title=f"Heavy dependency in bundle: {pkg} ({human_bytes(size)})",
                    description=f"{why}{alt_clause}",
                    file_path=f"node_modules/{pkg}/",
                    metric_name="bytes_in_bundle",
                    metric_value=size,
                ))
                break
        # oversized
        if size >= DEPENDENCY_OVERSIZED_BYTES:
            out.append(make_finding(
                "bundle.dependency_oversized",
                category="bundle_size",
                severity="medium" if size < 250 * KIB else "high",
                confidence="high",
                title=f"{pkg} contributes {human_bytes(size)} to the bundle",
                description=(
                    f"`{pkg}` accounts for {human_bytes(size)} of the JS bundle. "
                    "If only a subset is used, switch to per-method imports or tree-shakable ESM builds."
                ),
                file_path=f"node_modules/{pkg}/",
                metric_name="bytes_in_bundle",
                metric_value=size,
                metric_threshold=DEPENDENCY_OVERSIZED_BYTES,
            ))
    # duplicate pairs
    for pair, why in DUPLICATE_PAIRS:
        if pair.issubset(seen_pkgs):
            out.append(make_finding(
                "bundle.duplicate_dependency_libs",
                category="bundle_size",
                severity="medium",
                confidence="high",
                title=f"Duplicate-purpose libraries: {', '.join(sorted(pair))}",
                description=why,
                metric_name="duplicate_pair",
                code_snippet=", ".join(sorted(pair)),
            ))
    return out


def apply_asset_rules(assets: list[dict]) -> list[dict]:
    out: list[dict] = []
    non_image_total = 0
    for a in assets:
        if a["ext"] in IMAGE_EXTS:
            if a["bytes"] >= ASSET_IMAGE_LARGE_BYTES:
                out.append(make_finding(
                    "bundle.asset_image_too_large",
                    category="bundle_size",
                    severity="high" if a["bytes"] >= MIB else "medium",
                    confidence="high",
                    title=f"Image {a['rel_path']} is {human_bytes(a['bytes'])}",
                    description=(
                        f"`{a['rel_path']}` is {human_bytes(a['bytes'])}. "
                        "Images > 500 KiB block the bundle on first reference and consume memory on every screen they appear. "
                        "Resize to the largest in-app display dimension at 2x density and re-encode."
                    ),
                    file_path=a["rel_path"],
                    metric_name="image_bytes",
                    metric_value=a["bytes"],
                    metric_threshold=ASSET_IMAGE_LARGE_BYTES,
                ))
            if a["ext"] == ".png" and a["bytes"] >= PNG_WEBP_CANDIDATE_BYTES:
                out.append(make_finding(
                    "bundle.png_image_could_be_webp",
                    category="bundle_size",
                    severity="low",
                    confidence="medium",
                    title=f"PNG candidate for WebP conversion: {a['rel_path']}",
                    description=(
                        f"`{a['rel_path']}` is a {human_bytes(a['bytes'])} PNG. "
                        "WebP typically reduces size by 25–50% with no visible quality loss. "
                        "If transparency is required, WebP supports it; otherwise prefer JPEG-XL or JPEG."
                    ),
                    file_path=a["rel_path"],
                    metric_name="png_bytes",
                    metric_value=a["bytes"],
                ))
        else:
            non_image_total += a["bytes"]
    if non_image_total >= ASSET_TOTAL_BUDGET_BYTES:
        out.append(make_finding(
            "bundle.asset_total_too_large",
            category="bundle_size",
            severity="medium",
            confidence="high",
            title=f"Non-image assets total {human_bytes(non_image_total)}",
            description=(
                f"Non-image static assets (fonts, JSON, video, audio) sum to {human_bytes(non_image_total)}. "
                "Large static bundles inflate the install size and the over-the-air-update payload. "
                "Audit `assets/` and remove anything that isn't referenced at runtime."
            ),
            file_path="assets/",
            metric_name="asset_total_bytes",
            metric_value=non_image_total,
            metric_threshold=ASSET_TOTAL_BUDGET_BYTES,
        ))
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def consume_sme_mode(args, findings_dir: Path) -> int:
    """Hybrid path: the expensive `expo export --source-maps` + source-map-explorer
    ran ON THE POD (where node_modules lives); we were handed the resulting
    source-map-explorer JSON + the measured bundle byte count. Apply the same
    threshold + per-dependency rules agent-side. This gives full bundle
    composition without needing node_modules on the agent."""
    findings: list[dict] = []
    platform = args.platform or "android"
    bundle_bytes = args.bundle_bytes or 0
    used_no_bytecode = bool(args.no_bytecode)

    if bundle_bytes > 0:
        findings.extend(apply_bundle_rules({platform: bundle_bytes}, used_no_bytecode=used_no_bytecode))

    sme_path = Path(args.consume_sme)
    if sme_path.is_file():
        try:
            sme = json.loads(sme_path.read_text(encoding="utf-8"))
            aggregated = aggregate_modules_by_package(sme)
            findings.extend(apply_dependency_rules(aggregated))
        except Exception as e:
            findings.append(make_finding(
                "tooling.sme_json_parse_failed", category="tooling_error",
                severity="low", confidence="high",
                title="Could not parse source-map-explorer JSON",
                description=f"{type(e).__name__}: {e}", file_path=str(sme_path),
            ))
    out = findings_dir / "bundle.json"
    out.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"Bundle scan (consume-sme): {len(findings)} findings → {out}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run bundle + asset perf scan.")
    ap.add_argument("audit_id")
    ap.add_argument("--skip-export", action="store_true",
                    help="Reuse existing artifacts/bundle/ instead of running expo export.")
    ap.add_argument("--consume-sme", metavar="JSON",
                    help="Skip export; consume an on-pod-produced source-map-explorer JSON. "
                         "Pair with --bundle-bytes and --platform.")
    ap.add_argument("--bundle-bytes", type=int, default=0,
                    help="Measured bundle size in bytes (for --consume-sme threshold check).")
    ap.add_argument("--platform", default="android", help="Platform label for --consume-sme.")
    ap.add_argument("--no-bytecode", action="store_true",
                    help="Mark the supplied size as pre-Hermes JS (for --consume-sme labelling).")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    workspace = audit_dir / "workspace"
    artifacts_bundle = audit_dir / "artifacts" / "bundle"
    findings_dir = audit_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    # Hybrid consume mode — no local workspace/export needed.
    if args.consume_sme:
        return consume_sme_mode(args, findings_dir)

    if not workspace.is_dir():
        print(f"ERROR: workspace missing: {workspace}", file=sys.stderr)
        return 2

    findings: list[dict] = []
    used_no_bytecode = False

    # 1) Build bundle
    if not args.skip_export:
        ok, tail, used_no_bytecode = run_expo_export(workspace, artifacts_bundle)
        if not ok:
            findings.append(make_finding(
                "tooling.bundle_export_failed",
                category="tooling_error",
                severity="low",
                confidence="high",
                title="`expo export` failed; bundle audit skipped",
                description=f"Last stderr: {tail[:1200]}",
                file_path=str(artifacts_bundle),
            ))
            (findings_dir / "bundle.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
            print(f"Bundle scan: export failed; wrote {len(findings)} findings.", file=sys.stderr)
            return 0
        if used_no_bytecode:
            print("Bundle scan: used --no-bytecode fallback (hermesc arch mismatch).", file=sys.stderr)

    # 2) Per-platform bundle sizes
    per_platform: dict[str, int] = {}
    js_files = find_bundle_js_files(artifacts_bundle)
    for p in js_files:
        platform = _guess_platform(p, artifacts_bundle)
        per_platform[platform] = per_platform.get(platform, 0) + p.stat().st_size
    findings.extend(apply_bundle_rules(per_platform, used_no_bytecode=used_no_bytecode))

    # 3) Per-package attribution via source-map-explorer
    src_maps = find_source_maps(artifacts_bundle)
    aggregated: list[tuple[str, int]] = []
    for js in js_files:
        sm = src_maps.get(str(js))
        if sm is None:
            continue
        sme = run_source_map_explorer(js, sm)
        if sme is None:
            findings.append(make_finding(
                "tooling.source_map_explorer_failed",
                category="tooling_error",
                severity="low",
                confidence="high",
                title=f"source-map-explorer could not analyse {js.name}",
                description="Per-dependency attribution skipped for this bundle. Asset audit still ran.",
                file_path=str(js.relative_to(artifacts_bundle)),
            ))
            continue
        aggregated = _merge_size_lists(aggregated, aggregate_modules_by_package(sme))
    findings.extend(apply_dependency_rules(aggregated))

    # 4) Asset audit
    assets = scan_assets(workspace)
    findings.extend(apply_asset_rules(assets))

    out = findings_dir / "bundle.json"
    out.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"Bundle scan: {len(findings)} findings → {out}", file=sys.stderr)
    return 0


def _guess_platform(js_path: Path, bundle_root: Path) -> str:
    rel = str(js_path.relative_to(bundle_root)).replace("\\", "/").lower()
    if "/ios/" in rel or rel.startswith("ios/"):
        return "ios"
    if "/android/" in rel or rel.startswith("android/"):
        return "android"
    return "web" if "/web/" in rel else "unknown"


def _merge_size_lists(a: list[tuple[str, int]], b: list[tuple[str, int]]) -> list[tuple[str, int]]:
    d: dict[str, int] = dict(a)
    for k, v in b:
        d[k] = d.get(k, 0) + v
    return sorted(d.items(), key=lambda kv: kv[1], reverse=True)


if __name__ == "__main__":
    sys.exit(main())
