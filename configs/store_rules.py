"""
Store-publishing readiness rules — Stage 4e.

Each rule is `def rule_<name>(ctx: StoreCtx) -> list[dict]` returning Finding dicts
(schema: finding.schema.json with layer="store", category="publishing").

Conventions
-----------
- Rules read app.json (already merged with app.config.* by the worker), package.json,
  audit_facts.json, and a lightweight in-memory source index built by the worker.
  No tree-sitter — config + grep is sufficient for publish-readiness checks.

- Emergent customer apps run on Expo SDK 54. Where a setting is unset, treat the
  SDK 54 default as truth — that's how it actually ships. Rules only fire on
  *explicit* downgrades, missing values that aren't defaulted, or active
  misconfiguration.

- Severities follow the plan:
    CRITICAL → store will reject / build will fail.
    HIGH     → reviewer likely to flag, or the feature will silently break.
    MEDIUM   → recommended fix.
    LOW      → cosmetic / informational.

- Add a new rule:
    1. Write `rule_<name>(ctx) -> list[dict]` below.
    2. Add to APPLE_RULES / GOOGLE_RULES / CROSS_RULES / PROCESS_RULES at the bottom.
    3. Add deterministic-prose entries in render_report.py
       (DETERMINISTIC_ACTIONABLES / _AFTER_FIXING / _PLAIN_TERMS) keyed by rule_id.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sdk_disclosure_matrix import (
    SDKDisclosure,
    all_privacy_manifest_requirers,
    any_tracking_class,
    detect_disclosures,
)


# ── Emergent / Expo SDK 54 defaults ───────────────────────────────────────────
# These ARE the shipped values when the app.json field is omitted, so rules
# should treat absence as "uses default" rather than "missing", except where
# the default itself is the problem.
EXPO_SDK_54_DEFAULTS = {
    "android_target_sdk":   35,
    "android_compile_sdk":  35,
    "android_min_sdk":      24,
    "ios_deployment_target": "15.1",
    "hermes_enabled":       True,
    "new_arch_enabled":     True,
    "edge_to_edge":         True,
}

PLAY_TARGET_SDK_FLOOR_2026 = 35  # Play submission floor for 2026 — SDK 54 default already clears this.

PLACEHOLDER_ID_PATTERNS = (
    r"^app\.emergent\.",
    r"^com\.example\.",
    r"^host\.exp\.",
    r"^com\.expo\.",
    r"^com\.anonymous\.",
)
_PLACEHOLDER_RX = re.compile("|".join(PLACEHOLDER_ID_PATTERNS))


# ── Permission probes — used by two iOS+Android cross-rules ──────────────────
@dataclass(frozen=True)
class PermissionProbe:
    label: str
    source_patterns: tuple[str, ...]      # regexes; ANY match counts as "API is used"
    ios_info_plist_key: str | None = None  # the NS*UsageDescription key, if any
    android_permission: str | None = None  # the android.permission.* string, if any
    plugin_implies: tuple[str, ...] = ()   # Expo plugins that, when registered, imply this permission
    auto_added_by: tuple[str, ...] = ()    # deps that, when in package.json, cause Expo autolinking
                                           # to inject the permission + a default iOS usage description.
                                           # Stronger signal than `plugin_implies` (no plugin entry needed).


PERMISSION_PROBES: tuple[PermissionProbe, ...] = (
    PermissionProbe(
        label="camera",
        source_patterns=(
            r"\bCamera\.requestCameraPermissionsAsync\b",
            r"\bImagePicker\.launchCameraAsync\b",
            r"\buseCameraPermissions\b",
            r"from\s+['\"]expo-camera['\"]",
            r"from\s+['\"]react-native-vision-camera['\"]",
        ),
        ios_info_plist_key="NSCameraUsageDescription",
        android_permission="android.permission.CAMERA",
        plugin_implies=("expo-camera",),
        # Expo autolinking on SDK 54 injects CAMERA + default NSCameraUsageDescription
        # when any of these deps is present, even without an explicit plugin entry.
        auto_added_by=("expo-camera", "expo-image-picker", "react-native-vision-camera"),
    ),
    PermissionProbe(
        label="microphone (recording)",
        source_patterns=(
            r"\bAudio\.requestPermissionsAsync\b",
            r"\bAudio\.Recording\b",
            r"\bnew\s+Audio\.Recording\b",
        ),
        ios_info_plist_key="NSMicrophoneUsageDescription",
        android_permission="android.permission.RECORD_AUDIO",
        plugin_implies=(),
        # expo-av's plugin handles the iOS description default; deps alone don't,
        # so do NOT add auto_added_by — the rule should fire on a real mismatch.
    ),
    PermissionProbe(
        label="photo library",
        source_patterns=(
            r"\bImagePicker\.launchImageLibraryAsync\b",
            r"\bMediaLibrary\.requestPermissionsAsync\b",
            r"from\s+['\"]expo-media-library['\"]",
        ),
        ios_info_plist_key="NSPhotoLibraryUsageDescription",
        android_permission=None,  # Modern Android uses scoped storage; no perm needed for image picker
        plugin_implies=("expo-image-picker", "expo-media-library"),
        auto_added_by=("expo-image-picker", "expo-media-library"),
    ),
    PermissionProbe(
        label="location (foreground)",
        source_patterns=(
            r"\bLocation\.requestForegroundPermissionsAsync\b",
            r"\bLocation\.getCurrentPositionAsync\b",
            r"from\s+['\"]expo-location['\"]",
        ),
        ios_info_plist_key="NSLocationWhenInUseUsageDescription",
        android_permission="android.permission.ACCESS_FINE_LOCATION",
        plugin_implies=("expo-location",),
    ),
    PermissionProbe(
        label="location (background)",
        source_patterns=(
            r"\bLocation\.requestBackgroundPermissionsAsync\b",
            r"\bLocation\.startLocationUpdatesAsync\b",
        ),
        ios_info_plist_key="NSLocationAlwaysAndWhenInUseUsageDescription",
        android_permission="android.permission.ACCESS_BACKGROUND_LOCATION",
        plugin_implies=(),
    ),
    PermissionProbe(
        label="contacts",
        source_patterns=(
            r"\bContacts\.requestPermissionsAsync\b",
            r"from\s+['\"]expo-contacts['\"]",
        ),
        ios_info_plist_key="NSContactsUsageDescription",
        android_permission="android.permission.READ_CONTACTS",
        plugin_implies=("expo-contacts",),
    ),
    PermissionProbe(
        label="calendar",
        source_patterns=(
            r"\bCalendar\.requestCalendarPermissionsAsync\b",
            r"from\s+['\"]expo-calendar['\"]",
        ),
        ios_info_plist_key="NSCalendarsUsageDescription",
        android_permission="android.permission.READ_CALENDAR",
        plugin_implies=("expo-calendar",),
    ),
    PermissionProbe(
        label="Face ID / biometric",
        source_patterns=(
            r"\bLocalAuthentication\.authenticateAsync\b",
            r"from\s+['\"]expo-local-authentication['\"]",
        ),
        ios_info_plist_key="NSFaceIDUsageDescription",
        android_permission="android.permission.USE_BIOMETRIC",
        plugin_implies=("expo-local-authentication",),
    ),
    PermissionProbe(
        label="App Tracking Transparency (IDFA)",
        source_patterns=(
            r"\brequestTrackingPermissionsAsync\b",
            r"from\s+['\"]expo-tracking-transparency['\"]",
        ),
        ios_info_plist_key="NSUserTrackingUsageDescription",
        android_permission=None,
        plugin_implies=("expo-tracking-transparency",),
    ),
)


# ── Context dataclass ────────────────────────────────────────────────────────
@dataclass
class StoreCtx:
    workspace: Path
    app_config: dict       # the merged `expo` block from app.json/app.config.*
    package_json: dict     # full package.json
    dependencies: dict     # {name: version} of deps + devDeps merged
    facts: dict            # audit_facts.json
    apk_scan: dict         # artifacts/apk_scan.json (may be empty)
    source_index: dict[str, str]  # {relative_path: file_text}
    disclosures: list[SDKDisclosure] = field(default_factory=list)

    # ── helpers ──
    def source_grep(self, pattern: str) -> list[tuple[str, int, str]]:
        """Return (file, 1-based-line, line_text) for every regex hit across the index."""
        rx = re.compile(pattern)
        hits: list[tuple[str, int, str]] = []
        for path, text in self.source_index.items():
            for i, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append((path, i, line.strip()))
        return hits

    def source_contains_any(self, patterns: tuple[str, ...]) -> tuple[bool, str | None, int | None]:
        """First-hit search across the index. Returns (found, file, line)."""
        for p in patterns:
            hits = self.source_grep(p)
            if hits:
                f, ln, _ = hits[0]
                return True, f, ln
        return False, None, None

    def ios_info_plist_has(self, key: str) -> bool:
        ip = (self.app_config.get("ios") or {}).get("infoPlist") or {}
        return key in ip and ip.get(key) not in (None, "")

    def has_android_permission(self, perm: str) -> bool:
        # Strip android.permission. prefix for tolerant matching
        target = perm.split(".")[-1]
        decl = (self.app_config.get("android") or {}).get("permissions") or []
        for p in decl:
            if not isinstance(p, str):
                continue
            if p == perm or p.split(".")[-1] == target:
                return True
        return False

    def plugin_listed(self, plugin_name: str) -> bool:
        plugins = self.app_config.get("plugins") or []
        for entry in plugins:
            name = entry if isinstance(entry, str) else (entry[0] if isinstance(entry, list) and entry else None)
            if name == plugin_name:
                return True
        return False

    def has_dependency(self, name: str) -> bool:
        return name in self.dependencies

    def file_status(self, rel_path: str) -> str:
        """Three-valued: 'present' (file exists in workspace), 'missing'
        (parent dir exists but file does not), or 'unverified' (parent dir
        absent → workspace ingest excluded this area, so we can't tell)."""
        rel = rel_path.lstrip("./").replace("\\", "/")
        full = (self.workspace / rel).resolve()
        if full.is_file():
            return "present"
        parent = full.parent
        if parent.is_dir():
            return "missing"
        return "unverified"


# ── Finding helper ───────────────────────────────────────────────────────────
def _finding(
    rule_id: str,
    severity: str,
    title: str,
    description: str,
    *,
    confidence: str = "high",
    file: str = "app.json",
    function: str = "<config>",
    line: int | None = None,
    code_snippet: str = "",
    metric_name: str | None = None,
    metric_value: Any = None,
    metric_threshold: Any = None,
) -> dict:
    ev: dict[str, Any] = {"file": file, "function": function, "code_snippet": code_snippet}
    if line is not None:
        ev["line"] = line
    if metric_name is not None:
        ev["metric_name"] = metric_name
    if metric_value is not None:
        ev["metric_value"] = metric_value
    if metric_threshold is not None:
        ev["metric_threshold"] = metric_threshold
    return {
        "id": rule_id,
        "layer": "store",
        "category": "publishing",
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "description": description,
        "evidence": ev,
    }


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ APPLE APP STORE rules (store.ios.*)                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def rule_ios_bundle_identifier_placeholder(ctx: StoreCtx) -> list[dict]:
    bid = (ctx.app_config.get("ios") or {}).get("bundleIdentifier")
    if not bid:
        return [_finding(
            "store.ios.bundle_identifier_missing",
            "critical",
            "iOS bundleIdentifier is not set",
            "`expo.ios.bundleIdentifier` is required for App Store submission. Pick a reverse-DNS identifier you control and register it in App Store Connect.",
        )]
    if _PLACEHOLDER_RX.search(bid):
        return [_finding(
            "store.ios.bundle_identifier_placeholder",
            "critical",
            f"iOS bundleIdentifier is a placeholder (`{bid}`)",
            f"`expo.ios.bundleIdentifier = \"{bid}\"` matches an EAS / Expo placeholder pattern. Pick a real reverse-DNS identifier you control and register it in App Store Connect before submitting.",
            metric_name="bundle_identifier",
            metric_value=bid,
        )]
    return []


def rule_ios_missing_usage_description(ctx: StoreCtx) -> list[dict]:
    out: list[dict] = []
    for probe in PERMISSION_PROBES:
        if probe.ios_info_plist_key is None:
            continue
        used, file, line = ctx.source_contains_any(probe.source_patterns)
        plugin_used = any(ctx.plugin_listed(p) for p in probe.plugin_implies)
        if not (used or plugin_used):
            continue
        if ctx.ios_info_plist_has(probe.ios_info_plist_key):
            continue
        # Soften when a dep that auto-injects a default description is present.
        # Expo autolinking does inject a generic string at build time; App Review
        # accepts a generic default. This becomes MEDIUM "provide a custom string"
        # rather than CRITICAL "will be rejected."
        auto_dep = next((d for d in probe.auto_added_by if ctx.has_dependency(d)), None)
        if auto_dep:
            out.append(_finding(
                "store.ios.usage_description_uses_default",
                "medium",
                f"iOS `{probe.ios_info_plist_key}` uses the autolinked default (from `{auto_dep}`)",
                (
                    f"The app uses `{probe.label}` and `{auto_dep}` is in `package.json`, so Expo "
                    f"autolinking will inject a generic `{probe.ios_info_plist_key}` at build time. "
                    f"The submission won't be rejected, but the prompt will be generic. Set a custom "
                    f"string in `expo.ios.infoPlist.{probe.ios_info_plist_key}` that explains what the "
                    f"user is approving."
                ),
                file="app.json",
                function="ios.infoPlist",
                confidence="medium",
                metric_name="missing_info_plist_key",
                metric_value=probe.ios_info_plist_key,
            ))
            continue
        out.append(_finding(
            "store.ios.missing_usage_description",
            "critical",
            f"iOS `{probe.ios_info_plist_key}` is missing — required for `{probe.label}`",
            (
                f"The app uses `{probe.label}` "
                f"({'source: '+file+(':'+str(line) if line else '') if used else 'inferred from plugin'})"
                f" but `expo.ios.infoPlist.{probe.ios_info_plist_key}` is not set. "
                f"Apple's review automatically rejects builds that call permission APIs without a user-facing "
                f"description string."
            ),
            file="app.json",
            function="ios.infoPlist",
            metric_name="missing_info_plist_key",
            metric_value=probe.ios_info_plist_key,
        ))
    return out


def rule_ios_privacy_manifest_missing_for_sdk(ctx: StoreCtx) -> list[dict]:
    requirers = all_privacy_manifest_requirers(ctx.disclosures)
    if not requirers:
        return []
    declared = (ctx.app_config.get("ios") or {}).get("privacyManifests")
    # If any privacyManifests block exists, downgrade severity — partial config
    # is much closer to compliant than total absence.
    if declared:
        return []
    out: list[dict] = []
    for d in requirers:
        out.append(_finding(
            "store.ios.privacy_manifest_missing_for_sdk",
            "high",
            f"iOS privacy manifest missing for {d.label}",
            (
                f"{d.label} is in `package.json` but `expo.ios.privacyManifests` is not configured. "
                f"Since May 2024 Apple sends notices for missing privacy manifests; some submissions are rejected. "
                f"You must add the SDK-declared required-reason API list and the data-collection categories "
                f"({', '.join(d.apple_categories) if d.apple_categories else 'see vendor docs'})."
            ),
            file="app.json",
            function="ios.privacyManifests",
            confidence="high",
            metric_name="sdk_requiring_privacy_manifest",
            metric_value=d.label,
        ))
    return out


def rule_ios_tracking_transparency_missing(ctx: StoreCtx) -> list[dict]:
    if not any_tracking_class(ctx.disclosures):
        return []
    if ctx.ios_info_plist_has("NSUserTrackingUsageDescription"):
        return []
    tracking_sdks = [d.label for d in ctx.disclosures if d.needs_att]
    return [_finding(
        "store.ios.tracking_transparency_missing",
        "high",
        "App Tracking Transparency description missing",
        (
            f"The app includes a tracking-class SDK ({', '.join(tracking_sdks)}) but "
            f"`expo.ios.infoPlist.NSUserTrackingUsageDescription` is not set. Without it, "
            f"calling `requestTrackingPermissionsAsync` crashes, and App Review will flag the app."
        ),
        file="app.json",
        function="ios.infoPlist",
    )]


def rule_ios_encryption_export_undeclared(ctx: StoreCtx) -> list[dict]:
    cfg = (ctx.app_config.get("ios") or {}).get("config") or {}
    if "usesNonExemptEncryption" in cfg:
        return []
    return [_finding(
        "store.ios.encryption_export_undeclared",
        "medium",
        "iOS encryption export compliance not declared",
        (
            "`expo.ios.config.usesNonExemptEncryption` is not set. App Store Connect will "
            "prompt for export-compliance docs at every submission until you declare this. "
            "Most apps that only use HTTPS set this to `false`."
        ),
        file="app.json",
        function="ios.config",
    )]


def rule_ios_universal_links_missing(ctx: StoreCtx) -> list[dict]:
    scheme = ctx.app_config.get("scheme")
    if not scheme:
        return []
    # Only fire when there's evidence of deep-link usage in source.
    used, _, _ = ctx.source_contains_any((
        r"from\s+['\"]expo-linking['\"]",
        r"\bLinking\.createURL\b",
        r"\bLinking\.openURL\b",
        r"\buseURL\(",
        r"from\s+['\"]expo-router['\"]",  # expo-router uses scheme implicitly
    ))
    if not used:
        return []
    ad = (ctx.app_config.get("ios") or {}).get("associatedDomains")
    if ad and len(ad) > 0:
        return []
    return [_finding(
        "store.ios.universal_links_missing",
        "high",
        f"iOS Universal Links not configured (scheme `{scheme}` only)",
        (
            f"`expo.scheme = \"{scheme}\"` is declared and deep-link APIs are used in source, "
            f"but `expo.ios.associatedDomains` is empty. Custom schemes work in-app but are not "
            f"verified — links from email, SMS, and the web won't open your app. Add "
            f"`applinks:<your-domain>` and host an `apple-app-site-association` file."
        ),
        file="app.json",
        function="ios.associatedDomains",
    )]


def rule_ios_background_modes_unjustified(ctx: StoreCtx) -> list[dict]:
    ip = (ctx.app_config.get("ios") or {}).get("infoPlist") or {}
    modes = ip.get("UIBackgroundModes") or []
    if not modes:
        return []
    out: list[dict] = []
    justification = {
        "audio":               (r"Audio\.Sound|Audio\.Recording|playAsync|expo-av",),
        "location":            (r"Location\.startLocationUpdatesAsync|expo-location",),
        "voip":                (r"voip|callkit",),
        "fetch":               (r"BackgroundFetch|expo-background-fetch",),
        "remote-notification": (r"expo-notifications|setNotificationHandler",),
        "processing":          (r"BGProcessingTask|expo-task-manager",),
    }
    for mode in modes:
        patterns = justification.get(mode, ())
        if not patterns:
            continue
        used, _, _ = ctx.source_contains_any(patterns)
        if used:
            continue
        out.append(_finding(
            "store.ios.background_modes_unjustified",
            "medium",
            f"iOS UIBackgroundMode `{mode}` declared but no matching API usage in source",
            (
                f"`expo.ios.infoPlist.UIBackgroundModes` includes `{mode}` but no calls "
                f"that justify it were found in source. Apple Review asks for a written "
                f"justification for every background mode. Remove unused modes."
            ),
            file="app.json",
            function="ios.infoPlist.UIBackgroundModes",
            metric_name="unjustified_background_mode",
            metric_value=mode,
        ))
    return out


def rule_ios_ats_too_permissive(ctx: StoreCtx) -> list[dict]:
    ip = (ctx.app_config.get("ios") or {}).get("infoPlist") or {}
    ats = ip.get("NSAppTransportSecurity") or {}
    if not ats:
        return []
    if ats.get("NSAllowsArbitraryLoads") is True:
        return [_finding(
            "store.ios.app_transport_security_too_permissive",
            "high",
            "iOS App Transport Security allows arbitrary loads",
            (
                "`NSAppTransportSecurity.NSAllowsArbitraryLoads = true` disables HTTPS-only "
                "enforcement. App Review asks for written justification and frequently rejects. "
                "Use per-domain exceptions in `NSExceptionDomains` instead."
            ),
            file="app.json",
            function="ios.infoPlist.NSAppTransportSecurity",
        )]
    return []


def rule_ios_deployment_target_unset(ctx: StoreCtx) -> list[dict]:
    dt = (ctx.app_config.get("ios") or {}).get("deploymentTarget")
    if dt:
        return []
    # Unset = Expo SDK 54 default 15.1 (fine). Emit LOW so it surfaces in the report
    # but doesn't gate AT-RISK / BLOCKED.
    return [_finding(
        "store.ios.deployment_target_unset",
        "low",
        f"iOS deployment target unset (uses Expo SDK 54 default: {EXPO_SDK_54_DEFAULTS['ios_deployment_target']})",
        (
            f"`expo.ios.deploymentTarget` is not explicitly set. The Expo SDK 54 default "
            f"({EXPO_SDK_54_DEFAULTS['ios_deployment_target']}) is fine for App Store submission today, "
            f"but pinning it makes future SDK upgrades less surprising."
        ),
        file="app.json",
        function="ios.deploymentTarget",
        confidence="medium",
    )]


def rule_ios_iap_setup_reminder(ctx: StoreCtx) -> list[dict]:
    if not (ctx.has_dependency("react-native-iap") or ctx.has_dependency("expo-in-app-purchases")):
        return []
    return [_finding(
        "store.ios.iap_setup_reminder",
        "low",
        "iOS In-App Purchase products must be configured in App Store Connect",
        (
            "A React Native IAP dependency is present. You must create matching IAP products in "
            "App Store Connect with SKUs that match the ones requested in source. See the SKU "
            "enumeration table for the exact list extracted from your code."
        ),
        file="package.json",
        function="<dependencies>",
        confidence="medium",
    )]


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ GOOGLE PLAY STORE rules (store.android.*)                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def rule_android_app_package_placeholder(ctx: StoreCtx) -> list[dict]:
    pkg = (ctx.app_config.get("android") or {}).get("package")
    if not pkg:
        return [_finding(
            "store.android.app_package_missing",
            "critical",
            "Android package name is not set",
            "`expo.android.package` is required for Play Console submission. Pick a reverse-DNS package name you control.",
        )]
    if _PLACEHOLDER_RX.search(pkg):
        return [_finding(
            "store.android.app_package_placeholder",
            "critical",
            f"Android package is a placeholder (`{pkg}`)",
            f"`expo.android.package = \"{pkg}\"` matches an EAS / Expo placeholder pattern. Pick a real reverse-DNS package name you control and register it in Play Console before submitting.",
            metric_name="android_package",
            metric_value=pkg,
        )]
    return []


def rule_android_target_sdk_outdated(ctx: StoreCtx) -> list[dict]:
    target = (ctx.app_config.get("android") or {}).get("targetSdkVersion")
    if target is None:
        # SDK 54 default 35 ≥ floor; PASS.
        return []
    if isinstance(target, int) and target < PLAY_TARGET_SDK_FLOOR_2026:
        return [_finding(
            "store.android.target_sdk_outdated",
            "high",
            f"Android targetSdkVersion ({target}) is below the Play Console floor ({PLAY_TARGET_SDK_FLOOR_2026})",
            (
                f"`expo.android.targetSdkVersion` is explicitly set to {target}. Play Console rejects "
                f"new submissions targeting below {PLAY_TARGET_SDK_FLOOR_2026} for 2026. Remove the "
                f"override (Expo SDK 54 defaults to {EXPO_SDK_54_DEFAULTS['android_target_sdk']}) or "
                f"bump the value."
            ),
            file="app.json",
            function="android.targetSdkVersion",
            metric_name="target_sdk",
            metric_value=target,
            metric_threshold=PLAY_TARGET_SDK_FLOOR_2026,
        )]
    return []


def rule_android_permission_declared_but_unused(ctx: StoreCtx) -> list[dict]:
    declared = (ctx.app_config.get("android") or {}).get("permissions") or []
    if not declared:
        return []
    out: list[dict] = []
    # Build a probe lookup by android_permission tail.
    probe_by_perm: dict[str, PermissionProbe] = {}
    for p in PERMISSION_PROBES:
        if p.android_permission:
            probe_by_perm[p.android_permission.split(".")[-1]] = p

    for raw in declared:
        if not isinstance(raw, str):
            continue
        tail = raw.split(".")[-1]
        probe = probe_by_perm.get(tail)
        if probe is None:
            # Permission we don't have a probe for — skip rather than guess.
            continue
        used, _, _ = ctx.source_contains_any(probe.source_patterns)
        plugin_used = any(ctx.plugin_listed(p) for p in probe.plugin_implies)
        if used or plugin_used:
            continue
        out.append(_finding(
            "store.android.permission_declared_but_unused",
            "medium",
            f"Android permission `{tail}` declared but no source-code use detected",
            (
                f"`expo.android.permissions` includes `{raw}` but no calls to `{probe.label}` APIs were "
                f"found in source. Play Console flags over-broad permissions in policy review. "
                f"Remove it if you're not actually using the feature."
            ),
            file="app.json",
            function="android.permissions",
            confidence="medium",
            metric_name="declared_unused_permission",
            metric_value=tail,
        ))
    return out


def rule_android_permission_used_but_undeclared(ctx: StoreCtx) -> list[dict]:
    out: list[dict] = []
    for probe in PERMISSION_PROBES:
        if probe.android_permission is None:
            continue
        used, file, line = ctx.source_contains_any(probe.source_patterns)
        if not used:
            continue
        if ctx.has_android_permission(probe.android_permission):
            continue
        if any(ctx.plugin_listed(p) for p in probe.plugin_implies):
            # Plugin auto-injects this permission; skip.
            continue
        if any(ctx.has_dependency(d) for d in probe.auto_added_by):
            # Autolinking-only dep will inject the permission at build time. Skip.
            continue
        out.append(_finding(
            "store.android.permission_used_but_undeclared",
            "high",
            f"Android permission `{probe.android_permission}` used but not declared",
            (
                f"Source calls `{probe.label}` APIs ({file}:{line}) but the required permission "
                f"`{probe.android_permission}` is not in `expo.android.permissions` and no plugin "
                f"that auto-injects it is registered. The call will throw a SecurityException at runtime."
            ),
            file=file or "app.json",
            function="android.permissions",
            line=line,
            metric_name="undeclared_android_permission",
            metric_value=probe.android_permission,
        ))
    return out


def rule_android_post_notifications_permission_missing(ctx: StoreCtx) -> list[dict]:
    uses_notifs = ctx.has_dependency("expo-notifications") or ctx.has_dependency("@react-native-firebase/messaging")
    if not uses_notifs:
        return []
    if ctx.has_android_permission("android.permission.POST_NOTIFICATIONS"):
        return []
    # Expo notifications plugin auto-adds POST_NOTIFICATIONS; treat plugin presence as PASS.
    if ctx.plugin_listed("expo-notifications"):
        return []
    return [_finding(
        "store.android.post_notifications_permission_missing",
        "high",
        "Android POST_NOTIFICATIONS permission not declared",
        (
            "`expo-notifications` is in use but `POST_NOTIFICATIONS` is not in `expo.android.permissions` "
            "and the expo-notifications plugin is not registered. Android 13+ silently drops every "
            "notification until the user grants this runtime permission."
        ),
        file="app.json",
        function="android.permissions",
    )]


def rule_android_iap_billing_permission_missing(ctx: StoreCtx) -> list[dict]:
    if not (ctx.has_dependency("react-native-iap") or ctx.has_dependency("expo-in-app-purchases")):
        return []
    if ctx.has_android_permission("com.android.vending.BILLING"):
        return []
    return [_finding(
        "store.android.iap_billing_permission_missing",
        "critical",
        "Android `com.android.vending.BILLING` permission not declared",
        (
            "A React Native IAP dependency is present, but `com.android.vending.BILLING` is missing from "
            "`expo.android.permissions`. Without it, all IAP calls fail at runtime."
        ),
        file="app.json",
        function="android.permissions",
    )]


def rule_android_adaptive_icon_missing(ctx: StoreCtx) -> list[dict]:
    ai = (ctx.app_config.get("android") or {}).get("adaptiveIcon") or {}
    fg = ai.get("foregroundImage")
    bg = ai.get("backgroundColor")
    if fg and bg:
        return []
    return [_finding(
        "store.android.adaptive_icon_missing",
        "high",
        "Android adaptive icon incomplete",
        (
            "`expo.android.adaptiveIcon.foregroundImage` and/or `backgroundColor` are missing. "
            "Android 8.0+ launchers crop the legacy icon into shapes, which produces a clipped or "
            "ugly result. Provide both fields."
        ),
        file="app.json",
        function="android.adaptiveIcon",
    )]


def rule_android_cleartext_traffic_enabled(ctx: StoreCtx) -> list[dict]:
    ct = (ctx.app_config.get("android") or {}).get("usesCleartextTraffic")
    if ct is True:
        return [_finding(
            "store.android.cleartext_traffic_enabled",
            "critical",
            "Android `usesCleartextTraffic = true` in production",
            (
                "`expo.android.usesCleartextTraffic` is `true`. Play Console policy and Android 9+ "
                "default both forbid plain-HTTP traffic. If you genuinely need it for one domain, "
                "scope via `networkSecurityConfig` instead."
            ),
            file="app.json",
            function="android.usesCleartextTraffic",
        )]
    return []


def rule_android_intent_filters_missing_autoverify(ctx: StoreCtx) -> list[dict]:
    scheme = ctx.app_config.get("scheme")
    if not scheme:
        return []
    used, _, _ = ctx.source_contains_any((
        r"from\s+['\"]expo-linking['\"]",
        r"\bLinking\.createURL\b",
        r"from\s+['\"]expo-router['\"]",
    ))
    if not used:
        return []
    intent_filters = (ctx.app_config.get("android") or {}).get("intentFilters") or []
    autoverified = False
    for f in intent_filters:
        if isinstance(f, dict) and f.get("autoVerify") is True:
            autoverified = True
            break
    if autoverified:
        return []
    return [_finding(
        "store.android.intent_filters_missing_autoverify",
        "high",
        f"Android App Links not verified (scheme `{scheme}` only)",
        (
            f"`expo.scheme = \"{scheme}\"` is declared and deep-link APIs are used, but no "
            f"`expo.android.intentFilters` entry has `autoVerify: true` on a real domain. Custom "
            f"schemes work in-app but the system picker shows competing apps. Add a verified "
            f"`intentFilter` with `autoVerify: true` and host `assetlinks.json` at "
            f"`/.well-known/assetlinks.json` on the matching domain."
        ),
        file="app.json",
        function="android.intentFilters",
    )]


def rule_android_foreground_service_type_missing(ctx: StoreCtx) -> list[dict]:
    """Detect Android-specific background work without `foregroundServiceType` declared.
    Conservative: only Android signals (expo-task-manager + expo-background-fetch).
    iOS background flags (playsInSilentModeIOS, staysActiveInBackground) are unrelated
    — they drive UIBackgroundModes, not Android services.
    """
    triggers = []
    if ctx.has_dependency("expo-task-manager"):
        triggers.append("expo-task-manager")
    if ctx.has_dependency("expo-background-fetch"):
        triggers.append("expo-background-fetch")
    if not triggers:
        return []
    # Check for declared foregroundService entries.
    services = (ctx.app_config.get("android") or {}).get("services") or []
    has_fst = any((isinstance(s, dict) and s.get("foregroundServiceType")) for s in services)
    if has_fst:
        return []
    return [_finding(
        "store.android.foreground_service_type_missing",
        "medium",
        f"Android background work declared ({', '.join(triggers)}) without `foregroundServiceType`",
        (
            "Android 14+ requires `foregroundServiceType` for any service that does background work. "
            "Without it, the service is killed silently on modern devices."
        ),
        file="app.json",
        function="android.services",
    )]


def rule_android_version_code_missing_or_stuck(ctx: StoreCtx) -> list[dict]:
    vc = (ctx.app_config.get("android") or {}).get("versionCode")
    if vc is None:
        return [_finding(
            "store.android.version_code_missing_or_stuck",
            "low",
            "Android versionCode not pinned in app.json",
            (
                "`expo.android.versionCode` is not set. EAS auto-increments by default, which works "
                "fine in CI; if you build locally, every release must bump this integer or Play "
                "Console rejects the upload."
            ),
            file="app.json",
            function="android.versionCode",
            confidence="medium",
        )]
    return []


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ CROSS-CUTTING rules (store.cross.*)                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def rule_cross_display_name_missing(ctx: StoreCtx) -> list[dict]:
    if ctx.app_config.get("name"):
        return []
    return [_finding(
        "store.cross.display_name_missing",
        "critical",
        "App display name (`expo.name`) is not set",
        "`expo.name` is required. Both stores need a display name; missing it is a build-time error in EAS.",
        file="app.json",
        function="<expo>",
    )]


def rule_cross_icon_missing(ctx: StoreCtx) -> list[dict]:
    icon = ctx.app_config.get("icon")
    if not icon:
        return [_finding(
            "store.cross.icon_missing",
            "critical",
            "App icon (`expo.icon`) is not set",
            "`expo.icon` is required. Both stores reject submissions without a 1024×1024 icon.",
            file="app.json",
            function="<expo>",
        )]
    status = ctx.file_status(icon)
    if status == "present":
        return []
    if status == "missing":
        return [_finding(
            "store.cross.icon_missing",
            "critical",
            f"App icon `{icon}` referenced but file is missing",
            "The icon path is declared but the file is not present in the workspace.",
            file="app.json",
            function="expo.icon",
            metric_name="icon_path",
            metric_value=icon,
        )]
    # unverified — the asset directory was not ingested. Don't FAIL on absence;
    # surface as INFO so the operator confirms manually.
    return [_finding(
        "store.cross.icon_unverified",
        "info",
        f"App icon `{icon}` declared — could not verify in this workspace ingest",
        (
            f"`expo.icon = \"{icon}\"` is declared, but the asset directory was not ingested into "
            "the audit workspace (allowlist excludes `assets/`). Confirm the file exists in the repo."
        ),
        file="app.json",
        function="expo.icon",
        confidence="medium",
        metric_name="icon_path",
        metric_value=icon,
    )]


def rule_cross_version_mismatch(ctx: StoreCtx) -> list[dict]:
    v = ctx.app_config.get("version")
    if not v:
        return [_finding(
            "store.cross.version_missing",
            "high",
            "`expo.version` is not set",
            "Both stores require a marketing version string (e.g. `1.2.5`).",
            file="app.json",
            function="<expo>",
        )]
    return []


def rule_cross_dev_url_in_source(ctx: StoreCtx) -> list[dict]:
    """Hardcoded dev/preview URLs in shipped source = rejection or runtime breakage."""
    patterns = (
        r"https?://localhost",
        r"https?://127\.0\.0\.1",
        r"https?://192\.168\.",
        r"https?://[a-z0-9-]+\.preview\.emergentagent\.com",
        r"https?://[a-z0-9-]+\.ngrok\.",
    )
    out: list[dict] = []
    seen_files: set[str] = set()
    for p in patterns:
        for file, line, snippet in ctx.source_grep(p):
            # Skip test files
            if "/test" in file.replace("\\", "/").lower() or "__tests__" in file:
                continue
            if file in seen_files:
                continue
            seen_files.add(file)
            out.append(_finding(
                "store.cross.dev_url_in_source",
                "critical",
                f"Hardcoded dev/preview URL in shipped source: `{file}`",
                f"`{file}` (line {line}) contains a development URL: `{snippet[:120]}`. Production builds will either break or leak preview infrastructure.",
                file=file,
                function="<module>",
                line=line,
                code_snippet=snippet[:200],
            ))
    return out


def rule_cross_unguarded_test_keys(ctx: StoreCtx) -> list[dict]:
    patterns = (
        r"\bsk_test_[A-Za-z0-9]{8,}",     # Stripe secret test key
        r"\bpk_test_[A-Za-z0-9]{8,}",     # Stripe publishable test key
        r"\brk_test_[A-Za-z0-9]{8,}",     # Stripe restricted test key
    )
    out: list[dict] = []
    for p in patterns:
        for file, line, snippet in ctx.source_grep(p):
            if "/test" in file.replace("\\", "/").lower() or "__tests__" in file:
                continue
            out.append(_finding(
                "store.cross.unguarded_test_keys",
                "high",
                f"Test-mode API key shipped in `{file}`",
                f"`{file}` (line {line}) contains what looks like a test-mode API key. Replace with the production key before shipping; never ship test keys.",
                file=file,
                function="<module>",
                line=line,
                code_snippet=snippet[:200],
            ))
    return out


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ PROCESS-ITEM rules (store.process.*) — Phase A only (no network)         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def rule_process_privacy_policy_url_missing(ctx: StoreCtx) -> list[dict]:
    """Phase A: just check if a URL is declared anywhere config-side. Phase B
    (network HEAD/GET) is deferred per the slice plan."""
    extras = ctx.app_config.get("extra") or {}
    candidate = (
        extras.get("privacyPolicyUrl")
        or extras.get("privacy_policy_url")
        or ctx.app_config.get("privacyPolicyUrl")
    )
    if candidate:
        return []
    # Also accept a constant in source
    used, _, _ = ctx.source_contains_any((
        r"['\"]https?://[^'\"]+privacy[^'\"]*['\"]",
        r"privacyPolicy(?:Url)?\s*[:=]\s*['\"]https?:",
    ))
    if used:
        return []
    return [_finding(
        "store.process.privacy_policy_url_missing",
        "critical",
        "No privacy policy URL declared anywhere in config or source",
        (
            "Both App Store Connect and Play Console require a live privacy policy URL. "
            "Set one in `expo.extra.privacyPolicyUrl` (and reference it from your settings screen) "
            "and host the page before submitting."
        ),
        file="app.json",
        function="expo.extra",
        confidence="medium",
    )]


def rule_process_google_services_json_missing(ctx: StoreCtx) -> list[dict]:
    """For each referenced Firebase config file, two outcomes:
      present     → no finding
      not present → INFO ("confirm it's in EAS secrets / your CI build")

    We don't emit CRITICAL for absence because these files are usually
    gitignored — the workspace ingest legitimately won't include them — and
    we can't tell "missing from workspace" from "provided via EAS secrets."
    Surfacing as INFO keeps it on the user's checklist without false alarms.
    """
    out: list[dict] = []
    android_path = (ctx.app_config.get("android") or {}).get("googleServicesFile")
    if android_path and ctx.file_status(android_path) != "present":
        out.append(_finding(
            "store.process.google_services_json_unverified",
            "info",
            f"Confirm `{android_path}` is provided at build time",
            (
                f"`expo.android.googleServicesFile = \"{android_path}\"` is declared, but the "
                "file is not in the ingested workspace (commonly gitignored). Confirm it's provided "
                "via EAS secrets or your CI build environment — Firebase Android won't initialize without it."
            ),
            file="app.json",
            function="android.googleServicesFile",
            confidence="medium",
            metric_name="referenced_file",
            metric_value=android_path,
        ))
    ios_path = (ctx.app_config.get("ios") or {}).get("googleServicesFile")
    if ios_path and ctx.file_status(ios_path) != "present":
        out.append(_finding(
            "store.process.googleservice_info_plist_unverified",
            "info",
            f"Confirm `{ios_path}` is provided at build time",
            (
                f"`expo.ios.googleServicesFile = \"{ios_path}\"` is declared, but the file is not "
                "in the ingested workspace. Confirm it's provided via EAS secrets or your CI build."
            ),
            file="app.json",
            function="ios.googleServicesFile",
            confidence="medium",
            metric_name="referenced_file",
            metric_value=ios_path,
        ))
    return out


def rule_process_push_credentials_unverified(ctx: StoreCtx) -> list[dict]:
    push_used = (
        ctx.has_dependency("expo-notifications")
        or ctx.has_dependency("@react-native-firebase/messaging")
    )
    if not push_used:
        return []
    return [_finding(
        "store.process.push_credentials_unverified",
        "medium",
        "Push wiring detected — credentials must be confirmed outside the audit",
        (
            "Push libraries are in the dependency tree. The audit cannot confirm whether the APNs key is "
            "uploaded to Apple Developer Portal or whether the FCM service account JSON is in EAS secrets. "
            "Confirm both before shipping."
        ),
        file="package.json",
        function="<dependencies>",
        confidence="medium",
    )]


def rule_process_iap_skus_required(ctx: StoreCtx) -> list[dict]:
    if not (ctx.has_dependency("react-native-iap") or ctx.has_dependency("expo-in-app-purchases")):
        return []
    skus: list[tuple[str, str, int]] = []

    # Strategy:
    #  1. Scan files that reference IAP APIs.
    #  2. Skip comment lines (//, /*, *, ///).
    #  3. Look for object-property assignments where the value is a SKU-shaped
    #     literal: `<key>: 'lowercase_underscored_id'`. Real Astrova SKUs look
    #     like `astrova_basic`, `astrorecharge_29` — distinctive enough that
    #     this is reliable.
    #  4. Also accept array-literal SKU lists in IAP calls.
    EXCLUDED_VALUES = {"in-app", "subs", "ios", "android", "true", "false", "null",
                       "undefined", "monthly", "yearly", "basic", "pro", "premium",
                       "google", "apple", "managed", "consumable", "purchase"}
    # Strict file shortlist — only scan files that actually touch react-native-iap
    # (either by import or by referencing well-known SKU-collection constants).
    # Analytics services and event-name files use similar tokens but should not
    # contribute SKUs.
    import_rx = r"from\s+['\"]react-native-iap['\"]|require\(['\"]react-native-iap['\"]"
    sku_const_rx = r"\b(SUBSCRIPTION_SKUS?|CREDIT_SKUS?|ALL_[A-Z_]*SKUS?|PRODUCT_IDS?)\b"
    candidate_files: set[str] = set()
    for cp in (import_rx, sku_const_rx):
        for file, _line, _snip in ctx.source_grep(cp):
            candidate_files.add(file)

    # Property-assignment pattern: `<word>: 'sku_id'` with whitespace tolerant.
    assign_rx = re.compile(r"\b\w+\s*:\s*['\"]([a-z][a-z0-9_]{4,40})['\"]")
    # In-array SKU pattern: `[ 'sku_id', 'sku_id' ]`.
    array_item_rx = re.compile(r"['\"]([a-z][a-z0-9_]{4,40})['\"]")
    seen: set[str] = set()

    def looks_like_sku(s: str) -> bool:
        if s in EXCLUDED_VALUES:
            return False
        # SKUs almost always have an underscore (`astrova_pro`, `recharge_99`)
        # or a digit. Pure words like `subscription` or `purchase` aren't SKUs.
        return "_" in s or any(c.isdigit() for c in s)

    for f in candidate_files:
        text = ctx.source_index.get(f, "")
        in_block_comment = False
        for i, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line
            # Drop block comments
            if in_block_comment:
                if "*/" in line:
                    in_block_comment = False
                continue
            if "/*" in line and "*/" not in line:
                in_block_comment = True
                continue
            stripped = line.lstrip()
            if stripped.startswith(("//", "*", "///")):
                continue
            for rx in (assign_rx, array_item_rx):
                # Only invoke array_item_rx on lines that look like SKU arrays/calls
                if rx is array_item_rx and not re.search(r"(skus\s*[:=]|skus?\s*:|\[)", line, re.IGNORECASE):
                    continue
                for m in rx.finditer(line):
                    cand = m.group(1)
                    if not looks_like_sku(cand):
                        continue
                    if cand not in seen:
                        seen.add(cand)
                        skus.append((cand, f, i))
    if not skus:
        # Still emit a reminder that SKU enumeration could not auto-detect — user-actionable.
        return [_finding(
            "store.process.iap_skus_required",
            "low",
            "IAP detected — SKU enumeration could not extract literals automatically",
            (
                "A React Native IAP dependency is present but the audit could not pull SKU literals "
                "from source (they may be loaded from remote config). Verify the SKUs you create in "
                "App Store Connect / Play Console match what the app requests."
            ),
            file="package.json",
            function="<dependencies>",
            confidence="medium",
        )]
    descriptions = "\n".join(f"  • `{sku}` ({file}:{ln})" for sku, file, ln in skus)
    return [_finding(
        "store.process.iap_skus_required",
        "info",
        f"{len(skus)} IAP SKU(s) detected — must be created in both consoles",
        (
            "Create these SKUs in App Store Connect AND Play Console before submitting:\n"
            + descriptions
        ),
        file="<source>",
        function="<various>",
        confidence="medium",
        metric_name="iap_sku_count",
        metric_value=len(skus),
    )]


def rule_process_nutrition_label_categories(ctx: StoreCtx) -> list[dict]:
    out: list[dict] = []
    for d in ctx.disclosures:
        if not (d.apple_categories or d.google_categories):
            continue
        apple = "\n".join(f"    • {c}" for c in d.apple_categories) or "    • (none beyond required-reason API list)"
        google = "\n".join(f"    • {c}" for c in d.google_categories) or "    • (none)"
        out.append(_finding(
            "store.process.nutrition_label_categories",
            "info",
            f"{d.label} requires specific Privacy Nutrition Label / Data Safety entries",
            (
                f"Detected SDK: **{d.label}** ({d.sdk_class}).\n\n"
                f"Apple Privacy Nutrition Labels you must declare in App Store Connect:\n{apple}\n\n"
                f"Play Data Safety categories you must declare in Play Console:\n{google}\n\n"
                + (f"_Note: {d.notes}_" if d.notes else "")
            ),
            file="package.json",
            function="<dependencies>",
            confidence="medium",
            metric_name="sdk_label",
            metric_value=d.label,
        ))
    return out


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Rule registry                                                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

APPLE_RULES: tuple[Callable[[StoreCtx], list[dict]], ...] = (
    rule_ios_bundle_identifier_placeholder,
    rule_ios_missing_usage_description,
    rule_ios_privacy_manifest_missing_for_sdk,
    rule_ios_tracking_transparency_missing,
    rule_ios_encryption_export_undeclared,
    rule_ios_universal_links_missing,
    rule_ios_background_modes_unjustified,
    rule_ios_ats_too_permissive,
    rule_ios_deployment_target_unset,
    rule_ios_iap_setup_reminder,
)

GOOGLE_RULES: tuple[Callable[[StoreCtx], list[dict]], ...] = (
    rule_android_app_package_placeholder,
    rule_android_target_sdk_outdated,
    rule_android_permission_declared_but_unused,
    rule_android_permission_used_but_undeclared,
    rule_android_post_notifications_permission_missing,
    rule_android_iap_billing_permission_missing,
    rule_android_adaptive_icon_missing,
    rule_android_cleartext_traffic_enabled,
    rule_android_intent_filters_missing_autoverify,
    rule_android_foreground_service_type_missing,
    rule_android_version_code_missing_or_stuck,
)

CROSS_RULES: tuple[Callable[[StoreCtx], list[dict]], ...] = (
    rule_cross_display_name_missing,
    rule_cross_icon_missing,
    rule_cross_version_mismatch,
    rule_cross_dev_url_in_source,
    rule_cross_unguarded_test_keys,
)

PROCESS_RULES: tuple[Callable[[StoreCtx], list[dict]], ...] = (
    rule_process_privacy_policy_url_missing,
    rule_process_google_services_json_missing,
    rule_process_push_credentials_unverified,
    rule_process_iap_skus_required,
    rule_process_nutrition_label_categories,
)

ALL_RULES: tuple[Callable[[StoreCtx], list[dict]], ...] = (
    APPLE_RULES + GOOGLE_RULES + CROSS_RULES + PROCESS_RULES
)
