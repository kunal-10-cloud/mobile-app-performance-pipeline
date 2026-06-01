"""
SDK disclosure matrix — single source of truth for what each tracking/analytics/
payment SDK requires the app to declare in the App Store Privacy Nutrition Labels
and the Play Store Data Safety form.

Drives three rules in `configs/store_rules.py`:
  - store.ios.privacy_manifest_missing_for_sdk
  - store.ios.tracking_transparency_missing
  - store.process.nutrition_label_categories

Schema per entry:
  package_pattern      — regex matched against package.json dependency names
  category             — short label for which class this SDK is (analytics / push / etc.)
  needs_privacy_manifest — True if Apple's PrivacyInfo.xcprivacy must mention this SDK
  needs_att            — True if NSUserTrackingUsageDescription must be set (tracking-class)
  apple_categories     — Apple Privacy Nutrition Label categories the user must declare
  google_categories    — Play Data Safety categories the user must declare
  manifest_url         — Apple privacy-manifest reference (vendor docs / Apple commonly-used list)

Add new SDKs by appending a row. Pattern is a regex; match is case-insensitive.
Keep entries terse — this drives a customer-facing checklist, not legal advice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SDKDisclosure:
    package_pattern: str
    label: str
    sdk_class: str  # 'analytics' | 'crash' | 'push' | 'payments' | 'auth' | 'media' | 'other'
    needs_privacy_manifest: bool
    needs_att: bool  # tracking-class — requires NSUserTrackingUsageDescription
    apple_categories: tuple[str, ...] = field(default_factory=tuple)
    google_categories: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


# Seed matrix — covers the SDKs we see most often in Emergent's customer apps.
# Order matters only insofar as the first pattern match wins per dependency.
SDK_MATRIX: tuple[SDKDisclosure, ...] = (
    # ── Firebase suite ──────────────────────────────────────────────────────
    SDKDisclosure(
        package_pattern=r"^@react-native-firebase/analytics$|^firebase$|^@firebase/analytics$",
        label="Firebase Analytics",
        sdk_class="analytics",
        needs_privacy_manifest=True,
        needs_att=False,  # Apple's 1st-party exemption usually applies, but see notes
        apple_categories=(
            "Usage Data → Product Interaction",
            "Usage Data → Other Usage Data",
            "Identifiers → User ID",
            "Diagnostics → Crash Data",
            "Diagnostics → Performance Data",
        ),
        google_categories=(
            "App activity → App interactions",
            "App info and performance → Crash logs",
            "App info and performance → Diagnostics",
            "Device or other IDs",
        ),
        notes=(
            "Firebase Analytics historically uses the IDFA only when you enable "
            "IDFA collection explicitly. If you do, ATT becomes required and "
            "`needs_att` flips to True. Audit your Firebase init for "
            "`setAnalyticsCollectionEnabled` / IDFA settings."
        ),
    ),
    SDKDisclosure(
        package_pattern=r"^@react-native-firebase/crashlytics$",
        label="Firebase Crashlytics",
        sdk_class="crash",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=(
            "Diagnostics → Crash Data",
            "Diagnostics → Performance Data",
        ),
        google_categories=(
            "App info and performance → Crash logs",
            "App info and performance → Diagnostics",
        ),
    ),
    SDKDisclosure(
        package_pattern=r"^@react-native-firebase/messaging$",
        label="Firebase Cloud Messaging",
        sdk_class="push",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=("Identifiers → Device ID",),
        google_categories=("Device or other IDs",),
        notes="Push device token is treated as an identifier in both stores.",
    ),
    SDKDisclosure(
        package_pattern=r"^@react-native-firebase/app$",
        label="Firebase Core (app)",
        sdk_class="other",
        needs_privacy_manifest=True,
        needs_att=False,
        # Core itself uses required-reason APIs (UserDefaults, file timestamps).
        # Declared categories are minimal — the children (analytics, etc.) carry the data.
        notes="Bootstraps Firebase; uses required-reason APIs that the privacy manifest must list.",
    ),

    # ── Crash / observability ───────────────────────────────────────────────
    SDKDisclosure(
        package_pattern=r"^@sentry/react-native$|^sentry-expo$",
        label="Sentry",
        sdk_class="crash",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=(
            "Diagnostics → Crash Data",
            "Diagnostics → Performance Data",
            "Identifiers → User ID  (if you call `setUser`)",
        ),
        google_categories=(
            "App info and performance → Crash logs",
            "App info and performance → Diagnostics",
        ),
    ),
    SDKDisclosure(
        package_pattern=r"^bugsnag-react-native$|^@bugsnag/react-native$",
        label="Bugsnag",
        sdk_class="crash",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=(
            "Diagnostics → Crash Data",
            "Diagnostics → Performance Data",
        ),
        google_categories=(
            "App info and performance → Crash logs",
            "App info and performance → Diagnostics",
        ),
    ),

    # ── Product analytics ───────────────────────────────────────────────────
    SDKDisclosure(
        package_pattern=r"^posthog-react-native$|^posthog-js$",
        label="PostHog",
        sdk_class="analytics",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=(
            "Usage Data → Product Interaction",
            "Identifiers → User ID",
        ),
        google_categories=(
            "App activity → App interactions",
            "Device or other IDs",
        ),
    ),
    SDKDisclosure(
        package_pattern=r"^@amplitude/react-native$|^@amplitude/analytics-react-native$|^react-native-amplitude-analytics$",
        label="Amplitude",
        sdk_class="analytics",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=(
            "Usage Data → Product Interaction",
            "Identifiers → User ID",
        ),
        google_categories=(
            "App activity → App interactions",
            "Device or other IDs",
        ),
    ),
    SDKDisclosure(
        package_pattern=r"^mixpanel-react-native$",
        label="Mixpanel",
        sdk_class="analytics",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=(
            "Usage Data → Product Interaction",
            "Identifiers → User ID",
        ),
        google_categories=(
            "App activity → App interactions",
            "Device or other IDs",
        ),
    ),

    # ── Attribution / tracking — these DO require ATT ──────────────────────
    SDKDisclosure(
        package_pattern=r"^branch-sdk$|^react-native-branch$",
        label="Branch",
        sdk_class="analytics",
        needs_privacy_manifest=True,
        needs_att=True,
        apple_categories=(
            "Identifiers → Device ID",
            "Identifiers → User ID",
            "Usage Data → Product Interaction",
        ),
        google_categories=(
            "Device or other IDs",
            "App activity → App interactions",
        ),
        notes="Attribution uses IDFA on iOS → ATT prompt required.",
    ),
    SDKDisclosure(
        package_pattern=r"^react-native-adjust$|^adjust-react-native$",
        label="Adjust",
        sdk_class="analytics",
        needs_privacy_manifest=True,
        needs_att=True,
        apple_categories=(
            "Identifiers → Device ID",
            "Usage Data → Product Interaction",
        ),
        google_categories=(
            "Device or other IDs",
            "App activity → App interactions",
        ),
    ),
    SDKDisclosure(
        package_pattern=r"^@appsflyer/react-native|^react-native-appsflyer",
        label="AppsFlyer",
        sdk_class="analytics",
        needs_privacy_manifest=True,
        needs_att=True,
        apple_categories=(
            "Identifiers → Device ID",
            "Usage Data → Product Interaction",
        ),
        google_categories=(
            "Device or other IDs",
            "App activity → App interactions",
        ),
    ),

    # ── Subscriptions / IAP ─────────────────────────────────────────────────
    SDKDisclosure(
        package_pattern=r"^react-native-iap$|^expo-in-app-purchases$",
        label="React Native IAP",
        sdk_class="payments",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=("Purchases → Purchase History",),
        google_categories=("Financial info → Purchase history",),
        notes=(
            "You must create matching SKUs in App Store Connect AND Play Console "
            "before submitting. See the SKU enumeration block."
        ),
    ),
    SDKDisclosure(
        package_pattern=r"^react-native-purchases$|^@revenuecat/.+",
        label="RevenueCat",
        sdk_class="payments",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=(
            "Purchases → Purchase History",
            "Identifiers → User ID",
        ),
        google_categories=(
            "Financial info → Purchase history",
            "Device or other IDs",
        ),
    ),

    # ── Push (non-Firebase) ─────────────────────────────────────────────────
    SDKDisclosure(
        package_pattern=r"^react-native-onesignal$|^onesignal-expo-plugin$",
        label="OneSignal",
        sdk_class="push",
        needs_privacy_manifest=True,
        needs_att=False,
        apple_categories=(
            "Identifiers → Device ID",
            "Contact Info → Email Address  (if you collect it)",
        ),
        google_categories=("Device or other IDs",),
    ),
    SDKDisclosure(
        package_pattern=r"^expo-notifications$",
        label="Expo Notifications",
        sdk_class="push",
        needs_privacy_manifest=False,  # Apple/Expo-managed first-party
        needs_att=False,
        apple_categories=("Identifiers → Device ID",),
        google_categories=("Device or other IDs",),
        notes="Push device token = identifier. Expo's manifest covers required-reason APIs.",
    ),

    # ── Auth / OAuth (no analytics, listed for completeness) ───────────────
    SDKDisclosure(
        package_pattern=r"^expo-auth-session$|^expo-web-browser$",
        label="Expo Auth Session",
        sdk_class="auth",
        needs_privacy_manifest=False,
        needs_att=False,
        notes="OAuth-in-browser; no data collection of its own. Listing is for completeness.",
    ),
)


_compiled_patterns: list[tuple[re.Pattern, SDKDisclosure]] = [
    (re.compile(d.package_pattern, re.IGNORECASE), d) for d in SDK_MATRIX
]


def disclosure_for(package_name: str) -> SDKDisclosure | None:
    """Return the matching SDKDisclosure for a dependency name, or None."""
    for pattern, disclosure in _compiled_patterns:
        if pattern.search(package_name):
            return disclosure
    return None


def detect_disclosures(dependencies: dict[str, str]) -> list[SDKDisclosure]:
    """Walk a package.json dependencies dict, return ordered unique disclosures.
    Dedup by label so we don't report Firebase Core + Analytics as two separate
    items when the user reads it — though both privacy-manifest entries are listed."""
    seen: dict[str, SDKDisclosure] = {}
    for pkg in dependencies.keys():
        d = disclosure_for(pkg)
        if d is None:
            continue
        if d.label not in seen:
            seen[d.label] = d
    return list(seen.values())


def any_tracking_class(disclosures: list[SDKDisclosure]) -> bool:
    """True if any detected SDK requires App Tracking Transparency."""
    return any(d.needs_att for d in disclosures)


def all_privacy_manifest_requirers(disclosures: list[SDKDisclosure]) -> list[SDKDisclosure]:
    return [d for d in disclosures if d.needs_privacy_manifest]
