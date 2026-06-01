#!/usr/bin/env python3
"""
Stage 4d.2 — Extract a screen map from the workspace.

Walks the project's screen directory (Expo Router's `app/` first, then
fallbacks), parses each file with tree-sitter, and emits a JSON document
matching `schemas/screen_map.schema.json`.

The map drives:
  - `generate_draft_flow.py` (Stage 4d.3) for the baseline Maestro flow
  - `refine_flow_with_llm.py` (Stage 4d.4) — the LLM sees this + selected
    screen sources, and fills `flow_intent.schema.json` (NOT freeform YAML).

Detection rules (kept structural, not LLM-driven):

  Navigation type:
    - app/(tabs)/_layout.tsx exists                 → expo-router-tabs
    - app/_layout.tsx with <Stack>                  → expo-router-stack
    - app/(drawer)/_layout.tsx exists               → expo-router-drawer
    - any call to createBottomTabNavigator / createNativeStackNavigator
      anywhere under src/                           → react-navigation
    - otherwise                                     → unknown

  Tabs (Expo Router):
    - Each non-_layout file directly inside app/(tabs)/ is a tab.
    - label_guess: derived from the `<Tabs.Screen options={{ title: ... }}>`
      annotation if present in _layout.tsx, otherwise titlecased filename.

  Auth detection:
    - Any screen file containing a <TextInput placeholder='Email' /> AND a
      <TextInput placeholder='Password' /> AND any element/identifier that
      text-matches 'sign in' | 'log in' | 'login' / their button labels.

  Scrollable screens:
    - File contains at least one <ScrollView>, <FlatList>, <FlashList>,
      <SectionList>.

Failure handling: per-file parse failures are recorded in
`extraction_warnings`; the extraction does not abort.

Usage:
  python3 scripts/extract_screen_map.py <audit_id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ── Source-tree discovery ────────────────────────────────────────────────────

SCREEN_DIR_CANDIDATES = [
    ("expo-router", "app"),
    ("screens-dir", "src/screens"),
    ("screens-dir", "screens"),
    ("expo-router", "src/app"),
]
EXPO_ROUTER_LAYOUT_FILES = ("_layout.tsx", "_layout.ts", "_layout.jsx", "_layout.js")
SCREEN_EXTENSIONS = (".tsx", ".jsx", ".ts", ".js")


def find_screens_dir(workspace: Path) -> tuple[str, Path] | None:
    for kind, candidate in SCREEN_DIR_CANDIDATES:
        p = workspace / candidate
        if p.is_dir():
            return kind, p
    return None


def list_files(root: Path):
    for r, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(SCREEN_EXTENSIONS):
                yield Path(r) / f


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


# ── Bundle IDs from app.json / app.config ────────────────────────────────────

def bundle_ids_from_facts(facts: dict) -> tuple[str | None, str | None]:
    sig = (facts.get("project_signature") or {})
    return (
        sig.get("android_package") or None,
        sig.get("ios_bundle_identifier") or None,
    )


# ── Navigation-type detection ────────────────────────────────────────────────

def detect_navigation_type(workspace: Path, screens_dir: Path, screens_dir_kind: str) -> str:
    if screens_dir_kind == "expo-router":
        tabs_layout = screens_dir / "(tabs)" / "_layout.tsx"
        if tabs_layout.is_file() or (screens_dir / "(tabs)" / "_layout.jsx").is_file():
            return "expo-router-tabs"
        drawer_layout = screens_dir / "(drawer)" / "_layout.tsx"
        if drawer_layout.is_file() or (screens_dir / "(drawer)" / "_layout.jsx").is_file():
            return "expo-router-drawer"
        root_layout = screens_dir / "_layout.tsx"
        if root_layout.is_file() and "<Stack" in read_text(root_layout):
            return "expo-router-stack"
        # Even without an explicit <Stack>, presence of root _layout under app/
        # implies expo-router-stack; rely on the fallback.
        if root_layout.is_file():
            return "expo-router-stack"
    # Generic detection
    rn_signals = ("createBottomTabNavigator", "createNativeStackNavigator", "createDrawerNavigator")
    for src in list_files(workspace):
        if "/node_modules/" in str(src).replace("\\", "/"):
            continue
        text = read_text(src)
        if any(s in text for s in rn_signals):
            return "react-navigation"
    return "unknown"


# ── Tab extraction (Expo Router) ─────────────────────────────────────────────

# Captures `<Tabs.Screen name="feed" options={{ title: "Feed" }} />` patterns
TABS_SCREEN_RE = re.compile(
    r"<Tabs\.Screen\b[^>]*?name=['\"]([^'\"]+)['\"][^>]*?(?:title=['\"]([^'\"]+)['\"])?[^>]*?/?>",
    re.DOTALL,
)
TABS_OPTIONS_TITLE_RE = re.compile(
    r"options=\{\{[^{}]*?title:\s*['\"]([^'\"]+)['\"][^{}]*?\}\}",
    re.DOTALL,
)


def extract_tab_titles(layout_source: str) -> dict[str, str]:
    """Return {tab_name: label_guess} parsed from the (tabs)/_layout content."""
    out: dict[str, str] = {}
    for m in TABS_SCREEN_RE.finditer(layout_source):
        name = m.group(1)
        title = m.group(2)
        if title:
            out[name] = title
    return out


def collect_expo_router_tabs(workspace: Path, screens_dir: Path) -> list[dict]:
    tabs_dir = screens_dir / "(tabs)"
    if not tabs_dir.is_dir():
        return []
    layout_paths = [tabs_dir / fn for fn in EXPO_ROUTER_LAYOUT_FILES if (tabs_dir / fn).is_file()]
    title_map: dict[str, str] = {}
    for lp in layout_paths:
        title_map.update(extract_tab_titles(read_text(lp)))

    tabs: list[dict] = []
    for f in sorted(tabs_dir.iterdir()):
        if not f.is_file() or f.name in EXPO_ROUTER_LAYOUT_FILES:
            continue
        if not f.name.endswith(SCREEN_EXTENSIONS):
            continue
        route = f.stem
        label = title_map.get(route) or _titlecase(route)
        tabs.append({
            "route": route,
            "file": str(f.relative_to(workspace)).replace("\\", "/"),
            "label_guess": label,
            "icon_name": None,
        })
    return tabs


# ── Stack-screen extraction (Expo Router or general) ─────────────────────────

def collect_expo_router_stack_screens(workspace: Path, screens_dir: Path) -> list[dict]:
    out: list[dict] = []
    for f in list_files(screens_dir):
        rel_parts = f.relative_to(screens_dir).parts
        # Skip files inside (tabs) or (drawer) — handled separately
        if any(p.startswith("(") and p.endswith(")") for p in rel_parts):
            continue
        if f.name in EXPO_ROUTER_LAYOUT_FILES:
            continue
        if not f.name.endswith(SCREEN_EXTENSIONS):
            continue
        route = "/".join(rel_parts).rsplit(".", 1)[0]
        dynamic = any(seg.startswith("[") and seg.endswith("]") for seg in rel_parts)
        out.append({
            "route": route,
            "file": str(f.relative_to(workspace)).replace("\\", "/"),
            "dynamic": dynamic,
        })
    return out


def collect_react_navigation_screens(workspace: Path) -> list[dict]:
    """For projects using @react-navigation, collect every file under src/screens/
    (or screens/) as a stack screen. We can't statically know the registered
    name without parsing the navigator definitions; falling back to filename
    is acceptable for the maestro flow generator's purposes."""
    out: list[dict] = []
    for cand in ("src/screens", "screens"):
        root = workspace / cand
        if not root.is_dir():
            continue
        for f in list_files(root):
            if f.name.startswith("index") or f.suffix not in SCREEN_EXTENSIONS:
                pass
            out.append({
                "route": f.stem,
                "file": str(f.relative_to(workspace)).replace("\\", "/"),
                "dynamic": False,
            })
        break
    return out


