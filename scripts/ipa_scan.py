#!/usr/bin/env python3
"""
Stage 4c (iOS) — IPA scanner.

Analog of `apk_scan.py` for iOS. Reads a `.ipa` (a zip with `Payload/<App>.app/`
inside) and emits Findings + an artefact JSON describing what's actually
shipping. Cross-platform: works on Windows / Linux / macOS using only the
Python stdlib (`zipfile`, `plistlib`). On macOS, additionally runs `lipo` and
`codesign` for richer detail; on other hosts those fields are left blank.

What it surfaces (Astrova class app):
  - Shipped JS bundle size (`main.jsbundle` or Expo-style `_expo/static/js/ios/*.hbc`)
  - App total install footprint (sum of Payload contents, uncompressed)
  - Native framework count + total framework bytes (`Frameworks/*.framework`)
  - Architectures present (`lipo -info` on Apple Silicon; falls back to `Mach-O magic` heuristic)
  - PrivacyInfo.xcprivacy presence (Apple's May-2024 requirement)
  - Provisioning profile: aps-environment, application-identifier, capability flags
  - Info.plist key extraction: CFBundleIdentifier, CFBundleShortVersionString,
    CFBundleVersion, MinimumOSVersion, NS*UsageDescription presence, UIBackgroundModes,
    ITSAppUsesNonExemptEncryption, NSAppTransportSecurity flags

Findings emitted (layer = `bundle` to slot under Bundle composition):
  - bundle.shipped_bundle_size_ios          (always, informational)
  - bundle.bundle_too_large_warning_ios     (≥ 2 MiB)
  - bundle.bundle_too_large_critical_ios    (≥ 4 MiB)
  - bundle.ipa_install_footprint            (always, informational)
  - bundle.ipa_native_framework_count       (always, informational)
  - bundle.ipa_arch_32bit_only              (if no arm64 — Apple rejects)
  - bundle.ipa_privacy_manifest_missing     (when absent; Apple notices since May 2024)

Usage:
  python3 scripts/ipa_scan.py <audit_id> <path-to.ipa>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


# ── Constants ────────────────────────────────────────────────────────────────
JS_BUNDLE_CANDIDATES = (
    "main.jsbundle",                   # Bare RN / older Expo
    "main.jsbundle.hbc",                # Hermes bytecode (rare on iOS)
)
EXPO_JS_GLOB_PREFIX = "_expo/static/js/ios/"  # Expo-managed bundle location

BUNDLE_TOO_LARGE_WARNING_BYTES  = 2 * 1024 * 1024
BUNDLE_TOO_LARGE_CRITICAL_BYTES = 4 * 1024 * 1024

ARCH_MAGIC = {
    b"\xcf\xfa\xed\xfe": "arm64-or-x86_64",  # 64-bit Mach-O little-endian
    b"\xce\xfa\xed\xfe": "32-bit",            # 32-bit Mach-O — Apple rejects since 2018
    b"\xca\xfe\xba\xbe": "fat (universal)",   # Fat binary; needs lipo to break out
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def _emit(rule_id: str, severity: str, title: str, description: str,
          *, file: str = "", metric_name: str | None = None,
          metric_value: Any = None, metric_threshold: Any = None,
          confidence: str = "high") -> dict:
    ev: dict[str, Any] = {"file": file, "function": "<ipa>"}
    if metric_name is not None:
        ev["metric_name"] = metric_name
    if metric_value is not None:
        ev["metric_value"] = metric_value
    if metric_threshold is not None:
        ev["metric_threshold"] = metric_threshold
    return {
        "id": rule_id, "layer": "bundle", "category": "bundle_size",
        "severity": severity, "confidence": confidence,
        "title": title, "description": description,
        "evidence": ev,
    }


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MiB"
    if n >= 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n} B"


def _read_plist(blob: bytes) -> dict | None:
    """Parse a plist (binary or XML). Returns None on failure."""
    try:
        return plistlib.loads(blob)
    except Exception:
        return None


def _find_app_dir(extracted_root: Path) -> Path | None:
    payload = extracted_root / "Payload"
    if not payload.is_dir():
        return None
    for entry in payload.iterdir():
        if entry.is_dir() and entry.name.endswith(".app"):
            return entry
    return None


def _walk_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                continue
    return total


def _arch_from_magic(macho_path: Path) -> str:
    """Best-effort architecture identification from Mach-O magic bytes."""
    try:
        with macho_path.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return "unknown"
    return ARCH_MAGIC.get(head, "unknown")


def _lipo_archs(macho_path: Path) -> list[str]:
    """When on macOS, use `lipo -archs` to enumerate architectures (handles fat
    binaries cleanly). Empty list on non-macOS or lipo failure."""
    if shutil.which("lipo") is None:
        return []
    try:
        out = subprocess.run(
            ["lipo", "-archs", str(macho_path)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    return [t for t in out.stdout.strip().split() if t]


def _detect_main_binary_arches(app_dir: Path) -> list[str]:
    """Architectures the main app binary supports. App's main binary lives at
    `<App>.app/<App_name_without_extension>`."""
    name = app_dir.stem  # 'Foo.app' -> 'Foo'
    main_bin = app_dir / name
    if not main_bin.is_file():
        # Fall back: find the largest Mach-O at the app root.
        candidates = []
        for entry in app_dir.iterdir():
            if entry.is_file() and not entry.suffix:
                candidates.append(entry)
        if not candidates:
            return []
        main_bin = max(candidates, key=lambda p: p.stat().st_size)
    archs = _lipo_archs(main_bin)
    if archs:
        return archs
    # Fallback: read magic bytes
    return [_arch_from_magic(main_bin)]


def _detect_shipped_js_bundle(app_dir: Path) -> tuple[Path | None, int]:
    """Return (path, size) of the shipped JS bundle, or (None, 0)."""
    # Bare RN / older Expo
    for cand in JS_BUNDLE_CANDIDATES:
        p = app_dir / cand
        if p.is_file():
            try:
                return p, p.stat().st_size
            except OSError:
                pass
    # Expo Router managed shape: _expo/static/js/ios/index-<hash>.hbc
    expo_dir = app_dir / "_expo" / "static" / "js" / "ios"
    if expo_dir.is_dir():
        best: tuple[Path | None, int] = (None, 0)
        for entry in expo_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size > best[1]:
                best = (entry, size)
        if best[0] is not None:
            return best
    return None, 0


def _list_native_frameworks(app_dir: Path) -> tuple[int, int]:
    """Return (count, total_bytes) of .framework directories under Frameworks/."""
    fw_root = app_dir / "Frameworks"
    if not fw_root.is_dir():
        return 0, 0
    count = 0
    total = 0
    for entry in fw_root.iterdir():
        if entry.is_dir() and entry.suffix == ".framework":
            count += 1
            total += _walk_size(entry)
    return count, total


def _parse_provisioning_profile(profile_path: Path) -> dict | None:
    """`embedded.mobileprovision` is a CMS-signed plist. We use `security
    cms` on macOS; otherwise extract the embedded XML plist by string matching
    (the plist is plaintext inside the CMS wrapper)."""
    if not profile_path.is_file():
        return None
    try:
        blob = profile_path.read_bytes()
    except OSError:
        return None
    # Try macOS-native path first
    if sys.platform == "darwin" and shutil.which("security") is not None:
        try:
            out = subprocess.run(
                ["security", "cms", "-D", "-i", str(profile_path)],
                capture_output=True, timeout=10,
            )
            if out.returncode == 0:
                return _read_plist(out.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Cross-platform fallback: pull the embedded plist out of the CMS wrapper.
    start = blob.find(b"<?xml")
    end   = blob.find(b"</plist>")
    if start == -1 or end == -1:
        return None
    return _read_plist(blob[start:end + len(b"</plist>")])


# ── Main scan ────────────────────────────────────────────────────────────────
def scan_ipa(ipa_path: Path) -> tuple[dict, list[dict]]:
    """Extract + analyse. Returns (artefact_summary, findings)."""
    findings: list[dict] = []
    summary: dict[str, Any] = {
        "ipa_path": str(ipa_path),
        "ipa_size_bytes": ipa_path.stat().st_size,
        "scanned_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host_platform": sys.platform,
    }

    with tempfile.TemporaryDirectory(prefix="ipa_scan_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        with zipfile.ZipFile(ipa_path) as zf:
            zf.extractall(tmpdir)
        app_dir = _find_app_dir(tmpdir)
        if app_dir is None:
            findings.append(_emit(
                "tooling.ipa_no_app_payload", "low",
                "IPA does not contain a Payload/*.app directory",
                "The IPA may be malformed or not a real iOS app archive.",
                file=str(ipa_path), confidence="high",
            ))
            return summary, findings

        summary["app_name"] = app_dir.stem

        # ── Info.plist
        info_plist = _read_plist((app_dir / "Info.plist").read_bytes()) if (app_dir / "Info.plist").is_file() else {}
        info_keys = {
            "CFBundleIdentifier":         info_plist.get("CFBundleIdentifier"),
            "CFBundleShortVersionString": info_plist.get("CFBundleShortVersionString"),
            "CFBundleVersion":            info_plist.get("CFBundleVersion"),
            "MinimumOSVersion":           info_plist.get("MinimumOSVersion"),
            "ITSAppUsesNonExemptEncryption": info_plist.get("ITSAppUsesNonExemptEncryption"),
            "UIBackgroundModes":          info_plist.get("UIBackgroundModes") or [],
        }
        # NS*UsageDescription keys actually present
        ns_keys = sorted(k for k in info_plist if isinstance(k, str) and k.startswith("NS") and "UsageDescription" in k)
        info_keys["NS_usage_description_keys"] = ns_keys
        # ATS
        ats = info_plist.get("NSAppTransportSecurity") or {}
        info_keys["NSAllowsArbitraryLoads"] = ats.get("NSAllowsArbitraryLoads") if isinstance(ats, dict) else None
        summary["info_plist"] = info_keys

        # ── Architectures
        archs = _detect_main_binary_arches(app_dir)
        summary["architectures"] = archs
        if archs and not any(("arm64" in a) for a in archs):
            findings.append(_emit(
                "bundle.ipa_arch_32bit_only", "critical",
                "iOS binary does not include arm64",
                "Apple has required 64-bit (arm64) binaries since 2018. Without arm64 the app cannot be submitted.",
                file=f"Payload/{app_dir.name}/{app_dir.stem}",
                metric_name="architectures", metric_value=",".join(archs) or "unknown",
            ))

        # ── Shipped JS bundle
        bundle_path, bundle_bytes = _detect_shipped_js_bundle(app_dir)
        summary["shipped_js_bundle"] = {
            "path": str(bundle_path.relative_to(tmpdir)) if bundle_path is not None else None,
            "bytes": bundle_bytes,
            "pretty": _human(bundle_bytes) if bundle_bytes else None,
        }
        if bundle_path is not None and bundle_bytes > 0:
            findings.append(_emit(
                "bundle.shipped_bundle_size_ios", "low",
                f"Shipped iOS JS bundle is {_human(bundle_bytes)} (from IPA)",
                f"`{bundle_path.relative_to(tmpdir)}` in the IPA payload measures {_human(bundle_bytes)} ({bundle_bytes:,} bytes). This is the actual bytes the device will load at cold start.",
                file=f"Payload/{app_dir.name}/{bundle_path.relative_to(app_dir)}",
                metric_name="shipped_js_bundle_bytes", metric_value=bundle_bytes,
            ))
            if bundle_bytes >= BUNDLE_TOO_LARGE_CRITICAL_BYTES:
                findings.append(_emit(
                    "bundle.bundle_too_large_critical_ios", "critical",
                    f"Shipped iOS bundle is {_human(bundle_bytes)} (over 4 MiB)",
                    f"The shipped iOS JS bundle is {_human(bundle_bytes)}, past the 4 MiB critical ceiling. Trim before submission.",
                    file=f"Payload/{app_dir.name}/{bundle_path.relative_to(app_dir)}",
                    metric_name="shipped_js_bundle_bytes",
                    metric_value=bundle_bytes,
                    metric_threshold=BUNDLE_TOO_LARGE_CRITICAL_BYTES,
                ))
            elif bundle_bytes >= BUNDLE_TOO_LARGE_WARNING_BYTES:
                findings.append(_emit(
                    "bundle.bundle_too_large_warning_ios", "medium",
                    f"Shipped iOS bundle is {_human(bundle_bytes)}",
                    f"The shipped iOS JS bundle is {_human(bundle_bytes)}, over the 2 MiB recommended ceiling. Cold-start load time and memory both scale with this.",
                    file=f"Payload/{app_dir.name}/{bundle_path.relative_to(app_dir)}",
                    metric_name="shipped_js_bundle_bytes",
                    metric_value=bundle_bytes,
                    metric_threshold=BUNDLE_TOO_LARGE_WARNING_BYTES,
                ))

        # ── App install footprint
        app_bytes = _walk_size(app_dir)
        summary["app_install_bytes"] = app_bytes
        findings.append(_emit(
            "bundle.ipa_install_footprint", "low",
            f"iOS app is {_human(app_bytes)} on disk (uncompressed)",
            f"`Payload/{app_dir.name}/` totals {_human(app_bytes)} ({app_bytes:,} bytes). Smaller is better for App Store download size + install footprint.",
            file=f"Payload/{app_dir.name}/",
            metric_name="ipa_install_bytes", metric_value=app_bytes,
        ))

        # ── Native frameworks
        fw_count, fw_bytes = _list_native_frameworks(app_dir)
        summary["native_frameworks"] = {"count": fw_count, "bytes": fw_bytes, "pretty": _human(fw_bytes)}
        if fw_count > 0:
            findings.append(_emit(
                "bundle.ipa_native_framework_count", "low",
                f"{fw_count} native framework(s) bundled ({_human(fw_bytes)})",
                f"`Payload/{app_dir.name}/Frameworks/` contains {fw_count} .framework bundle(s), totalling {_human(fw_bytes)}. Each native framework is loaded at cold start.",
                file=f"Payload/{app_dir.name}/Frameworks/",
                metric_name="framework_count", metric_value=fw_count,
            ))

        # ── PrivacyInfo.xcprivacy
        privacy_path = app_dir / "PrivacyInfo.xcprivacy"
        summary["privacy_manifest_present"] = privacy_path.is_file()
        if not privacy_path.is_file():
            findings.append(_emit(
                "bundle.ipa_privacy_manifest_missing", "high",
                "iOS PrivacyInfo.xcprivacy is missing from the IPA",
                (
                    "Apple has been sending notices for missing privacy manifests since May 2024. "
                    "Detected SDKs that collect data (Firebase, IAP, etc.) require this file to be present at the app root, "
                    "describing required-reason APIs and data-collection categories. Generate one via the privacy manifest tool "
                    "(Xcode 15+) or manually before submission."
                ),
                file=f"Payload/{app_dir.name}/PrivacyInfo.xcprivacy",
            ))

        # ── Provisioning profile (informational)
        prov = _parse_provisioning_profile(app_dir / "embedded.mobileprovision")
        if prov:
            entitlements = prov.get("Entitlements") or {}
            summary["provisioning_profile"] = {
                "AppIDName":               prov.get("AppIDName"),
                "TeamIdentifier":          prov.get("TeamIdentifier"),
                "TeamName":                prov.get("TeamName"),
                "CreationDate":            str(prov.get("CreationDate")) if prov.get("CreationDate") else None,
                "ExpirationDate":          str(prov.get("ExpirationDate")) if prov.get("ExpirationDate") else None,
                "aps-environment":         entitlements.get("aps-environment"),
                "application-identifier":  entitlements.get("application-identifier"),
                "get-task-allow":          entitlements.get("get-task-allow"),
            }

    return summary, findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan an iOS IPA for perf/store signals.")
    ap.add_argument("audit_id")
    ap.add_argument("ipa_path", type=Path)
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    artifacts_dir = audit_dir / "artifacts"
    findings_dir = audit_dir / "findings"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    findings_dir.mkdir(parents=True, exist_ok=True)

    if not args.ipa_path.is_file():
        print(f"ERROR: IPA not found: {args.ipa_path}", file=sys.stderr)
        return 2

    summary, findings = scan_ipa(args.ipa_path)
    (artifacts_dir / "ipa_scan.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (findings_dir / "ipa.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    sev_count = lambda s: sum(1 for f in findings if f["severity"] == s)
    print(
        f"ipa scan: {len(findings)} findings "
        f"({sev_count('critical')} critical, {sev_count('high')} high, "
        f"{sev_count('medium')} medium, {sev_count('low')} low)  "
        f"app={summary.get('app_name')} archs={summary.get('architectures')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
