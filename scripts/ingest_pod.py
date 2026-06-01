#!/usr/bin/env python3
"""
Stage 2 — Ingest source from the pod into the audit workspace.

The actual file-fetching is performed by the LLM via envcore_gateway MCP tool
calls (Claude has direct access to the MCP; a Python subprocess does not). This
script's role is to OWN the allowlist / denylist and to VALIDATE the result.

Three modes:

  --print-manifest          Print the allowlist + denylist as JSON to stdout.
                            SKILL.md Step 2 reads this, then instructs the LLM
                            to walk the pod with envcore_gateway and fetch every
                            allowlisted path that survives the denylist filter.

  --validate <workspace>    Confirm the workspace was populated correctly:
                              - every required allowlist file is present (warn on
                                missing optional ones)
                              - no denylisted path / secret-pattern file leaked in
                              - workspace tree is rooted as expected
                            Exits 0 on success, non-zero on validation failure.

  --gather-local <src> <dst>   Local development helper (no MCP needed). Walks
                               <src> using the same allowlist/denylist and copies
                               to <dst>. Useful for testing the pipeline against
                               a checked-out project before integrating with envcore.

Allowlist and denylist are the source of truth — edit here, not in SKILL.md.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# Allowlist — paths fetched from the pod into workspace/
# ──────────────────────────────────────────────────────────────────────────────
#
# Tuple shape: (path_relative_to_pod_root, kind, required)
#   kind     : "file" or "dir"
#   required : True  -> validate() fails if missing
#              False -> validate() warns but continues
#
ALLOWLIST: list[tuple[str, str, bool]] = [
    # Package + lock
    ("package.json",              "file", True),
    ("package-lock.json",         "file", False),
    ("yarn.lock",                 "file", False),
    ("pnpm-lock.yaml",            "file", False),

    # Expo / RN config
    ("app.json",                  "file", False),
    ("app.config.js",             "file", False),
    ("app.config.ts",             "file", False),
    ("eas.json",                  "file", False),
    ("expo-env.d.ts",             "file", False),

    # Build / tooling config
    ("tsconfig.json",             "file", False),
    ("babel.config.js",           "file", False),
    ("metro.config.js",           "file", False),
    ("metro.config.ts",           "file", False),
    (".eslintrc.js",              "file", False),
    (".eslintrc.json",            "file", False),
    ("eslint.config.js",          "file", False),

    # Source trees (frontend)
    ("app",                       "dir",  False),  # Expo Router
    ("src",                       "dir",  False),
    ("components",                "dir",  False),
    ("screens",                   "dir",  False),
    ("lib",                       "dir",  False),
    ("hooks",                     "dir",  False),
    ("utils",                     "dir",  False),
    ("constants",                 "dir",  False),
    ("types",                     "dir",  False),

    # Assets
    ("assets",                    "dir",  False),

    # Backend source trees (Stage 4f — FastAPI/Python perf checks).
    # Emergent's split layout is /app/frontend + /app/backend; for that case,
    # ingest the backend separately from /app/backend and land at workspace/backend/.
    # The backend_scan worker reads from workspace/backend/ if present; otherwise
    # the stage is skipped cleanly.
    ("backend",                   "dir",  False),
    ("server",                    "dir",  False),  # alt layout
    ("api",                       "dir",  False),  # alt layout
    ("requirements.txt",          "file", False),
    ("pyproject.toml",            "file", False),
    ("poetry.lock",               "file", False),
    ("uv.lock",                   "file", False),

    # Optional user overrides
    (".audit",                    "dir",  False),
]

# ──────────────────────────────────────────────────────────────────────────────
# Denylist — paths/patterns that must never be fetched, even if inside an
# allowlisted directory. Secrets and build outputs.
# ──────────────────────────────────────────────────────────────────────────────
DENYLIST_DIRS = {
    "node_modules",
    ".expo",
    ".expo-shared",
    ".next",
    ".git",
    "build",
    "dist",
    "ios/build",
    "ios/Pods",
    "android/build",
    "android/.gradle",
    "android/app/build",
    "__tests__",       # tests excluded from perf scope; can be re-enabled per project
    "coverage",
    ".turbo",
    ".cache",
    "tmp",
    ".tmp",
    # Python (backend ingest — Stage 4f)
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "site-packages",
    "egg-info",
}

DENYLIST_FILE_GLOBS = [
    ".env",
    ".env.*",
    "*.env",
    "*secret*",
    "*credential*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.keystore",
    "*.jks",
    "GoogleService-Info.plist",
    "google-services.json",
    ".npmrc",       # may contain registry tokens
    ".yarnrc.yml",  # may contain registry tokens
    "id_rsa*",
    "id_ed25519*",
]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def is_denylisted_dir(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    for part in parts:
        if part in DENYLIST_DIRS:
            return True
    # Multi-part matches (e.g. "ios/build")
    rel_posix = Path(rel_path).as_posix()
    for denied in DENYLIST_DIRS:
        if "/" in denied and (rel_posix == denied or rel_posix.startswith(denied + "/")):
            return True
    return False


def is_denylisted_file(filename: str) -> bool:
    base = os.path.basename(filename)
    return any(fnmatch.fnmatch(base, pat) for pat in DENYLIST_FILE_GLOBS)


# ──────────────────────────────────────────────────────────────────────────────
# Mode: --print-manifest
# ──────────────────────────────────────────────────────────────────────────────

def print_manifest() -> None:
    manifest = {
        "allowlist": [
            {"path": p, "kind": k, "required": r}
            for p, k, r in ALLOWLIST
        ],
        "denylist_dirs": sorted(DENYLIST_DIRS),
        "denylist_file_globs": list(DENYLIST_FILE_GLOBS),
        "instructions": (
            "For each allowlist entry: if kind=file, fetch it if it exists on the pod. "
            "If kind=dir, recursively walk it, fetch every file whose path does not "
            "traverse a denylist_dirs entry and whose basename does not match any "
            "denylist_file_globs entry. Preserve relative paths under workspace/. "
            "Missing optional paths are not errors; missing required paths are."
        ),
    }
    print(json.dumps(manifest, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# Mode: --validate
# ──────────────────────────────────────────────────────────────────────────────

def validate(workspace: Path) -> int:
    if not workspace.is_dir():
        print(f"FAIL: workspace not found or not a directory: {workspace}", file=sys.stderr)
        return 2

    errors: list[str] = []
    warnings: list[str] = []

    # Required allowlist files must exist
    for path, kind, required in ALLOWLIST:
        target = workspace / path
        if kind == "file":
            if not target.is_file():
                msg = f"allowlist file not present: {path}"
                (errors if required else warnings).append(msg)
        elif kind == "dir":
            if not target.is_dir():
                msg = f"allowlist dir not present: {path}/"
                (errors if required else warnings).append(msg)

    # Sweep the workspace tree for any denylist leaks
    for root, dirs, files in os.walk(workspace):
        rel_root = os.path.relpath(root, workspace)
        # Prune denylisted directories
        pruned = []
        for d in list(dirs):
            sub_rel = os.path.normpath(os.path.join(rel_root, d)) if rel_root != "." else d
            if is_denylisted_dir(sub_rel):
                errors.append(f"denylist directory leaked into workspace: {sub_rel}/")
                pruned.append(d)
        for d in pruned:
            dirs.remove(d)
        # Check files
        for f in files:
            if is_denylisted_file(f):
                rel_file = os.path.normpath(os.path.join(rel_root, f)) if rel_root != "." else f
                errors.append(f"denylist file leaked into workspace: {rel_file}")

    # At least one source-tree dir must be present, otherwise nothing to analyse
    source_dirs_present = any(
        (workspace / d).is_dir() for d in ("app", "src", "components", "screens")
    )
    if not source_dirs_present:
        errors.append("no source directory present (need at least one of app/, src/, components/, screens/)")

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    for e in errors:
        print(f"FAIL: {e}", file=sys.stderr)

    if errors:
        print(f"\nValidation FAILED: {len(errors)} error(s), {len(warnings)} warning(s)", file=sys.stderr)
        return 1
    print(f"Validation OK: 0 errors, {len(warnings)} warning(s)", file=sys.stderr)
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# Mode: --gather-local (development helper)
# ──────────────────────────────────────────────────────────────────────────────

def gather_local(src: Path, dst: Path) -> int:
    if not src.is_dir():
        print(f"FAIL: source not found: {src}", file=sys.stderr)
        return 2
    dst.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped_denylist = 0
    missing_required: list[str] = []

    for path, kind, required in ALLOWLIST:
        s = src / path
        d = dst / path
        if not s.exists():
            if required:
                missing_required.append(path)
            continue
        if kind == "file":
            if is_denylisted_file(s.name):
                skipped_denylist += 1
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            copied += 1
        elif kind == "dir":
            for root, dirs, files in os.walk(s):
                rel_root = os.path.relpath(root, s)
                # Prune denylist dirs
                dirs[:] = [
                    sub for sub in dirs
                    if not is_denylisted_dir(
                        os.path.normpath(os.path.join(path, rel_root, sub)) if rel_root != "." else os.path.join(path, sub)
                    )
                ]
                for f in files:
                    if is_denylisted_file(f):
                        skipped_denylist += 1
                        continue
                    s_file = Path(root) / f
                    rel_file = s_file.relative_to(src)
                    d_file = dst / rel_file
                    d_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s_file, d_file)
                    copied += 1

    if missing_required:
        print(f"FAIL: required allowlist paths missing in source: {missing_required}", file=sys.stderr)
        return 1
    print(f"Gathered {copied} file(s) into {dst}. Skipped {skipped_denylist} denylist match(es).", file=sys.stderr)
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Ingest manifest and validation for the mobile perf audit.")
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument("--print-manifest", action="store_true", help="Print the allowlist/denylist manifest as JSON to stdout.")
    sub.add_argument("--validate", metavar="WORKSPACE", type=Path, help="Validate the workspace was populated correctly.")
    sub.add_argument("--gather-local", nargs=2, metavar=("SRC", "DST"), help="Local development: copy from SRC to DST using the allowlist/denylist.")
    args = p.parse_args()

    if args.print_manifest:
        print_manifest()
        return 0
    if args.validate:
        return validate(args.validate)
    if args.gather_local:
        src, dst = args.gather_local
        return gather_local(Path(src), Path(dst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
