#!/usr/bin/env python3
"""
Build the synthetic iOS IPA fixture used to regression-test `scripts/ipa_scan.py`.

The IPA is not committed to the repo (it's a binary the script can rebuild from
the spec below in under a second). To regenerate it before running a test:

    python3 test-fixture/build_ios_fixture.py

Produces `test-fixture/ios-fixture/fixture.ipa`. The fixture plants exactly
these rule IDs:

    Rule                                          Why it fires
    --------------------------------------------  ---------------------------------
    bundle.shipped_bundle_size_ios                always (informational)
    bundle.bundle_too_large_warning_ios           shipped JS bundle ≥ 2 MiB
    bundle.ipa_install_footprint                  always (informational)
    bundle.ipa_native_framework_count             1 framework planted
    bundle.ipa_privacy_manifest_missing           PrivacyInfo.xcprivacy intentionally omitted

These match the rules in the "iOS IPA scan" section of `expected.md`.
"""
from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import zipfile
from pathlib import Path


def build(ipa_path: Path) -> None:
    ipa_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        app_dir = Path(tmp) / "Payload" / "Astrova.app"
        app_dir.mkdir(parents=True)

        # Info.plist — minimal valid set. Intentionally omits
        # ITSAppUsesNonExemptEncryption (the audit's other rules surface this
        # via the app.json store-readiness check, not the IPA scan).
        info = {
            "CFBundleIdentifier":         "app.emergent.astrovademo",
            "CFBundleShortVersionString": "1.2.5",
            "CFBundleVersion":            "1250",
            "MinimumOSVersion":           "15.1",
            "UIBackgroundModes":          ["audio", "remote-notification"],
            "NSMicrophoneUsageDescription": "Allow voice messages to astrologers",
            # Intentional: no NSCameraUsageDescription / NSPhotoLibraryUsageDescription.
        }
        (app_dir / "Info.plist").write_bytes(plistlib.dumps(info))

        # Main binary — Mach-O 64-bit magic + padding so the scanner's
        # architecture detector reports 64-bit (arm64-or-x86_64).
        macho_64 = b"\xcf\xfa\xed\xfe" + b"\x00" * 60 + b"binary-content" * 200
        (app_dir / "Astrova").write_bytes(macho_64)

        # Shipped JS bundle (Expo Router layout) — sized into the WARNING band
        # (~3.2 MiB > 2 MiB warning threshold, < 4 MiB critical).
        expo_dir = app_dir / "_expo" / "static" / "js" / "ios"
        expo_dir.mkdir(parents=True)
        (expo_dir / "index-abc123.hbc").write_bytes(b"X" * (3 * 1024 * 1024 + 200_000))

        # One native framework so the framework-count rule fires.
        fw = app_dir / "Frameworks" / "Hermes.framework"
        fw.mkdir(parents=True)
        (fw / "Hermes").write_bytes(b"\xcf\xfa\xed\xfe" + b"Y" * (2 * 1024 * 1024))

        # Intentional: no PrivacyInfo.xcprivacy → triggers
        # bundle.ipa_privacy_manifest_missing (HIGH).

        # Provisioning profile — minimal CMS wrapper around an XML plist.
        prov_plist = {
            "AppIDName":               "AstrovaDemo",
            "TeamIdentifier":          ["TEAM1"],
            "Entitlements": {
                "aps-environment":        "production",
                "application-identifier": "TEAM1.app.emergent.astrovademo",
            },
        }
        cms_wrapper = (
            b"\x30\x82\x01\x00\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02\xa0\x82"
            + plistlib.dumps(prov_plist)
            + b"\x00\x00"
        )
        (app_dir / "embedded.mobileprovision").write_bytes(cms_wrapper)

        # Zip the Payload/ tree into the IPA.
        with zipfile.ZipFile(ipa_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(Path(tmp)):
                for f in files:
                    full = Path(root) / f
                    zf.write(full, full.relative_to(tmp))

    print(f"built fixture IPA: {ipa_path} ({ipa_path.stat().st_size} bytes)")


def main() -> int:
    here = Path(__file__).resolve().parent
    ipa_path = here / "ios-fixture" / "fixture.ipa"
    build(ipa_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