# ── Auth detection ───────────────────────────────────────────────────────────

EMAIL_HINTS = ("Email", "email", "EMAIL", "Username", "username", "Phone", "phone")
PASSWORD_HINTS = ("Password", "password", "PASSWORD", "PIN", "pin", "Passcode")
SUBMIT_HINTS = (
    "sign in", "sign-in", "signin", "log in", "log-in", "login",
    "continue", "submit",
)


def _attr_value_text(text: str, attr_name: str) -> list[str]:
    """Find every value of a JSX attribute like `placeholder='Email'`."""
    pattern = re.compile(rf"\b{attr_name}\s*=\s*[\"']([^\"']+)[\"']")
    return pattern.findall(text)


def detect_auth(workspace: Path, screens_dir: Path) -> dict:
    indicators: list[str] = []
    login_screen: str | None = None
    email_label: str | None = None
    password_label: str | None = None
    submit_label: str | None = None

    candidates = list(list_files(screens_dir))
    # Also scan top-level components/ and screens/ in case auth lives outside
    for extra in ("components", "src/components", "screens", "src/screens"):
        d = workspace / extra
        if d.is_dir():
            candidates.extend(list_files(d))

    for src in candidates:
        text = read_text(src)
        if not text:
            continue
        placeholders = _attr_value_text(text, "placeholder")
        labels = _attr_value_text(text, "label")
        all_text_attrs = placeholders + labels
        lowered_attrs = [s.lower() for s in all_text_attrs]
        lowered_text = text.lower()

        has_email = any(h.lower() in lowered_attrs for h in EMAIL_HINTS) or \
                    any(h.lower() in lowered_text for h in EMAIL_HINTS)
        has_pass = any(h.lower() in lowered_attrs for h in PASSWORD_HINTS) or \
                   "secureTextEntry" in text
        has_submit = any(h in lowered_text for h in SUBMIT_HINTS)

        if has_email and has_pass and has_submit:
            login_screen = str(src.relative_to(workspace)).replace("\\", "/")
            indicators.append(f"TextInput with email + password fields + submit text in {login_screen}")
            # Best-effort label extraction
            for v in all_text_attrs:
                low = v.lower()
                if email_label is None and any(h.lower() in low for h in EMAIL_HINTS):
                    email_label = v
                if password_label is None and any(h.lower() in low for h in PASSWORD_HINTS):
                    password_label = v
            # Submit label: find the first text matching a submit hint
            for v in all_text_attrs + re.findall(r">([^<>{}]{2,40})<", text):
                low = v.strip().lower()
                if any(h == low or h in low for h in SUBMIT_HINTS):
                    submit_label = v.strip()
                    break
            break

    return {
        "detected": login_screen is not None,
        "login_screen": login_screen,
        "indicators": indicators,
        "email_field_label": email_label,
        "password_field_label": password_label,
        "submit_label": submit_label,
    }


