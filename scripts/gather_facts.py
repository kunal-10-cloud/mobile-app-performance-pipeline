#!/usr/bin/env python3
"""
Gather pinned codebase facts deterministically. Output: audit_facts.json.

Real parsers and AST queries — NEVER grep. Source of truth for every negative
claim in the report per references.md Rule 1.3.

Inputs:
  - ${AUDIT_DIR}/workspace/                  (populated by Stage 2)
  - ${AUDIT_DIR}/facts/audit_meta.json       (populated by Stage 3; optional)

Outputs:
  - ${AUDIT_DIR}/facts/audit_facts.json      (validated against schemas/facts.schema.json)

Usage:
  python3 scripts/gather_facts.py <audit_id>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make ast_rules importable
sys.path.insert(0, str(REPO_ROOT / "configs"))
import ast_rules  # noqa: E402

from tree_sitter import Node, Query  # noqa: E402

KNOWN_HEAVY_DEPS = {
    "moment", "lodash", "rxjs", "three", "chart.js", "monaco-editor",
    "pdf-lib", "xlsx", "jquery", "@mui/material",
}


# ──────────────────────────────────────────────────────────────────────────────
# Generic helpers
# ──────────────────────────────────────────────────────────────────────────────

def iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def iter_source_files(workspace: Path) -> Iterable[Path]:
    exts = (".js", ".jsx", ".ts", ".tsx")
    skip_dirs = {"node_modules", ".expo", ".git", "build", "dist", "ios", "android", "__tests__", ".turbo"}
    for root, dirs, files in os.walk(workspace):
        # Prune
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith(exts):
                yield Path(root) / f


def first_segment_of_dep(deps: dict, name: str) -> str | None:
    val = deps.get(name)
    if not isinstance(val, str):
        return None
    return val.lstrip("^~>=< ").split(" ")[0]


# ──────────────────────────────────────────────────────────────────────────────
# Manifest parsing — package.json, app.json, app.config.{js,ts}
# ──────────────────────────────────────────────────────────────────────────────

def parse_package_json(workspace: Path) -> tuple[dict, dict]:
    """Return (package_dict, all_deps_dict). Empty dicts on missing/invalid file."""
    pkg = load_json(workspace / "package.json") or {}
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    return pkg, deps


def parse_app_config(workspace: Path) -> dict:
    """Return the resolved expo config block.

    For app.json: pure JSON parse.
    For app.config.{js,ts}: shell out to `npx --no-install expo config --json` so we
    get Expo's own resolver (which handles function-form configs, env-var interpolation,
    dotenv, etc.). If neither is available, return {}.

    Never grep the JS file; that gives wrong answers for half of real Expo projects.
    """
    app_json = load_json(workspace / "app.json") or {}
    expo_block = app_json.get("expo", {}) if isinstance(app_json, dict) else {}
    if expo_block:
        return expo_block

    # Fall back to `npx expo config --json` if app.config.{js,ts} exists
    if (workspace / "app.config.js").exists() or (workspace / "app.config.ts").exists():
        import subprocess
        try:
            r = subprocess.run(
                ["npx", "--no-install", "expo", "config", "--json"],
                cwd=workspace, capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0 and r.stdout.strip().startswith("{"):
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    return data.get("expo", data)
        except Exception:
            pass

    return {}


def detect_package_manager(workspace: Path) -> str:
    if (workspace / "yarn.lock").exists():
        return "yarn"
    if (workspace / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (workspace / "package-lock.json").exists():
        return "npm"
    if (workspace / "package.json").exists():
        return "npm"
    return "unknown"


def detect_hermes(expo_block: dict, sdk_version: str | None) -> bool | None:
    """Returns True/False/None per facts.schema.json. None means 'not determinable'.

    SDK 50+: Hermes is default; absence of jsEngine means True.
    SDK ≤49: Hermes was opt-in; absence of jsEngine means False/unset.
    """
    js_engine = expo_block.get("jsEngine")
    if js_engine == "hermes":
        return True
    if js_engine == "jsc":
        return False
    if sdk_version:
        try:
            major = int(sdk_version.split(".")[0])
            if major >= 50:
                return True
            else:
                return False
        except Exception:
            return None
    return None


def detect_new_arch(expo_block: dict) -> bool | None:
    val = expo_block.get("newArchEnabled")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() == "true"
    return None


# ──────────────────────────────────────────────────────────────────────────────
# AST queries for source_pattern_counts
# ──────────────────────────────────────────────────────────────────────────────

def _query_count(tree, lang, source: bytes, query_string: str, capture: str) -> int:
    try:
        q = Query(lang, query_string)
    except Exception:
        return 0
    matches = q.matches(tree.root_node)
    n = 0
    for _pat, captures in matches:
        n += len(captures.get(capture, []))
    return n


# Per-pattern queries. Each captures a node tagged @<capture> we then count.

_Q_REACT_MEMO = """
[
  (call_expression
    function: (identifier) @callee (#eq? @callee "memo")
  ) @hit
  (call_expression
    function: (member_expression
      object: (identifier) @obj (#eq? @obj "React")
      property: (property_identifier) @prop (#eq? @prop "memo"))
  ) @hit
]
"""

_Q_USE_MEMO = """
(call_expression
  function: (identifier) @callee (#eq? @callee "useMemo")
) @hit
"""

_Q_USE_CALLBACK = """
(call_expression
  function: (identifier) @callee (#eq? @callee "useCallback")
) @hit
"""

_Q_USE_EFFECT = """
(call_expression
  function: (identifier) @callee (#eq? @callee "useEffect")
) @hit
"""

_Q_JSX_TAG_TEMPLATE = """
[
  (jsx_self_closing_element name: (identifier) @tag (#eq? @tag "{TAG}")) @hit
  (jsx_element open_tag: (jsx_opening_element name: (identifier) @tag (#eq? @tag "{TAG}"))) @hit
]
"""


def gather_source_pattern_counts(workspace: Path) -> dict:
    """Walk all source files, run a battery of AST queries, return aggregated counts."""
    counts = {
        "react_memo_count": 0,
        "use_memo_count": 0,
        "use_callback_count": 0,
        "use_effect_count": 0,
        "use_effect_with_deps_count": 0,
        "use_effect_empty_deps_count": 0,
        "scrollview_count": 0,
        "flatlist_count": 0,
        "sectionlist_count": 0,
        "flashlist_count": 0,
        "rn_image_usage_count": 0,
        "expo_image_usage_count": 0,
        "console_log_count": 0,
        "console_log_dev_guarded_count": 0,
        "animated_rn_import_count": 0,
        "reanimated_import_count": 0,
        "inline_arrow_renderitem_count": 0,
        "inline_object_jsx_props_count": 0,
    }

    for file_path in iter_source_files(workspace):
        try:
            source = file_path.read_bytes()
        except Exception:
            continue
        rel = str(file_path.relative_to(workspace))
        tree = ast_rules.parse_file(rel, source)
        if tree is None:
            continue
        lang = ast_rules.language_for(rel)

        # Plain queries
        counts["react_memo_count"]   += _query_count(tree, lang, source, _Q_REACT_MEMO, "hit")
        counts["use_memo_count"]     += _query_count(tree, lang, source, _Q_USE_MEMO, "hit")
        counts["use_callback_count"] += _query_count(tree, lang, source, _Q_USE_CALLBACK, "hit")
        counts["use_effect_count"]   += _query_count(tree, lang, source, _Q_USE_EFFECT, "hit")

        for tag, key in (
            ("ScrollView",  "scrollview_count"),
            ("FlatList",    "flatlist_count"),
            ("SectionList", "sectionlist_count"),
            ("FlashList",   "flashlist_count"),
        ):
            counts[key] += _query_count(tree, lang, source, _Q_JSX_TAG_TEMPLATE.replace("{TAG}", tag), "hit")

        # Imports — drives RN-vs-expo Image and Animated-vs-reanimated counts
        imports = ast_rules.import_specifiers(tree, source)
        for ident, module in imports.items():
            if ident == "Image":
                if module == "react-native":
                    # Count actual JSX usages of <Image> in this file
                    counts["rn_image_usage_count"] += _query_count(
                        tree, lang, source,
                        _Q_JSX_TAG_TEMPLATE.replace("{TAG}", "Image"), "hit",
                    )
                elif module == "expo-image":
                    counts["expo_image_usage_count"] += _query_count(
                        tree, lang, source,
                        _Q_JSX_TAG_TEMPLATE.replace("{TAG}", "Image"), "hit",
                    )
            if ident == "Animated" and module == "react-native":
                counts["animated_rn_import_count"] += 1
            if module == "react-native-reanimated":
                counts["reanimated_import_count"] += 1

        # useEffect with-deps vs empty-deps vs no-deps
        # Walk call_expressions named useEffect, inspect args
        def walk_useeffect(node: Node):
            if node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None:
                    text = source[fn.start_byte:fn.end_byte].decode("utf-8", errors="replace")
                    if text == "useEffect" or text.endswith(".useEffect"):
                        args = node.child_by_field_name("arguments")
                        if args is not None:
                            arg_nodes = [c for c in args.children if c.type not in ("(", ")", ",")]
                            if len(arg_nodes) >= 2:
                                deps_node = arg_nodes[1]
                                if deps_node.type == "array":
                                    # empty if no non-trivia children
                                    contents = [c for c in deps_node.children if c.type not in ("[", "]", ",")]
                                    if not contents:
                                        counts["use_effect_empty_deps_count"] += 1
                                    else:
                                        counts["use_effect_with_deps_count"] += 1
                                else:
                                    counts["use_effect_with_deps_count"] += 1
            for c in node.children:
                walk_useeffect(c)
        walk_useeffect(tree.root_node)

        # console.log counting + __DEV__ guarded variant
        def walk_console(node: Node, dev_guarded_ancestor=False):
            local_dev = dev_guarded_ancestor
            if node.type == "if_statement":
                cond = node.child_by_field_name("condition")
                if cond is not None and b"__DEV__" in source[cond.start_byte:cond.end_byte]:
                    local_dev = True
            if node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None and fn.type == "member_expression":
                    obj = fn.child_by_field_name("object")
                    if obj is not None and source[obj.start_byte:obj.end_byte] == b"console":
                        prop = fn.child_by_field_name("property")
                        if prop is not None:
                            method = source[prop.start_byte:prop.end_byte].decode("utf-8", errors="replace")
                            if method == "log":
                                counts["console_log_count"] += 1
                                if local_dev:
                                    counts["console_log_dev_guarded_count"] += 1
            for c in node.children:
                walk_console(c, local_dev)
        walk_console(tree.root_node)

        # Inline-arrow renderItem (counted separately from the rule, for the facts)
        def walk_renderitem(node: Node):
            if node.type in ("jsx_self_closing_element", "jsx_opening_element"):
                name = node.child_by_field_name("name")
                tag = source[name.start_byte:name.end_byte].decode("utf-8", errors="replace") if name is not None else ""
                if tag in ("FlatList", "SectionList", "FlashList"):
                    for child in node.children:
                        if child.type == "jsx_attribute":
                            attr_name = None
                            for c in child.children:
                                if c.type == "property_identifier":
                                    attr_name = c
                                    break
                            if attr_name is not None and source[attr_name.start_byte:attr_name.end_byte] == b"renderItem":
                                for c in child.children:
                                    if c.type == "jsx_expression":
                                        for cc in c.children:
                                            if cc.type == "arrow_function":
                                                counts["inline_arrow_renderitem_count"] += 1
            for c in node.children:
                walk_renderitem(c)
        walk_renderitem(tree.root_node)

        # Inline object literals as JSX props: any jsx_attribute whose value's
        # jsx_expression contains an `object` literal (e.g. style={{...}}).
        def walk_inline_obj(node: Node):
            if node.type == "jsx_attribute":
                for c in node.children:
                    if c.type == "jsx_expression":
                        for cc in c.children:
                            if cc.type == "object":
                                counts["inline_object_jsx_props_count"] += 1
            for c in node.children:
                walk_inline_obj(c)
        walk_inline_obj(tree.root_node)

    return counts


# ──────────────────────────────────────────────────────────────────────────────
# Asset analysis (images)
# ──────────────────────────────────────────────────────────────────────────────

def gather_asset_facts(workspace: Path) -> dict:
    assets_dir = workspace / "assets"
    counts = {
        "image_asset_count": 0,
        "image_asset_total_bytes": 0,
        "images_over_500kb_count": 0,
        "images_over_2mb_count": 0,
        "png_count": 0,
        "webp_count": 0,
        "svg_count": 0,
    }
    if not assets_dir.is_dir():
        return counts
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
    for root, _dirs, files in os.walk(assets_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in image_exts:
                continue
            p = Path(root) / f
            try:
                size = p.stat().st_size
            except Exception:
                continue
            counts["image_asset_count"] += 1
            counts["image_asset_total_bytes"] += size
            if size > 2_000_000:
                counts["images_over_2mb_count"] += 1
            elif size > 500_000:
                counts["images_over_500kb_count"] += 1
            if ext == ".png":
                counts["png_count"] += 1
            elif ext == ".webp":
                counts["webp_count"] += 1
            elif ext == ".svg":
                counts["svg_count"] += 1
    return counts


# ──────────────────────────────────────────────────────────────────────────────
# Presence flags
# ──────────────────────────────────────────────────────────────────────────────

def gather_presence(workspace: Path) -> dict:
    return {
        "app_dir_present":         (workspace / "app").is_dir(),
        "src_dir_present":         (workspace / "src").is_dir(),
        "screens_dir_present":     (workspace / "screens").is_dir(),
        "assets_dir_present":      (workspace / "assets").is_dir(),
        "audit_overrides_present": (workspace / ".audit").is_dir(),
        "babel_config_present":    (workspace / "babel.config.js").is_file() or (workspace / "babel.config.ts").is_file(),
        "metro_config_present":    (workspace / "metro.config.js").is_file() or (workspace / "metro.config.ts").is_file(),
        "eas_json_present":        (workspace / "eas.json").is_file(),
        "ios_dir_present":         (workspace / "ios").is_dir(),
        "android_dir_present":     (workspace / "android").is_dir(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Optional tooling outputs (best-effort; fail-soft)
# ──────────────────────────────────────────────────────────────────────────────

def gather_tooling_status(workspace: Path, audit_meta_facts: dict | None) -> dict:
    import subprocess

    status = {
        "expo_doctor_pass_count": 0,
        "expo_doctor_fail_count": 0,
        "circular_dependency_count": 0,
        "unused_dependency_count": 0,
        "outdated_dependency_count": 0,
        "npm_audit_high_count": 0,
        "npm_audit_critical_count": 0,
    }
    if audit_meta_facts:
        ed = audit_meta_facts.get("expo_doctor") or {}
        status["expo_doctor_pass_count"] = int(ed.get("passed") or 0)
        status["expo_doctor_fail_count"] = int(ed.get("failed") or 0)

    # madge --circular (best-effort)
    try:
        r = subprocess.run(
            ["npx", "--no-install", "madge", "--circular", "--json", "."],
            cwd=workspace, capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and r.stdout.strip().startswith("["):
            cycles = json.loads(r.stdout)
            if isinstance(cycles, list):
                status["circular_dependency_count"] = len(cycles)
    except Exception:
        pass

    # depcheck (best-effort)
    try:
        r = subprocess.run(
            ["npx", "--no-install", "depcheck", "--json"],
            cwd=workspace, capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout or "{}")
            unused = data.get("dependencies", []) if isinstance(data, dict) else []
            status["unused_dependency_count"] = len(unused) if isinstance(unused, list) else 0
    except Exception:
        pass

    # npm-check-updates (best-effort)
    try:
        r = subprocess.run(
            ["npx", "--no-install", "npm-check-updates", "--jsonUpgraded"],
            cwd=workspace, capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            up = json.loads(r.stdout or "{}")
            if isinstance(up, dict):
                status["outdated_dependency_count"] = len(up)
    except Exception:
        pass

    # npm audit
    try:
        r = subprocess.run(
            ["npm", "audit", "--json"],
            cwd=workspace, capture_output=True, text=True, timeout=60,
        )
        # npm audit may exit non-zero when vulns present; parse stdout anyway
        if r.stdout.strip().startswith("{"):
            data = json.loads(r.stdout)
            metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
            vulns = metadata.get("vulnerabilities", {}) if isinstance(metadata, dict) else {}
            status["npm_audit_high_count"] = int(vulns.get("high", 0))
            status["npm_audit_critical_count"] = int(vulns.get("critical", 0))
    except Exception:
        pass

    return status


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def gather_backend_facts(workspace: Path) -> dict:
    """Stage 4f facts — counts that ground backend rule cross-references and
    let `references.md` §3 Step 4 downgrade/upgrade severity confidently.

    If no backend tree was ingested, returns `{"present": False}` and the rest
    is zeroed. Quick and cheap — regex on Python source, no AST.
    """
    backend_root = None
    for candidate in ("backend", "server", "api"):
        if (workspace / candidate).is_dir():
            backend_root = workspace / candidate
            break
    if backend_root is None:
        return {"present": False}

    py_files: list[Path] = []
    for dirpath, _dirs, files in os.walk(backend_root):
        for f in files:
            if f.endswith(".py"):
                p = Path(dirpath) / f
                try:
                    if p.stat().st_size > 1024 * 1024:
                        continue
                except OSError:
                    continue
                py_files.append(p)

    # Route + handler counts
    route_re = re.compile(r"@\w+\.(get|post|put|delete|patch|options|head)\(")
    async_def_re = re.compile(r"^\s*async\s+def\s+\w+")
    sync_def_re = re.compile(r"^\s*def\s+\w+")
    create_index_re = re.compile(r"create_index\(\s*[\"'](\w+)")
    pydantic_v1_re = re.compile(r"^\s*from\s+pydantic\s+import\s+.*BaseModel|^\s*import\s+pydantic", re.MULTILINE)
    pydantic_v2_re = re.compile(r"model_config\s*=\s*ConfigDict|from\s+pydantic\s+import\s+ConfigDict")

    route_count = 0
    async_handler_count = 0
    sync_handler_count = 0
    create_index_count = 0
    indexed_collections: set[str] = set()
    pydantic_v2_signals = 0
    pydantic_present = False

    for p in py_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Count handlers by walking the file: a route_re line is followed
        # by either `async def` or `def` (usually within 3 lines, accounting
        # for additional decorators).
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if route_re.search(line):
                route_count += 1
                # peek down 1-3 lines for the function definition
                for j in range(i + 1, min(i + 5, len(lines))):
                    if async_def_re.search(lines[j]):
                        async_handler_count += 1
                        break
                    if sync_def_re.search(lines[j]):
                        sync_handler_count += 1
                        break

        for m in create_index_re.finditer(text):
            create_index_count += 1
            indexed_collections.add(m.group(1))

        if pydantic_v1_re.search(text):
            pydantic_present = True
        if pydantic_v2_re.search(text):
            pydantic_v2_signals += 1

    pydantic_major = None
    if pydantic_present:
        pydantic_major = 2 if pydantic_v2_signals > 0 else 1

    return {
        "present":               True,
        "root":                  str(backend_root.relative_to(workspace)).replace("\\", "/"),
        "python_file_count":     len(py_files),
        "route_declaration_count": route_count,
        "async_handler_count":   async_handler_count,
        "sync_handler_count":    sync_handler_count,
        "create_index_count":    create_index_count,
        "indexed_collections":   sorted(indexed_collections),
        "pydantic_major_version": pydantic_major,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Gather pinned codebase facts.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    workspace = audit_dir / "workspace"
    facts_dir = audit_dir / "facts"

    if not workspace.is_dir():
        print(f"ERROR: workspace not found: {workspace}", file=sys.stderr)
        return 2
    facts_dir.mkdir(parents=True, exist_ok=True)

    pkg, deps = parse_package_json(workspace)
    expo_block = parse_app_config(workspace)
    sdk_version = first_segment_of_dep(deps, "expo")

    project_signature = {
        "package_manager":          detect_package_manager(workspace),
        "expo_sdk_version":         sdk_version,
        "react_native_version":     first_segment_of_dep(deps, "react-native"),
        "react_version":            first_segment_of_dep(deps, "react"),
        "typescript_present":       "typescript" in deps,
        "expo_router_present":      "expo-router" in deps,
        "react_navigation_present": any(d.startswith("@react-navigation/") for d in deps),
        "hermes_enabled":           detect_hermes(expo_block, sdk_version),
        "new_architecture_enabled": detect_new_arch(expo_block),
        "bundle_identifier_android": (expo_block.get("android") or {}).get("package") if isinstance(expo_block.get("android"), dict) else None,
        "bundle_identifier_ios":     (expo_block.get("ios")     or {}).get("bundleIdentifier") if isinstance(expo_block.get("ios"), dict) else None,
    }

    dependencies = {
        "production_count":       len(pkg.get("dependencies") or {}),
        "dev_count":              len(pkg.get("devDependencies") or {}),
        "expo_image_present":     "expo-image" in deps,
        "flash_list_present":     "@shopify/flash-list" in deps,
        "reanimated_present":     "react-native-reanimated" in deps,
        "gesture_handler_present":"react-native-gesture-handler" in deps,
        "screens_present":        "react-native-screens" in deps,
        "known_heavy_deps":       sorted([d for d in deps if d in KNOWN_HEAVY_DEPS]),
    }

    print("Walking AST queries for source pattern counts...", file=sys.stderr)
    source_pattern_counts = gather_source_pattern_counts(workspace)

    print("Scanning assets...", file=sys.stderr)
    assets = gather_asset_facts(workspace)

    presence = gather_presence(workspace)

    audit_meta = load_json(facts_dir / "audit_meta.json") or {}
    print("Gathering tooling status (madge, depcheck, ncu, npm audit) — best-effort...", file=sys.stderr)
    tooling_status = gather_tooling_status(workspace, audit_meta)

    print("Gathering backend facts (Stage 4f)...", file=sys.stderr)
    backend_facts = gather_backend_facts(workspace)

    facts = {
        "audit_id": args.audit_id,
        "facts_gathered_at": iso_utc_now(),
        "workspace_root": str(workspace.resolve()),
        "project_signature": project_signature,
        "dependencies": dependencies,
        "source_pattern_counts": source_pattern_counts,
        "assets": assets,
        "presence": presence,
        "tooling_status": tooling_status,
        "backend": backend_facts,
    }

    out = facts_dir / "audit_facts.json"
    out.write_text(json.dumps(facts, indent=2), encoding="utf-8")
    print(f"wrote {out}", file=sys.stderr)
    print(json.dumps({k: facts.get(k) for k in ("project_signature", "dependencies")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
