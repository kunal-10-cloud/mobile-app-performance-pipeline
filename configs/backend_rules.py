"""
Backend / DB / algorithm perf rules — Stage 4f.

Ported verbatim (semantics + detection patterns) from the web pipeline's
`perf-audit/perf_audit.py`. Each rule is `def rule_<name>(ctx) -> list[dict]`
returning Finding dicts in our shape.

Coverage map (legacy `_GENERIC` / `_BACKEND` / `_DATABASE` / `_ALGORITHMS`
check keys → new rule_ids):

  async_handlers           → backend.sync_route_handler
  n_plus_1                 → backend.n_plus_one_query
  unbounded_queries        → backend.unbounded_query
  mongo_singleton          → backend.mongo_client_not_singleton
  missing_indexes          → database.missing_index
  sequential_async         → backend.sequential_await_chain
  blocking_handlers        → backend.blocking_work_in_handler
  over_fetching            → backend.no_projection_on_query
  pydantic_overhead        → backend.pydantic_complex_model
  algorithmic_complexity   → algorithms.nested_iteration
  inefficient_data_structures → algorithms.linear_array_lookup_in_loop
  promise_parallelization  → backend.sequential_fetch_chain  (JS-side, runs on backend handlers using fetch/axios)

Detection is regex + Python `ast` (where the legacy uses ast). Tree-sitter is
not needed for backend Python — the legacy patterns are battle-tested at this
shape.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ── Patterns ported verbatim from perf-audit/perf_audit.py ───────────────────

# Sync calls that BLOCK an `async def` handler (PERF-005). Matched
# case-insensitively across the function body.
BLOCKING_PATTERNS = (
    r"send_email|send_mail|smtp|sendgrid|ses\.send|resend\.",
    r"send_notification|push_notification|fcm\.|firebase_admin\.messaging",
    r"webhook|httpx\.post|requests\.post|aiohttp.*post",
    r"analytics|track_event|segment\.",
    r"stripe\.|paypal\.|razorpay\.",
)
_BLOCKING_RX = re.compile("|".join(BLOCKING_PATTERNS), re.IGNORECASE)

# Mongo query methods (PERF-006 N+1).
_MONGO_CALL_RX = re.compile(r"\.(find_one|find|aggregate|count_documents|distinct)\s*\(")

# Mongo unbounded patterns (PERF-007).
# Bounded if any of these markers appear; unbounded otherwise (when paired with .find).
_BOUNDED_RX = re.compile(
    r"\.limit\("                              # .limit(N) or .limit(var)
    r"|limit\s*="                              # limit= kwarg
    r"|\.to_list\(\s*\d+\s*\)"                 # .to_list(50)
    r"|\.to_list\(\s*length\s*=\s*\d+\s*\)"    # .to_list(length=50)
)
_TO_LIST_UNBOUNDED_RX = re.compile(
    r"\.to_list\(\s*\)"                            # .to_list()
    r"|\.to_list\(\s*None\s*\)"                    # .to_list(None)
    r"|\.to_list\(\s*length\s*=\s*None\s*\)"       # .to_list(length=None)
)

# Mongo client instantiation (PERF-002 / mongo_singleton).
_MONGO_CLIENT_RX = re.compile(r"(MongoClient|AsyncIOMotorClient)\s*\(")

# .find( {filter} ) with no projection (PERF-016 / over_fetching).
_FIND_NO_PROJECTION_RX = re.compile(r"\.find\(\s*\{[^}]*\}\s*\)")

# create_index — for the missing-indexes derivation (PERF-012).
_CREATE_INDEX_RX = re.compile(r"create_index\(\s*[\"'](\w+)")

# Field-in-find-filter pattern (PERF-012 — derive queried fields).
_FIND_FILTER_RX = re.compile(r"\.find\(\s*\{([^}]+)\}")
_FILTER_KEY_RX = re.compile(r"[\"'](\w+)[\"']\s*:")

# Promise parallelization (PERF-010 JS variant, ported by user request).
_AWAIT_FETCH_RX = re.compile(r"await\s+(fetch|axios)")

# Nested iteration (PERF-015 / algorithmic_complexity) — JS array hot patterns.
_NESTED_ITER_PATTERNS = (
    r"\.filter\(.*\.filter\(",
    r"\.find\(.*\.find\(",
    r"\.some\(.*\.some\(",
    r"\.includes\(.*\.includes\(",
    r"for\s.*for\s.*\.includes\(",
)
_NESTED_ITER_RX = re.compile("|".join(_NESTED_ITER_PATTERNS))

# Linear array lookup inside loops (PERF-014 / inefficient_data_structures).
_ARRAY_LOOKUP_RX = re.compile(r"\.includes\(|\.indexOf\(|\.find\(")
_LOOP_START_RX = re.compile(r"^\s*(for\s|for\(|\.forEach\(|\.map\(|\.filter\()")


# ── Context dataclass ────────────────────────────────────────────────────────
@dataclass
class BackendCtx:
    workspace: Path
    backend_root: Path             # workspace/backend or whichever sub-tree exists
    python_files: list[Path] = field(default_factory=list)  # .py files
    polyglot_files: list[Path] = field(default_factory=list)  # .js/.ts under backend (e.g. node-based services)
    facts: dict = field(default_factory=dict)


# ── Finding helper ───────────────────────────────────────────────────────────
def _finding(
    rule_id: str,
    severity: str,
    title: str,
    description: str,
    *,
    category: str = "backend_perf",
    confidence: str = "high",
    file: str = "",
    function: str = "",
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
        "layer": "backend",
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "description": description,
        "evidence": ev,
    }


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse(content: str) -> ast.AST | None:
    try:
        return ast.parse(content)
    except SyntaxError:
        return None


def _has_route_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        dec_str = ast.dump(dec)
        if any(m in dec_str for m in ("'get'", "'post'", "'put'", "'delete'", "'patch'")):
            return True
    return False


def _rel(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ BACKEND PERF RULES                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def rule_sync_route_handler(ctx: BackendCtx) -> list[dict]:
    """PERF-005 — sync (`def`) route handler should be `async def`."""
    out: list[dict] = []
    for fpath in ctx.python_files:
        content = _read(fpath)
        if not content:
            continue
        tree = _parse(content)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and not isinstance(node, ast.AsyncFunctionDef):
                if _has_route_decorator(node):
                    # The legacy rule bumps to severity A for "high-frequency"
                    # endpoints (get/list/search/auth/login/fetch). Mirror that.
                    is_hot = any(kw in node.name.lower() for kw in
                                 ("get", "list", "search", "auth", "login", "fetch"))
                    severity = "high" if is_hot else "medium"
                    out.append(_finding(
                        "backend.sync_route_handler",
                        severity,
                        f"Sync route handler `{node.name}` blocks the event loop",
                        (
                            f"Route handler `{node.name}` is declared with `def` instead of `async def`. "
                            f"FastAPI runs sync handlers in a thread pool; under any real concurrency the "
                            f"pool becomes the bottleneck and request latency climbs. Change to "
                            f"`async def {node.name}(...)` and `await` all DB / HTTP calls inside."
                        ),
                        file=_rel(fpath, ctx.workspace),
                        function=node.name,
                        line=node.lineno,
                    ))
    return out


def rule_n_plus_one_query(ctx: BackendCtx) -> list[dict]:
    """PERF-006 — Mongo query inside a loop."""
    out: list[dict] = []
    for fpath in ctx.python_files:
        content = _read(fpath)
        if not content:
            continue
        lines = content.split("\n")
        in_loop = False
        loop_indent = 0
        loop_line = 0
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if stripped.startswith(("for ", "async for ", "while ")):
                in_loop = True
                loop_indent = indent
                loop_line = i
            elif in_loop and indent <= loop_indent and stripped and not stripped.startswith("#"):
                in_loop = False
            if in_loop and _MONGO_CALL_RX.search(line):
                # locate enclosing fn name (heuristic — same as legacy)
                window = "\n".join(lines[max(0, loop_line - 20):loop_line])
                m = re.search(r"def\s+(\w+)", window)
                fn_name = m.group(1) if m else "<unknown>"
                out.append(_finding(
                    "backend.n_plus_one_query",
                    "high",
                    f"N+1 query inside `{fn_name}`",
                    (
                        "A MongoDB query call is executed inside a loop — each iteration makes a "
                        "separate round-trip. Collect the keys first, then issue ONE batched query "
                        "(`db.coll.find({'_id': {'$in': ids}}).to_list(None)`), and build a lookup "
                        "dict for the rest of the loop."
                    ),
                    file=_rel(fpath, ctx.workspace),
                    function=fn_name,
                    line=i,
                    code_snippet=line.strip()[:200],
                ))
    return out


def rule_unbounded_query(ctx: BackendCtx) -> list[dict]:
    """PERF-007 — `.find()` without `.limit()` / bounded `.to_list(N)`."""
    out: list[dict] = []
    for fpath in ctx.python_files:
        content = _read(fpath)
        if not content:
            continue
        # find every `.find(` then walk forward up to ~3 lines for chained
        # limit/to_list calls. If we find a bound, skip; if to_list-unbounded
        # appears, flag; else flag as raw unbounded find.
        lines = content.split("\n")
        for i, line in enumerate(lines, 1):
            if ".find(" not in line:
                continue
            # join with the next ~3 lines to catch chained calls
            window = " ".join(lines[i - 1 : i + 3])
            if _BOUNDED_RX.search(window):
                continue
            # Either explicitly unbounded to_list(...) or a bare .find(...)
            unbounded_marker = "to_list(...)" if _TO_LIST_UNBOUNDED_RX.search(window) else ".find(...)"
            # locate enclosing fn name
            window_above = "\n".join(lines[max(0, i - 30):i])
            m = re.search(r"def\s+(\w+)", window_above)
            fn_name = m.group(1) if m else "<unknown>"
            out.append(_finding(
                "backend.unbounded_query",
                "high",
                f"Unbounded query in `{fn_name}` (`{unbounded_marker}`)",
                (
                    "This query has no `.limit(N)` and uses an unbounded `.to_list()`. Response size "
                    "grows linearly with the collection — fine at hundreds of rows, fatal at hundreds "
                    "of thousands. Add `skip` / `limit` parameters to the route and bound the cursor: "
                    "`.find(filter).skip(skip).limit(limit).to_list(limit)`."
                ),
                file=_rel(fpath, ctx.workspace),
                function=fn_name,
                line=i,
                code_snippet=line.strip()[:200],
            ))
    return out


def rule_mongo_client_not_singleton(ctx: BackendCtx) -> list[dict]:
    """mongo_singleton — `MongoClient(...)` / `AsyncIOMotorClient(...)`
    instantiated inside a route handler instead of at module-level singleton."""
    out: list[dict] = []
    for fpath in ctx.python_files:
        content = _read(fpath)
        if not content:
            continue
        tree = _parse(content)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _has_route_decorator(node):
                    continue
                func_src = ast.get_source_segment(content, node) or ""
                if _MONGO_CLIENT_RX.search(func_src):
                    out.append(_finding(
                        "backend.mongo_client_not_singleton",
                        "critical",
                        f"MongoDB client created inside route handler `{node.name}`",
                        (
                            f"`MongoClient` / `AsyncIOMotorClient` is instantiated inside route handler "
                            f"`{node.name}`. Every request opens a fresh connection pool (default 100 "
                            f"connections), exhausts MongoDB's connection limit under concurrency, and "
                            f"adds 50-200 ms of handshake latency per call. Move the client to module "
                            f"scope (or a FastAPI startup event) and inject via `Depends()`."
                        ),
                        category="database",
                        file=_rel(fpath, ctx.workspace),
                        function=node.name,
                        line=node.lineno,
                    ))
    return out


def rule_missing_index(ctx: BackendCtx) -> list[dict]:
    """PERF-012 — fields queried via `.find({field: …})` with no matching
    `create_index('field')` anywhere in the same file."""
    out: list[dict] = []
    for fpath in ctx.python_files:
        content = _read(fpath)
        if not content:
            continue
        queried: set[str] = set()
        for m in _FIND_FILTER_RX.finditer(content):
            for k in _FILTER_KEY_RX.findall(m.group(1)):
                if k not in ("_id", "$in", "$or", "$and", "$gt", "$lt", "$gte", "$lte", "$ne", "$exists"):
                    queried.add(k)
        indexed: set[str] = set(m.group(1) for m in _CREATE_INDEX_RX.finditer(content))
        missing = queried - indexed - {"_id"}
        if missing:
            sample = sorted(missing)[:5]
            out.append(_finding(
                "database.missing_index",
                "high",
                f"Missing indexes for {len(missing)} queried fields",
                (
                    f"Fields `{', '.join(sample)}`{' …' if len(missing) > 5 else ''} are used in "
                    f"`.find({{...}})` filters in this file but have no matching `create_index(...)` "
                    f"call. Queries on unindexed fields trigger a full collection scan — fast at small "
                    f"sizes, fatal past ~10K documents. Add startup-time index creation "
                    f"(`@app.on_event('startup')`) for each frequently-queried field."
                ),
                category="database",
                file=_rel(fpath, ctx.workspace),
                function="<module>",
                metric_name="missing_index_field_count",
                metric_value=len(missing),
            ))
    return out


def rule_sequential_await_chain(ctx: BackendCtx) -> list[dict]:
    """PERF-010 — ≥3 consecutive `await` statements likely parallelizable."""
    out: list[dict] = []
    for fpath in ctx.python_files:
        content = _read(fpath)
        if not content:
            continue
        lines = content.split("\n")
        consecutive: list[int] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            is_await = stripped.startswith("await ") or "= await " in stripped
            if is_await:
                consecutive.append(i)
                continue
            if len(consecutive) >= 3:
                # Heuristic: distinct vars assigned → independent (parallelizable).
                await_lines = [lines[j - 1].strip() for j in consecutive]
                assigned = []
                for al in await_lines:
                    if "=" in al:
                        assigned.append(al.split("=", 1)[0].strip())
                if assigned and len(set(assigned)) == len(assigned) and len(assigned) >= 2:
                    out.append(_finding(
                        "backend.sequential_await_chain",
                        "medium",
                        f"{len(consecutive)} sequential `await`s — likely `asyncio.gather` candidate",
                        (
                            f"{len(consecutive)} consecutive `await` statements at the same scope; "
                            f"each assigns a distinct variable, so they read as independent operations. "
                            f"Sequential total latency = sum of all awaits; `asyncio.gather(...)` makes "
                            f"it max of all awaits. Confirm no inter-await dependency before parallelising."
                        ),
                        file=_rel(fpath, ctx.workspace),
                        function="<scope>",
                        line=consecutive[0],
                    ))
            consecutive = []
    return out


def rule_blocking_work_in_handler(ctx: BackendCtx) -> list[dict]:
    """blocking_handlers / PERF-009 — email / webhook / payment / analytics
    calls inside a route handler not wrapped in `BackgroundTasks`."""
    out: list[dict] = []
    for fpath in ctx.python_files:
        content = _read(fpath)
        if not content:
            continue
        tree = _parse(content)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _has_route_decorator(node):
                    continue
                func_src = ast.get_source_segment(content, node) or ""
                if not _BLOCKING_RX.search(func_src):
                    continue
                if "BackgroundTasks" in func_src or "background_task" in func_src.lower():
                    continue
                out.append(_finding(
                    "backend.blocking_work_in_handler",
                    "high",
                    f"Blocking external work inside route handler `{node.name}`",
                    (
                        f"Route handler `{node.name}` runs an external service call "
                        "(email / push / webhook / payment / analytics) inline before returning. "
                        "Request latency = external service latency; if the provider takes 30s, "
                        "the user waits 30s. Move to FastAPI `BackgroundTasks` or a job queue."
                    ),
                    file=_rel(fpath, ctx.workspace),
                    function=node.name,
                    line=node.lineno,
                ))
    return out


def rule_no_projection_on_query(ctx: BackendCtx) -> list[dict]:
    """PERF-016 — `.find({filter})` with no projection (second arg). Cap at 10."""
    out: list[dict] = []
    cap = 10
    for fpath in ctx.python_files:
        if len(out) >= cap:
            break
        content = _read(fpath)
        if not content:
            continue
        for m in _FIND_NO_PROJECTION_RX.finditer(content):
            line_num = content[: m.start()].count("\n") + 1
            out.append(_finding(
                "backend.no_projection_on_query",
                "low",
                "Query without a projection (returns every field)",
                (
                    "`.find(filter)` is called without a projection argument, so MongoDB returns every "
                    "field of every matching document. Marginal at small document sizes; meaningful "
                    "when documents are large. Add a projection: `.find(filter, {'a': 1, 'b': 1})`."
                ),
                file=_rel(fpath, ctx.workspace),
                function="<scope>",
                line=line_num,
                code_snippet=m.group(0)[:200],
            ))
            if len(out) >= cap:
                break
    return out


def rule_pydantic_complex_model(ctx: BackendCtx) -> list[dict]:
    """PERF-013 — Pydantic models with ≥3 nested/list fields (informational)."""
    out: list[dict] = []
    for fpath in ctx.python_files:
        content = _read(fpath)
        if not content:
            continue
        tree = _parse(content)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            is_pydantic = any(
                isinstance(b, ast.Name) and b.id == "BaseModel" for b in node.bases
            )
            if not is_pydantic:
                continue
            nested = 0
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and item.annotation:
                    ann_str = ast.dump(item.annotation)
                    if "List" in ann_str or "Optional" in ann_str:
                        nested += 1
            if nested >= 3:
                out.append(_finding(
                    "backend.pydantic_complex_model",
                    "low",
                    f"Complex Pydantic model `{node.name}` ({nested} nested/optional fields)",
                    (
                        f"`{node.name}` has {nested} nested or Optional fields. Pydantic v2 validates "
                        f"in Rust so the overhead is usually negligible. On Pydantic v1 (or for very "
                        f"high-frequency endpoints) consider simpler `TypedDict` / dataclasses for "
                        f"internal-only models. Profile before refactoring."
                    ),
                    confidence="medium",
                    file=_rel(fpath, ctx.workspace),
                    function=node.name,
                    line=node.lineno,
                    metric_name="nested_field_count",
                    metric_value=nested,
                    metric_threshold=3,
                ))
    return out


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ ALGORITHMS rules                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def rule_nested_iteration(ctx: BackendCtx) -> list[dict]:
    """PERF-015 — nested `.filter().filter()` / `.find().find()` / `for…for…includes(`."""
    out: list[dict] = []
    # Run across BOTH python and polyglot files (JS-flavored backends exist).
    for fpath in list(ctx.python_files) + list(ctx.polyglot_files):
        content = _read(fpath)
        if not content:
            continue
        for i, line in enumerate(content.split("\n"), 1):
            if _NESTED_ITER_RX.search(line):
                out.append(_finding(
                    "algorithms.nested_iteration",
                    "medium",
                    "Nested iteration pattern (O(n²) shape)",
                    (
                        "Nested `.filter`/`.find`/`.some`/`.includes` (or a `for` loop with an "
                        "`includes` lookup inside another `for`). Complexity scales quadratically "
                        "with data size. Build a `Map` / `Set` once before the outer loop and look "
                        "up in O(1) inside it."
                    ),
                    category="algorithms",
                    file=_rel(fpath, ctx.workspace),
                    function="<scope>",
                    line=i,
                    code_snippet=line.strip()[:200],
                ))
                # one finding per line is plenty
                continue
    return out


def rule_linear_array_lookup_in_loop(ctx: BackendCtx) -> list[dict]:
    """PERF-014 — `.includes()` / `.indexOf()` / `.find()` on an array inside
    a `for` / `.forEach` / `.map` / `.filter` loop. Cap at 10 to keep noise low."""
    out: list[dict] = []
    cap = 10
    for fpath in list(ctx.python_files) + list(ctx.polyglot_files):
        if len(out) >= cap:
            break
        content = _read(fpath)
        if not content:
            continue
        in_loop = False
        for i, raw in enumerate(content.split("\n"), 1):
            stripped = raw.strip()
            if _LOOP_START_RX.search(raw):
                in_loop = True
            elif in_loop and not stripped:
                in_loop = False
            if in_loop and _ARRAY_LOOKUP_RX.search(raw):
                out.append(_finding(
                    "algorithms.linear_array_lookup_in_loop",
                    "low",
                    "Array lookup inside a loop (O(n×m) shape)",
                    (
                        "`.includes()` / `.indexOf()` / `.find()` runs on an array inside an outer loop, "
                        "so the total cost is O(n×m). Convert the lookup array to a `Set` (or `dict` in "
                        "Python) before the loop: `const ids = new Set(arr); if (ids.has(x))`. Constant "
                        "factor, identical semantics."
                    ),
                    category="algorithms",
                    file=_rel(fpath, ctx.workspace),
                    function="<scope>",
                    line=i,
                    code_snippet=raw.strip()[:200],
                ))
                if len(out) >= cap:
                    break
    return out


def rule_sequential_fetch_chain(ctx: BackendCtx) -> list[dict]:
    """PERF-010 JS variant (promise_parallelization) — ≥2 consecutive
    `await fetch(...)` / `await axios.X(...)` calls likely parallelizable."""
    out: list[dict] = []
    for fpath in ctx.polyglot_files:
        content = _read(fpath)
        if not content:
            continue
        lines = content.split("\n")
        consecutive: list[int] = []
        for i, line in enumerate(lines, 1):
            if _AWAIT_FETCH_RX.search(line):
                consecutive.append(i)
                continue
            if len(consecutive) >= 2:
                out.append(_finding(
                    "backend.sequential_fetch_chain",
                    "medium",
                    f"{len(consecutive)} sequential `await fetch/axios` calls",
                    (
                        f"{len(consecutive)} `await fetch(...)` / `await axios.X(...)` calls run in "
                        f"sequence. Total latency is the sum of all of them; if the calls are "
                        f"independent, `Promise.all([fetch(...), fetch(...)])` makes it the max."
                    ),
                    file=_rel(fpath, ctx.workspace),
                    function="<scope>",
                    line=consecutive[0],
                ))
            consecutive = []
    return out


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Rule registry                                                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

BACKEND_RULES: tuple[Callable[[BackendCtx], list[dict]], ...] = (
    rule_sync_route_handler,
    rule_n_plus_one_query,
    rule_unbounded_query,
    rule_mongo_client_not_singleton,
    rule_missing_index,
    rule_sequential_await_chain,
    rule_blocking_work_in_handler,
    rule_no_projection_on_query,
    rule_pydantic_complex_model,
)

ALGORITHM_RULES: tuple[Callable[[BackendCtx], list[dict]], ...] = (
    rule_nested_iteration,
    rule_linear_array_lookup_in_loop,
    rule_sequential_fetch_chain,
)

ALL_RULES: tuple[Callable[[BackendCtx], list[dict]], ...] = BACKEND_RULES + ALGORITHM_RULES
