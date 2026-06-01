#!/usr/bin/env python3
"""
Stage 4f — Backend / DB / algorithm perf scan.

Walks the backend source under `workspace/backend/` (or `workspace/server/`,
`workspace/api/` as fallbacks), runs every rule in `configs/backend_rules.py`,
and writes `findings/backend.json`.

Coverage ported verbatim from the web pipeline's perf-audit:
  - PERF-005 sync route handler           (backend.sync_route_handler)
  - PERF-006 N+1 queries                  (backend.n_plus_one_query)
  - PERF-007 unbounded queries            (backend.unbounded_query)
  - PERF-009 blocking work in handler     (backend.blocking_work_in_handler)
  - PERF-010 sequential awaits            (backend.sequential_await_chain)
  - PERF-010 (JS) promise parallelisation (backend.sequential_fetch_chain)
  - PERF-012 missing index                (database.missing_index)
  - PERF-013 complex Pydantic model       (backend.pydantic_complex_model)
  - PERF-014 array lookup in loop         (algorithms.linear_array_lookup_in_loop)
  - PERF-015 nested iteration             (algorithms.nested_iteration)
  - PERF-016 no projection                (backend.no_projection_on_query)
  - mongo_singleton                       (backend.mongo_client_not_singleton)

If no backend source was ingested, emits a single tooling.* finding noting that
the stage was skipped, then exits 0 (so the rest of the pipeline keeps going).

Usage:
  python3 scripts/backend_scan.py <audit_id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "configs"))

import backend_rules  # noqa: E402


# Candidate backend source roots, checked in order. First match wins.
BACKEND_DIR_CANDIDATES = ("backend", "server", "api")

# Extensions we treat as Python vs polyglot-JS for the rule split.
PY_EXTS = (".py",)
JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs")

# Bounded file size; pathological large files are skipped.
MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB


def find_backend_root(workspace: Path) -> Path | None:
    for d in BACKEND_DIR_CANDIDATES:
        candidate = workspace / d
        if candidate.is_dir():
            return candidate
    return None


def collect_files(root: Path) -> tuple[list[Path], list[Path]]:
    """Walk the backend tree, returning (python_files, polyglot_files)."""
    py: list[Path] = []
    js: list[Path] = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in PY_EXTS + JS_EXTS:
                continue
            full = Path(dirpath) / f
            try:
                if full.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            if ext in PY_EXTS:
                py.append(full)
            else:
                js.append(full)
    return py, js


def load_facts(audit_dir: Path) -> dict:
    for candidate in (
        audit_dir / "artifacts" / "audit_facts.json",
        audit_dir / "audit_facts.json",
        audit_dir / "facts" / "audit_facts.json",
    ):
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 4f — backend / DB / algorithm perf scan.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    workspace = audit_dir / "workspace"
    findings_dir = audit_dir / "findings"

    if not workspace.is_dir():
        print(f"ERROR: workspace not found: {workspace}", file=sys.stderr)
        return 2
    findings_dir.mkdir(parents=True, exist_ok=True)

    backend_root = find_backend_root(workspace)
    if backend_root is None:
        # Stage skipped — write a single tooling finding so the coverage table
        # reflects the truth: "backend source not ingested".
        tooling = [{
            "id": "tooling.backend_source_missing",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": "Backend source not ingested — Stage 4f skipped",
            "description": (
                "No `backend/`, `server/`, or `api/` directory in the workspace. "
                "If the app has a backend on the pod (typical Emergent layout is "
                "`/app/backend/`), re-ingest with the backend tree included to run "
                "the backend / DB / algorithm rules."
            ),
            "evidence": {"file": str(workspace), "function": "<workspace>"},
        }]
        (findings_dir / "backend.json").write_text(json.dumps(tooling, indent=2), encoding="utf-8")
        print("backend scan: no backend source — stage skipped", file=sys.stderr)
        return 0

    py_files, js_files = collect_files(backend_root)
    if not py_files and not js_files:
        tooling = [{
            "id": "tooling.backend_source_empty",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": f"Backend dir `{backend_root.name}/` ingested but contains no source files",
            "description": "No .py / .js / .ts files under the ingested backend tree.",
            "evidence": {"file": str(backend_root), "function": "<workspace>"},
        }]
        (findings_dir / "backend.json").write_text(json.dumps(tooling, indent=2), encoding="utf-8")
        return 0

    facts = load_facts(audit_dir)
    ctx = backend_rules.BackendCtx(
        workspace=workspace,
        backend_root=backend_root,
        python_files=py_files,
        polyglot_files=js_files,
        facts=facts,
    )

    all_findings: list[dict] = []
    rule_errors: list[dict] = []
    for fn in backend_rules.ALL_RULES:
        try:
            results = fn(ctx) or []
        except Exception as e:  # noqa: BLE001
            rule_errors.append({
                "id": "tooling.backend_rule_failed",
                "layer": "tooling",
                "category": "tooling_error",
                "severity": "low",
                "confidence": "high",
                "title": f"Backend rule `{fn.__name__}` raised",
                "description": f"{type(e).__name__}: {e}",
                "evidence": {"file": "configs/backend_rules.py", "function": fn.__name__},
            })
            continue
        for f in results:
            f.setdefault("confidence", "high")
        all_findings.extend(results)

    (findings_dir / "backend.json").write_text(json.dumps(all_findings, indent=2), encoding="utf-8")

    sev_count = lambda s: sum(1 for f in all_findings if f["severity"] == s)
    print(
        f"backend scan: {len(all_findings)} findings "
        f"({sev_count('critical')} critical, {sev_count('high')} high, "
        f"{sev_count('medium')} medium, {sev_count('low')} low) "
        f"across {len(py_files)} .py + {len(js_files)} .js/.ts files",
        file=sys.stderr,
    )
    if rule_errors:
        err_out = findings_dir / "backend_errors.json"
        err_out.write_text(json.dumps(rule_errors, indent=2), encoding="utf-8")
        print(f"  {len(rule_errors)} rule(s) raised — see {err_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