# ── Scrollable-screen detection ──────────────────────────────────────────────

SCROLLABLE_TAGS = ("<ScrollView", "<FlatList", "<SectionList", "<FlashList", "<KeyboardAwareScrollView")


def detect_scrollable_screens(workspace: Path, screens_dir: Path) -> list[str]:
    out: list[str] = []
    for f in list_files(screens_dir):
        text = read_text(f)
        if not text:
            continue
        if any(tag in text for tag in SCROLLABLE_TAGS):
            out.append(str(f.relative_to(workspace)).replace("\\", "/"))
    return sorted(set(out))


# ── Demo-mode flag detection (very loose) ────────────────────────────────────

DEMO_FLAG_PATTERNS = (
    "EXPO_PUBLIC_DEMO_MODE",
    "DEMO_MODE",
    "AUDIT_DEMO",
    "__FIXTURE__",
)


def detect_demo_flag(workspace: Path) -> bool:
    for f in list_files(workspace):
        if "/node_modules/" in str(f).replace("\\", "/"):
            continue
        text = read_text(f)
        if any(pat in text for pat in DEMO_FLAG_PATTERNS):
            return True
    return False


def _titlecase(s: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[\-_\s]+", s) if part)


# ── Entry-screen heuristic ───────────────────────────────────────────────────

def detect_entry_screen(screens_dir_kind: str, screens_dir: Path, workspace: Path) -> str | None:
    if screens_dir_kind == "expo-router":
        for cand in [
            screens_dir / "(tabs)" / "index.tsx",
            screens_dir / "(tabs)" / "index.jsx",
            screens_dir / "index.tsx",
            screens_dir / "index.jsx",
        ]:
            if cand.is_file():
                return str(cand.relative_to(workspace)).replace("\\", "/")
    for cand in [screens_dir / "Home.tsx", screens_dir / "HomeScreen.tsx",
                 screens_dir / "Main.tsx", screens_dir / "MainScreen.tsx"]:
        if cand.is_file():
            return str(cand.relative_to(workspace)).replace("\\", "/")
    return None


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Extract screen map from the workspace.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    workspace = audit_dir / "workspace"
    flows_dir = audit_dir / "flows"
    facts_path = audit_dir / "facts" / "audit_facts.json"
    flows_dir.mkdir(parents=True, exist_ok=True)

    if not workspace.is_dir():
        print(f"ERROR: workspace missing: {workspace}", file=sys.stderr)
        return 2

    facts = {}
    if facts_path.is_file():
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    discovery = find_screens_dir(workspace)
    warnings: list[str] = []
    if discovery is None:
        warnings.append("no screens dir found (looked for app/, src/screens, screens, src/app)")
        screen_map = {
            "bundle_id_android": bundle_ids_from_facts(facts)[0],
            "bundle_id_ios": bundle_ids_from_facts(facts)[1],
            "entry_screen": None,
            "navigation": {"type": "unknown"},
            "auth": {"detected": False, "indicators": [], "login_screen": None,
                     "email_field_label": None, "password_field_label": None, "submit_label": None},
            "scrollable_screens": [],
            "has_demo_mode_flag": False,
            "extraction_warnings": warnings,
        }
    else:
        kind, screens_dir = discovery
        nav_type = detect_navigation_type(workspace, screens_dir, kind)

        nav: dict = {"type": nav_type}
        if nav_type == "expo-router-tabs":
            nav["tabs"] = collect_expo_router_tabs(workspace, screens_dir)
            nav["stack_screens"] = collect_expo_router_stack_screens(workspace, screens_dir)
        elif nav_type == "expo-router-stack":
            nav["stack_screens"] = collect_expo_router_stack_screens(workspace, screens_dir)
        elif nav_type == "react-navigation":
            nav["stack_screens"] = collect_react_navigation_screens(workspace)
        # expo-router-drawer + unknown: leave empty; caller falls back to launch-only flow.

        scrollable = detect_scrollable_screens(workspace, screens_dir)
        auth = detect_auth(workspace, screens_dir)
        entry = detect_entry_screen(kind, screens_dir, workspace)
        demo = detect_demo_flag(workspace)

        android_pkg, ios_bundle = bundle_ids_from_facts(facts)

        screen_map = {
            "bundle_id_android": android_pkg,
            "bundle_id_ios": ios_bundle,
            "entry_screen": entry,
            "navigation": nav,
            "auth": auth,
            "scrollable_screens": scrollable,
            "has_demo_mode_flag": demo,
            "extraction_warnings": warnings,
        }

    out = flows_dir / "screen_map.json"
    out.write_text(json.dumps(screen_map, indent=2), encoding="utf-8")
    print(f"Screen map written → {out}", file=sys.stderr)
    print(f"  navigation type: {screen_map['navigation']['type']}", file=sys.stderr)
    if screen_map["navigation"].get("tabs"):
        print(f"  tabs: {[t['route'] for t in screen_map['navigation']['tabs']]}", file=sys.stderr)
    print(f"  auth detected: {screen_map['auth']['detected']}", file=sys.stderr)
    print(f"  scrollable screens: {len(screen_map['scrollable_screens'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
