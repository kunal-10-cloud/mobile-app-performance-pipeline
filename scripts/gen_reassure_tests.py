#!/usr/bin/env python3
"""
Stage 4c (part 1) — generate Reassure perf tests for each detected screen.

Walks the project's screen directory (Expo Router's `app/`, or `src/screens/`,
or `screens/`), identifies each file that default-exports a React component,
and emits a `__reassure_tests__/<screen>.perf-test.tsx` from
`configs/reassure-test-template.tsx`.

Detection heuristic per file:
  - Has `export default function NAME(...)` OR `export default NAME;` with
    a NAME that is PascalCase, AND
  - Body contains at least one JSX element

If the heuristic doesn't fire, the file is skipped silently — the audit
should fail open on screens we can't auto-generate for.

Failure handling: per-file errors become a single tooling.* Finding,
written to findings/reassure_gen.json; the script always exits 0.

Usage:
  python3 scripts/gen_reassure_tests.py <audit_id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "configs"))
import ast_rules  # noqa: E402


TEMPLATE_PATH = REPO_ROOT / "configs" / "reassure-test-template.tsx"

# Candidate screen directories, checked in priority order
SCREEN_DIR_CANDIDATES = ["app", "src/screens", "screens", "src/app"]

# Skip these even if they live in app/ — they're Expo Router conventions,
# not user-facing screens that benefit from render-perf measurement
EXPO_ROUTER_SPECIAL = {"_layout.tsx", "_layout.ts", "+not-found.tsx", "+html.tsx"}

# Reassure default
DEFAULT_RUNS = 10


def find_screens_dir(workspace: Path) -> Path | None:
    for candidate in SCREEN_DIR_CANDIDATES:
        p = workspace / candidate
        if p.is_dir():
            return p
    return None


def iter_screen_files(screens_dir: Path):
    for root, _dirs, files in os.walk(screens_dir):
        for f in files:
            if f.endswith((".tsx", ".jsx")) and f not in EXPO_ROUTER_SPECIAL:
                yield Path(root) / f


def _is_pascal(name: str) -> bool:
    return bool(name) and name[0].isupper() and name[0].isalpha()


def detect_component_export(source: bytes) -> str | None:
    """Return the exported component identifier, or None if no component-shaped
    default export is found."""
    text = source.decode("utf-8", errors="replace")

    # Form A: `export default function Name(...)`
    m = re.search(r"\bexport\s+default\s+function\s+([A-Za-z_$][\w$]*)\s*\(", text)
    if m and _is_pascal(m.group(1)):
        return m.group(1)

    # Form B: `export default Name;` (Name declared elsewhere as PascalCase)
    m = re.search(r"\bexport\s+default\s+([A-Za-z_$][\w$]*)\s*;", text)
    if m and _is_pascal(m.group(1)):
        return m.group(1)

    # Form C: `export default React.memo(Name)` / `export default memo(Name)`
    m = re.search(r"\bexport\s+default\s+(?:React\.)?memo\s*\(\s*([A-Za-z_$][\w$]*)\s*\)", text)
    if m and _is_pascal(m.group(1)):
        return m.group(1)

    return None


def file_contains_jsx(source: bytes, file_path: str) -> bool:
    tree = ast_rules.parse_file(file_path, source)
    if tree is None:
        return False
    return ast_rules._node_contains_jsx(tree.root_node)


def render_template(
    template: str,
    *,
    component_name: str,
    component_import_path: str,
    component_props_literal: str,
    runs: int,
) -> str:
    return (
        template
        .replace("{{COMPONENT_NAME}}", component_name)
        .replace("{{COMPONENT_IMPORT_PATH}}", component_import_path)
        .replace("{{COMPONENT_PROPS_LITERAL}}", component_props_literal)
        .replace("{{RUNS}}", str(runs))
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate Reassure perf tests for each screen.")
    ap.add_argument("audit_id")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"Number of Reassure measurement runs per test (default {DEFAULT_RUNS})")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    workspace = audit_dir / "workspace"
    findings_dir = audit_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    if not workspace.is_dir():
        print(f"ERROR: workspace missing: {workspace}", file=sys.stderr)
        return 2
    if not TEMPLATE_PATH.is_file():
        print(f"ERROR: template missing: {TEMPLATE_PATH}", file=sys.stderr)
        return 2

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    screens_dir = find_screens_dir(workspace)
    tooling_findings: list[dict] = []

    if screens_dir is None:
        tooling_findings.append({
            "id": "tooling.reassure_no_screens_dir",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "No screens directory found",
            "description": (
                "Looked for any of: " + ", ".join(SCREEN_DIR_CANDIDATES) +
                ". Reassure generation skipped; the project may use a non-standard layout."
            ),
            "evidence": {"file": str(workspace)},
        })
        (findings_dir / "reassure_gen.json").write_text(json.dumps(tooling_findings, indent=2), encoding="utf-8")
        print("No screens dir found; nothing generated.", file=sys.stderr)
        return 0

    out_dir = workspace / "__reassure_tests__"
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    skipped = 0
    for src_file in iter_screen_files(screens_dir):
        rel = src_file.relative_to(workspace)
        try:
            source = src_file.read_bytes()
        except Exception as e:
            tooling_findings.append({
                "id": "tooling.reassure_read_failed",
                "layer": "tooling",
                "category": "tooling_error",
                "severity": "low",
                "confidence": "high",
                "title": f"Could not read {rel}",
                "description": f"{type(e).__name__}: {e}",
                "evidence": {"file": str(rel)},
            })
            continue

        component_name = detect_component_export(source)
        if component_name is None or not file_contains_jsx(source, str(src_file)):
            skipped += 1
            continue

        # Import path: from `<workspace>/__reassure_tests__/<file>.perf-test.tsx`
        # back to the actual screen. Strip extension; Node/TS resolves the rest.
        # All `__reassure_tests__/*` live one level below workspace, so the
        # relative import is `../<original_path_minus_ext>`.
        import_path = "../" + str(rel).replace("\\", "/").rsplit(".", 1)[0]

        # Test file naming: flatten the screen path into a stable filename
        flat = str(rel).replace("\\", "/").replace("/", "__").rsplit(".", 1)[0]
        out_file = out_dir / f"{flat}.perf-test.tsx"

        # Best-effort default props. Without static type info we can't infer
        # the real shape; an empty object covers the no-props case which is
        # most common for screens. If the screen requires props, the test will
        # throw and surface as a Finding — the operator can drop a hand-written
        # test alongside.
        rendered = render_template(
            template,
            component_name=component_name,
            component_import_path=import_path,
            component_props_literal="{}",
            runs=args.runs,
        )
        out_file.write_text(rendered, encoding="utf-8")
        generated += 1

    (findings_dir / "reassure_gen.json").write_text(json.dumps(tooling_findings, indent=2), encoding="utf-8")
    print(f"Reassure tests: generated {generated}, skipped {skipped} non-component files.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
