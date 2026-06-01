"""
Custom AST rules for the static_scan stage.

Each rule is a function `(tree, file_path, source_bytes) -> list[Finding]`,
where `Finding` is a dict matching schemas/finding.schema.json (minus verdict
+ verification_method, which Pass A fills in later).

Rules use tree-sitter-typescript queries. Receiver-type and enclosing-scope
checks are done structurally, NOT via regex on source. This is the key
difference from grep-based scanning — false-positive rate is materially lower.

To add a new rule:
  1. Implement a function below with the (tree, file_path, source_bytes) signature.
  2. Register it in RULES at the bottom.
  3. Add a §3 entry to references.md describing the rule, its preconditions,
     common FP shapes, and report framing.
  4. Add a positive + negative fixture under test-fixture/ that the rule
     fires on (positive) and doesn't fire on (negative).
"""
from __future__ import annotations

import os
from typing import Callable

import tree_sitter_typescript as ts_ts
from tree_sitter import Language, Node, Parser, Query

# ──────────────────────────────────────────────────────────────────────────────
# Language setup
# ──────────────────────────────────────────────────────────────────────────────
# Two TypeScript dialects — TSX (used for .tsx files) and TypeScript (.ts).
# We also use TSX to parse .jsx and .js files since the TSX grammar is a
# superset that handles JSX + JS correctly; TS grammar would reject JSX.

TSX_LANGUAGE = Language(ts_ts.language_tsx())
TS_LANGUAGE = Language(ts_ts.language_typescript())


def parser_for(file_path: str) -> Parser:
    ext = os.path.splitext(file_path)[1].lower()
    # Use TSX grammar for anything that might contain JSX.
    if ext in (".tsx", ".jsx", ".js"):
        return Parser(TSX_LANGUAGE)
    # Plain .ts files: TS grammar (no JSX expected).
    return Parser(TS_LANGUAGE)


def language_for(file_path: str) -> Language:
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".tsx", ".jsx", ".js"):
        return TSX_LANGUAGE
    return TS_LANGUAGE


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def enclosing_function_name(node: Node, source: bytes) -> str:
    """Walk upward to find the nearest function declaration or arrow-bound const.
    Returns '<module>' if at top level."""
    cur = node.parent
    while cur is not None:
        if cur.type == "function_declaration":
            name = cur.child_by_field_name("name")
            if name is not None:
                return node_text(name, source)
            return "<anonymous>"
        if cur.type == "method_definition":
            name = cur.child_by_field_name("name")
            if name is not None:
                return node_text(name, source)
        if cur.type in ("class_declaration",):
            name = cur.child_by_field_name("name")
            if name is not None:
                return f"<class:{node_text(name, source)}>"
        if cur.type == "variable_declarator":
            # `const X = (...) => { ... }`
            value = cur.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression"):
                name = cur.child_by_field_name("name")
                if name is not None:
                    return node_text(name, source)
        cur = cur.parent
    return "<module>"


def line_of(node: Node) -> int:
    # tree-sitter rows are 0-indexed; report 1-indexed line numbers.
    return node.start_point[0] + 1


def snippet_around(source: bytes, node: Node, context_lines: int = 1) -> str:
    """Return a short code snippet centred on the node — for evidence.code_snippet."""
    src = source.decode("utf-8", errors="replace")
    lines = src.splitlines()
    start = max(0, node.start_point[0] - context_lines)
    end = min(len(lines), node.end_point[0] + context_lines + 1)
    return "\n".join(lines[start:end])[:400]


def import_specifiers(tree, source: bytes) -> dict[str, str]:
    """Map identifier -> module from which it was imported.
    Example: `import {Animated, Text} from 'react-native'` →
        {'Animated': 'react-native', 'Text': 'react-native'}
    `import Image from 'react-native'` →
        {'Image': 'react-native'}  (default)
    """
    out: dict[str, str] = {}
    cursor = tree.walk()
    visited = False
    while True:
        node = cursor.node
        if node.type == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                module = node_text(source_node, source).strip("\"'")
                # Iterate children of the import_statement looking for clauses
                for child in node.children:
                    if child.type == "import_clause":
                        for sub in child.children:
                            if sub.type == "identifier":
                                out[node_text(sub, source)] = module
                            elif sub.type == "named_imports":
                                for spec in sub.children:
                                    if spec.type == "import_specifier":
                                        name_node = spec.child_by_field_name("name")
                                        alias_node = spec.child_by_field_name("alias")
                                        ident = alias_node if alias_node else name_node
                                        if ident is not None:
                                            out[node_text(ident, source)] = module
                            elif sub.type == "namespace_import":
                                for spec in sub.children:
                                    if spec.type == "identifier":
                                        out[node_text(spec, source)] = module
        # depth-first traversal
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                visited = True
                break
        if visited:
            break
    return out


def make_finding(
    rule_id: str,
    *,
    category: str,
    severity: str,
    confidence: str,
    title: str,
    description: str,
    file_path: str,
    function: str,
    line: int,
    code_snippet: str,
) -> dict:
    return {
        "id": rule_id,
        "layer": "static",
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "description": description,
        "evidence": {
            "file": file_path,
            "function": function,
            "line": line,
            "code_snippet": code_snippet,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Rule 1: static.scrollview_with_long_list
# ──────────────────────────────────────────────────────────────────────────────
# Match: <ScrollView> JSX element whose children include `{collection.map(...)}`
# where the collection is not statically bounded (i.e. not `.slice(0, N).map`).
#
# Query approach: find every JSX element whose opening tag is 'ScrollView',
# then walk its descendants looking for a call_expression whose function ends
# in `.map`. Confirm the receiver is NOT a `.slice(0, N)` member chain.

_SCROLLVIEW_QUERY = """
(jsx_element
  open_tag: (jsx_opening_element
    name: (identifier) @tag_name (#eq? @tag_name "ScrollView")
  )
) @scrollview
"""


def _has_slice_bound_ancestor(call_node: Node, source: bytes) -> bool:
    """Return True if `xs.map(...)` is actually `xs.slice(0, N).map(...)`."""
    # call_expression > member_expression > [object] > member_expression(.slice) > call_expression
    callee = call_node.child_by_field_name("function")
    if callee is None or callee.type != "member_expression":
        return False
    obj = callee.child_by_field_name("object")
    if obj is None:
        return False
    # obj should be a call_expression whose callee.property == "slice"
    if obj.type != "call_expression":
        return False
    obj_callee = obj.child_by_field_name("function")
    if obj_callee is None or obj_callee.type != "member_expression":
        return False
    prop = obj_callee.child_by_field_name("property")
    if prop is None:
        return False
    return node_text(prop, source) == "slice"


def _map_callback_returns_jsx(map_call: Node) -> bool:
    """True if the first argument to `.map(...)` is a function whose body
    contains JSX. Filters out value-producing maps (e.g. `xs.map(x => x.id)`)
    which don't render children and must not trip the ScrollView rule."""
    args = map_call.child_by_field_name("arguments")
    if args is None:
        return False
    for c in args.children:
        if c.type in ("arrow_function", "function_expression", "function"):
            body = c.child_by_field_name("body")
            if body is not None and _node_contains_jsx(body):
                return True
    return False


def _map_callback_returns_jsx(map_call: Node) -> bool:
    """True if the first argument to `.map(...)` is a function whose body
    contains JSX. Filters out value-producing maps (e.g. `xs.map(x => x.id)`
    or `data.map(d => d.value)` inside Math.max) which don't render children
    and must not trip the ScrollView rule."""
    args = map_call.child_by_field_name("arguments")
    if args is None:
        return False
    for c in args.children:
        if c.type in ("arrow_function", "function_expression", "function"):
            body = c.child_by_field_name("body")
            if body is not None and _node_contains_jsx(body):
                return True
    return False


def rule_scrollview_with_long_list(tree, file_path: str, source: bytes) -> list[dict]:
    findings: list[dict] = []
    lang = language_for(file_path)
    try:
        q = Query(lang, _SCROLLVIEW_QUERY)
    except Exception:
        return findings

    matches = q.matches(tree.root_node)
    for _pat, captures in matches:
        sv_nodes = captures.get("scrollview", [])
        for sv in sv_nodes:
            # Walk descendants looking for .map calls inside JSX expressions
            stack = list(sv.children)
            found_map = False
            map_call = None
            while stack:
                n = stack.pop()
                if n.type == "call_expression":
                    fn = n.child_by_field_name("function")
                    if fn is not None and fn.type == "member_expression":
                        prop = fn.child_by_field_name("property")
                        if prop is not None and node_text(prop, source) == "map":
                            # Real finding only when (a) the collection isn't
                            # statically bounded by .slice(0,N), AND (b) the map
                            # callback returns JSX (renders children). A map that
                            # produces values is not a rendering map.
                            if not _has_slice_bound_ancestor(n, source) and _map_callback_returns_jsx(n):
                                found_map = True
                                map_call = n
                                break
                stack.extend(n.children)
            if found_map and map_call is not None:
                # Anchor the finding at the <ScrollView> opening tag — that's the
                # actionable location (where you'd swap to FlatList/FlashList).
                # The map call is still shown in the snippet so the reader sees
                # what's being rendered.
                findings.append(make_finding(
                    "static.scrollview_with_long_list",
                    category="runtime_jank",
                    severity="high",
                    confidence="medium",
                    title="ScrollView renders unbounded list via .map()",
                    description="A <ScrollView> renders its children with an unbounded `.map()` call. Every child mounts eagerly; for collections of >20 items this causes visible jank and memory pressure.",
                    file_path=file_path,
                    function=enclosing_function_name(sv, source),
                    line=line_of(sv),
                    code_snippet=snippet_around(source, map_call, context_lines=2),
                ))
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Rule 2: static.image_without_caching
# ──────────────────────────────────────────────────────────────────────────────
# Match: <Image source={{ uri: ... }}> where Image is imported from 'react-native'.
#
# Approach: scan imports first to learn which `Image` identifier maps to which
# module. Then walk JSX elements named `Image`, inspect the `source` prop, flag
# remote URIs when the import came from 'react-native'.

_IMAGE_JSX_QUERY = """
(jsx_self_closing_element
  name: (identifier) @tag (#eq? @tag "Image")
) @image_self_closing

(jsx_element
  open_tag: (jsx_opening_element
    name: (identifier) @tag2 (#eq? @tag2 "Image")
  )
) @image_pair
"""


def _has_remote_uri_source(image_node: Node, source: bytes) -> bool:
    """Return True if the Image element has source={{ uri: ... }} (object with uri)."""
    # Search attributes for `source={...}`
    open_tag = image_node
    if image_node.type == "jsx_element":
        open_tag = image_node.children[0]  # jsx_opening_element
    # Iterate attributes
    for child in open_tag.children:
        if child.type != "jsx_attribute":
            continue
        # attribute name
        name_node = None
        for c in child.children:
            if c.type == "property_identifier":
                name_node = c
                break
        if name_node is None or node_text(name_node, source) != "source":
            continue
        # Find the value: jsx_expression containing an object_expression with uri key
        for c in child.children:
            if c.type == "jsx_expression":
                for cc in c.children:
                    if cc.type == "object":
                        # walk object pairs for a 'uri' key
                        for prop in cc.children:
                            if prop.type == "pair":
                                key = prop.child_by_field_name("key")
                                if key is not None and node_text(key, source).strip("'\"") == "uri":
                                    return True
                    if cc.type == "identifier":
                        # source={someVar} — we can't tell statically, assume remote
                        return True
    return False


def rule_image_without_caching(tree, file_path: str, source: bytes) -> list[dict]:
    findings: list[dict] = []
    lang = language_for(file_path)
    imports = import_specifiers(tree, source)
    image_module = imports.get("Image")
    if image_module != "react-native":
        # If Image isn't imported from react-native (or not imported at all),
        # the rule doesn't apply in this file.
        return findings
    try:
        q = Query(lang, _IMAGE_JSX_QUERY)
    except Exception:
        return findings

    matches = q.matches(tree.root_node)
    seen_lines: set[int] = set()
    for _pat, captures in matches:
        for key in ("image_self_closing", "image_pair"):
            for n in captures.get(key, []):
                if not _has_remote_uri_source(n, source):
                    continue
                ln = line_of(n)
                if ln in seen_lines:
                    continue
                seen_lines.add(ln)
                findings.append(make_finding(
                    "static.image_without_caching",
                    category="runtime_jank",
                    severity="high",
                    confidence="high",
                    title="<Image> from react-native used for remote URI (no caching)",
                    description="React Native's <Image> does not cache remote images to disk; every navigation back to a screen re-downloads. expo-image provides drop-in caching.",
                    file_path=file_path,
                    function=enclosing_function_name(n, source),
                    line=ln,
                    code_snippet=snippet_around(source, n, context_lines=2),
                ))
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Rule 3: static.inline_arrow_in_renderitem
# ──────────────────────────────────────────────────────────────────────────────
# Match: <FlatList renderItem={() => ...}> (or SectionList / FlashList).
# Inline arrow in renderItem creates a fresh function reference every parent
# render, defeating the list's internal memoization.

_LIST_TAGS = {"FlatList", "SectionList", "FlashList"}


def rule_inline_arrow_in_renderitem(tree, file_path: str, source: bytes) -> list[dict]:
    findings: list[dict] = []

    # Iterate every JSX element whose tag is in _LIST_TAGS
    def visit(node: Node):
        if node.type in ("jsx_self_closing_element", "jsx_opening_element"):
            name = node.child_by_field_name("name")
            tag = node_text(name, source) if name is not None else ""
            if tag in _LIST_TAGS:
                # Find renderItem attribute
                for child in node.children:
                    if child.type != "jsx_attribute":
                        continue
                    attr_name = None
                    for c in child.children:
                        if c.type == "property_identifier":
                            attr_name = c
                            break
                    if attr_name is None or node_text(attr_name, source) != "renderItem":
                        continue
                    # Look at the value
                    for c in child.children:
                        if c.type == "jsx_expression":
                            for cc in c.children:
                                if cc.type == "arrow_function":
                                    findings.append(make_finding(
                                        "static.inline_arrow_in_renderitem",
                                        category="runtime_jank",
                                        severity="high",
                                        confidence="high",
                                        title=f"Inline arrow as renderItem on <{tag}>",
                                        description=f"renderItem={{() => ...}} on <{tag}> creates a new function reference on every parent render, forcing every row to re-render.",
                                        file_path=file_path,
                                        function=enclosing_function_name(cc, source),
                                        line=line_of(cc),
                                        code_snippet=snippet_around(source, cc, context_lines=2),
                                    ))
        for ch in node.children:
            visit(ch)

    visit(tree.root_node)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Rule 4: static.useeffect_no_deps
# ──────────────────────────────────────────────────────────────────────────────
# Match: useEffect(callback) with no second argument (no dependency array) —
# effect runs on every render.

def rule_useeffect_no_deps(tree, file_path: str, source: bytes) -> list[dict]:
    findings: list[dict] = []

    def visit(node: Node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                # Match useEffect(...) or React.useEffect(...)
                callee_text = node_text(fn, source)
                is_use_effect = callee_text == "useEffect" or callee_text.endswith(".useEffect")
                if is_use_effect:
                    args = node.child_by_field_name("arguments")
                    if args is not None:
                        # Count non-trivia children (skip "(", ",", ")")
                        arg_nodes = [c for c in args.children if c.type not in ("(", ")", ",")]
                        if len(arg_nodes) < 2:
                            findings.append(make_finding(
                                "static.useeffect_no_deps",
                                category="runtime_jank",
                                severity="medium",
                                confidence="high",
                                title="useEffect without dependency array",
                                description="useEffect called without a dependency array; the callback runs on every render and any setState inside it can cause render loops.",
                                file_path=file_path,
                                function=enclosing_function_name(node, source),
                                line=line_of(node),
                                code_snippet=snippet_around(source, node, context_lines=1),
                            ))
        for ch in node.children:
            visit(ch)

    visit(tree.root_node)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Rule 4b: static.useeffect_missing_cleanup
# ──────────────────────────────────────────────────────────────────────────────
# Match: `useEffect(() => { …side-effect-starting call… }, [...])` whose body
# starts a long-lived resource (timer, listener, subscription, audio session,
# Firebase listener, WebSocket) but does NOT return a cleanup function.
#
# This is the static pre-flight for the runtime symptom caught by the device
# layer (`device.memory_growth_suspected_leak`). The runtime catches the leak;
# this rule names the *site* that's leaking.

# Names of functions/methods whose return value MUST be torn down. Each entry is
# matched against the callee text (`setInterval`, `someObj.addEventListener`,
# `subscription.subscribe`). The check is "ends-with" tolerant so any receiver
# matches (e.g. `Audio.Sound.createAsync` matches the `.createAsync` tail).
_NEEDS_CLEANUP_CALLEES = (
    "setInterval",
    "setTimeout",       # less critical than setInterval but still leaks
    "addEventListener",
    ".subscribe",       # RxJS, AppState, NetInfo, store subscriptions
    ".addListener",     # EventEmitter, Navigation listeners
    "onSnapshot",       # Firestore
    "onValue",          # Firebase RTDB
    "onAuthStateChanged",
    "onMessage",        # FCM foreground listener
    "createAsync",      # expo-av Audio.Sound.createAsync / Audio.Recording.createAsync
)

# Constructor names that create long-lived objects needing close()/disconnect().
_NEEDS_CLEANUP_CONSTRUCTORS = (
    "WebSocket",
    "EventSource",
)


def _is_cleanup_callee(text: str) -> bool:
    """True if the callee text matches any of the side-effect-starting names."""
    for marker in _NEEDS_CLEANUP_CALLEES:
        if marker.startswith("."):
            if text.endswith(marker):
                return True
        elif text == marker or text.endswith("." + marker):
            return True
    return False


def _body_has_cleanup_signal(body: Node, source: bytes) -> tuple[bool, str]:
    """Walk the useEffect callback body. Return (any_signal, first_marker)
    indicating whether a side-effect-starting call/constructor is present."""
    found: list[str] = []

    def visit(n: Node):
        if found:
            return
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None:
                callee = node_text(fn, source)
                if _is_cleanup_callee(callee):
                    found.append(callee)
                    return
        elif n.type == "new_expression":
            ctor = n.child_by_field_name("constructor")
            if ctor is not None:
                ctor_text = node_text(ctor, source)
                # match exact or .Name tail
                for c in _NEEDS_CLEANUP_CONSTRUCTORS:
                    if ctor_text == c or ctor_text.endswith("." + c):
                        found.append(f"new {ctor_text}")
                        return
        for ch in n.children:
            visit(ch)

    visit(body)
    return (bool(found), found[0] if found else "")


def _body_has_cleanup_return(body: Node, source: bytes) -> bool:
    """True if the callback body has a top-level `return <fn>` whose value is
    callable (arrow / function expression / identifier reference). We only
    look at top-level returns of the body so a `return` inside an `if/then`
    that early-exits before subscribing isn't mistaken for cleanup."""
    # body is the statement_block (or single expression) of the arrow.
    # Walk only direct + control-flow descendants of the block, not nested fns.
    def visit(n: Node, inside_inner_fn: bool):
        if n.type in ("function_expression", "arrow_function", "function_declaration"):
            # Don't descend into nested functions — their `return`s don't count.
            inside_inner_fn = True
        if not inside_inner_fn and n.type == "return_statement":
            # The return's child (skipping the `return` keyword) is the expr.
            for ch in n.children:
                if ch.type in ("arrow_function", "function_expression"):
                    return True
                if ch.type == "identifier":
                    # `return cleanup;` — cleanup defined above; accept.
                    return True
                if ch.type == "call_expression":
                    # `return curry(...)` — accept as cleanup factory.
                    return True
        for ch in n.children:
            if visit(ch, inside_inner_fn):
                return True
        return False

    return visit(body, inside_inner_fn=False)


# Markers that compound severely without cleanup (recurring timers, live
# listeners, open sockets, retained audio sessions). HIGH-severity by themselves.
_HIGH_SEVERITY_MARKERS = {
    "setInterval",
    "addEventListener",
    ".addListener",
    ".subscribe",
    "onSnapshot",
    "onValue",
    "onAuthStateChanged",
    "onMessage",
    "createAsync",
    "new WebSocket",
    "new EventSource",
}


def _severity_for_marker(marker: str) -> str:
    """setTimeout is bounded (one-shot, even if recursive) — MEDIUM.
    The compounding patterns (setInterval, listeners, sockets, audio) — HIGH."""
    if marker in _HIGH_SEVERITY_MARKERS:
        return "high"
    # Tolerant suffix match for `someObj.subscribe` etc.
    for m in _HIGH_SEVERITY_MARKERS:
        if m.startswith(".") and marker.endswith(m):
            return "high"
        if m.startswith("new ") and marker.startswith("new ") and marker.endswith(m[4:]):
            return "high"
    return "medium"


def rule_useeffect_missing_cleanup(tree, file_path: str, source: bytes) -> list[dict]:
    """Find useEffect callbacks that start a long-lived resource but don't
    return a cleanup. Severity follows the marker: setInterval / listener /
    socket / audio → HIGH; setTimeout → MEDIUM (bounded). Confidence MEDIUM
    (cleanup may live inside a custom hook the AST can't follow)."""
    findings: list[dict] = []

    def visit(node: Node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                callee_text = node_text(fn, source)
                if callee_text == "useEffect" or callee_text.endswith(".useEffect"):
                    args = node.child_by_field_name("arguments")
                    if args is not None:
                        arg_nodes = [c for c in args.children if c.type not in ("(", ")", ",")]
                        if arg_nodes:
                            callback = arg_nodes[0]
                            body = None
                            if callback.type == "arrow_function":
                                body = callback.child_by_field_name("body")
                            elif callback.type == "function_expression":
                                body = callback.child_by_field_name("body")
                            if body is not None:
                                has_signal, marker = _body_has_cleanup_signal(body, source)
                                if has_signal and not _body_has_cleanup_return(body, source):
                                    severity = _severity_for_marker(marker)
                                    findings.append(make_finding(
                                        "static.useeffect_missing_cleanup",
                                        category="memory",
                                        severity=severity,
                                        confidence="medium",
                                        title=f"useEffect starts `{marker}` but does not return a cleanup",
                                        description=(
                                            f"This `useEffect` calls `{marker}` in its body but the "
                                            f"callback does not return a cleanup function. The resource is "
                                            f"recreated on every effect re-run and the previous one is never "
                                            f"torn down — a leak that compounds across navigations and "
                                            f"dependency changes. Return `() => {{ /* clear / remove / unsubscribe / unloadAsync */ }}` "
                                            f"so the resource is released when the effect re-runs or the component unmounts."
                                        ),
                                        file_path=file_path,
                                        function=enclosing_function_name(node, source),
                                        line=line_of(node),
                                        code_snippet=snippet_around(source, node, context_lines=2),
                                    ))
        for ch in node.children:
            visit(ch)

    visit(tree.root_node)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Rule 5: static.console_log_in_production_code
# ──────────────────────────────────────────────────────────────────────────────
# Match: console.log / .warn / .error / .info NOT inside `if (__DEV__) {...}`.
# Each call crosses the JSI bridge and serialises arguments in production.

_PRODUCTION_PATH_HINTS = ("app/", "src/", "components/", "screens/", "lib/", "hooks/", "utils/")
_CONSOLE_METHODS_TO_FLAG = {"log", "warn", "info", "debug"}  # 'error' often legit, exclude


def _is_dev_guarded(node: Node, source: bytes) -> bool:
    """Walk upward looking for `if (__DEV__) {...}` enclosing block."""
    cur = node.parent
    while cur is not None:
        if cur.type == "if_statement":
            cond = cur.child_by_field_name("condition")
            if cond is not None and "__DEV__" in node_text(cond, source):
                return True
        cur = cur.parent
    return False


def rule_console_log_in_production_code(tree, file_path: str, source: bytes) -> list[dict]:
    # Only flag files under conventional production-source paths
    norm = file_path.replace("\\", "/")
    if not any(hint in norm for hint in _PRODUCTION_PATH_HINTS):
        return []
    if "/__tests__/" in norm or norm.endswith(".test.ts") or norm.endswith(".test.tsx") or norm.endswith(".spec.ts") or norm.endswith(".spec.tsx"):
        return []

    findings: list[dict] = []

    def visit(node: Node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                obj = fn.child_by_field_name("object")
                prop = fn.child_by_field_name("property")
                if obj is not None and prop is not None:
                    if node_text(obj, source) == "console" and node_text(prop, source) in _CONSOLE_METHODS_TO_FLAG:
                        if not _is_dev_guarded(node, source):
                            findings.append(make_finding(
                                "static.console_log_in_production_code",
                                category="code_quality",
                                severity="low",
                                confidence="medium",
                                title=f"console.{node_text(prop, source)} outside __DEV__ guard",
                                description="Console calls cross the JSI bridge and serialise their arguments even in release builds. Wrap in `if (__DEV__)` or strip via babel-plugin-transform-remove-console.",
                                file_path=file_path,
                                function=enclosing_function_name(node, source),
                                line=line_of(node),
                                code_snippet=snippet_around(source, node, context_lines=1),
                            ))
        for ch in node.children:
            visit(ch)

    visit(tree.root_node)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Rule 6: static.animated_api_usage
# ──────────────────────────────────────────────────────────────────────────────
# Match: `import { Animated, ... } from 'react-native'` — Animated runs on the JS thread.
# Reanimated runs on the UI thread and is the preferred alternative.

def rule_animated_api_usage(tree, file_path: str, source: bytes) -> list[dict]:
    imports = import_specifiers(tree, source)
    if imports.get("Animated") != "react-native":
        return []
    # Find the actual import-statement line for the snippet
    cursor = tree.walk()
    line = 1
    snippet = "import { Animated } from 'react-native'"
    found = False
    def visit(n):
        nonlocal line, snippet, found
        if found: return
        if n.type == "import_statement":
            src_node = n.child_by_field_name("source")
            if src_node is not None and "react-native" in node_text(src_node, source):
                text = node_text(n, source)
                if "Animated" in text:
                    line = line_of(n)
                    snippet = text[:200]
                    found = True
                    return
        for c in n.children:
            visit(c)
    visit(tree.root_node)

    return [make_finding(
        "static.animated_api_usage",
        category="runtime_jank",
        severity="medium",
        confidence="high",
        title="Uses React Native's Animated API",
        description="Animated runs animations on the JS thread; on busy frames they jitter. react-native-reanimated runs animations on the UI thread and is the recommended alternative.",
        file_path=file_path,
        function="<module>",
        line=line,
        code_snippet=snippet,
    )]


# ──────────────────────────────────────────────────────────────────────────────
# Rule 7: static.inline_object_props
# ──────────────────────────────────────────────────────────────────────────────
# Match: JSX attribute whose value is a literal `{...}` (object expression).
# Each parent render creates a fresh object reference, which breaks shallow-
# equality memoization in child components.
#
# Common shapes:
#   style={{ flex: 1, padding: 8 }}
#   contentContainerStyle={{ paddingTop: 16 }}
#   data={{ foo: bar }}  (rare but real)
#
# Excludes:
#   - style={styles.foo}     (named reference — fine)
#   - style={[styles.a, b]}  (array literal — flagged by a sibling rule, not here)
#   - children of plain HTML-like tags whose props are not memoized (e.g. <View />)
#     — still real, but lower severity. We flag uniformly and let synthesis
#       prioritize by enclosing component complexity.

def _attr_name(attr: Node, source: bytes) -> str | None:
    for c in attr.children:
        if c.type == "property_identifier":
            return node_text(c, source)
    return None


def _attr_value_expression(attr: Node) -> Node | None:
    """Return the inner expression of a `name={...}` attribute, or None."""
    for c in attr.children:
        if c.type == "jsx_expression":
            for cc in c.children:
                if cc.type in ("(", ")", "{", "}"):
                    continue
                return cc
    return None


# Style props specifically — call out for severity; non-style object literals
# are still flagged but at lower severity (often less impactful per render).
_STYLE_PROP_NAMES = {
    "style", "contentContainerStyle", "ListHeaderComponentStyle",
    "ListFooterComponentStyle", "columnWrapperStyle", "imageStyle",
}


def rule_inline_object_props(tree, file_path: str, source: bytes) -> list[dict]:
    findings: list[dict] = []

    def visit(node: Node):
        if node.type in ("jsx_self_closing_element", "jsx_opening_element"):
            for child in node.children:
                if child.type != "jsx_attribute":
                    continue
                name = _attr_name(child, source)
                if name is None:
                    continue
                value = _attr_value_expression(child)
                if value is None or value.type != "object":
                    continue
                is_style = name in _STYLE_PROP_NAMES
                findings.append(make_finding(
                    "static.inline_object_props",
                    category="runtime_jank",
                    severity="medium" if is_style else "low",
                    confidence="high",
                    title=f"Inline object literal as `{name}` prop",
                    description=(
                        f"`{name}={{{{...}}}}` creates a new object on every parent render. "
                        "Children that rely on shallow-equality memoization (`React.memo`, `PureComponent`, "
                        "`shouldComponentUpdate`) will always re-render because the prop reference changed. "
                        "Hoist the object out of the component, or use `StyleSheet.create` for style props."
                    ),
                    file_path=file_path,
                    function=enclosing_function_name(value, source),
                    line=line_of(value),
                    code_snippet=snippet_around(source, value, context_lines=1),
                ))
        for ch in node.children:
            visit(ch)

    visit(tree.root_node)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Rule 8: static.large_unmemoized_component
# ──────────────────────────────────────────────────────────────────────────────
# Match: a function component (function declaration or arrow assigned to a
# PascalCase const) longer than _LARGE_COMPONENT_LINE_THRESHOLD lines whose
# default/named export is NOT wrapped in `memo(...)` / `React.memo(...)`.
#
# Heuristic: a function is a "component" if it:
#   - has a PascalCase name (`MyScreen`, `ProductCard`), and
#   - returns JSX (we detect at least one jsx_element / jsx_self_closing_element
#     / jsx_fragment in its body)
#
# False positives we accept:
#   - Component intentionally not memoized because all of its renders are
#     state-driven (no prop-change re-renders). Pass A may downgrade to
#     UNCERTAIN when facts.source_pattern_counts.react_memo_count > 0
#     (i.e. the author knows about memo and chose not to apply it here).
#
# We do NOT flag tiny components (< threshold) because the per-render cost is
# low and adding memo everywhere wastes lookup time.

_LARGE_COMPONENT_LINE_THRESHOLD = 100


def _node_contains_jsx(node: Node) -> bool:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment"):
            return True
        stack.extend(n.children)
    return False


def _is_pascal_case(name: str) -> bool:
    return bool(name) and name[0].isupper() and name[0].isalpha()


def _file_uses_memo_for(name: str, tree, source: bytes) -> bool:
    """Return True if anywhere in the file we see `memo(Name)`, `React.memo(Name)`,
    or `const X = memo(Name)` / `export default memo(Name)`."""
    needles = (f"memo({name})", f"React.memo({name})")
    src = source.decode("utf-8", errors="replace")
    return any(n in src for n in needles)


def rule_large_unmemoized_component(tree, file_path: str, source: bytes) -> list[dict]:
    findings: list[dict] = []

    def emit(name: str, body_node: Node, definition_node: Node) -> None:
        line_count = body_node.end_point[0] - body_node.start_point[0] + 1
        if line_count < _LARGE_COMPONENT_LINE_THRESHOLD:
            return
        if not _node_contains_jsx(body_node):
            return
        if _file_uses_memo_for(name, tree, source):
            return
        findings.append(make_finding(
            "static.large_unmemoized_component",
            category="runtime_jank",
            severity="medium",
            confidence="medium",
            title=f"Large component `{name}` is not memoized",
            description=(
                f"`{name}` is {line_count} lines long and is not wrapped in `React.memo`. "
                "Any parent re-render reconciles its entire subtree even when its props are unchanged. "
                "Wrap in `React.memo` (with a `propsAreEqual` comparator if props include callbacks/objects) "
                "so the subtree only reconciles when its own state changes or when props actually differ."
            ),
            file_path=file_path,
            function=name,
            line=line_of(definition_node),
            code_snippet=snippet_around(source, definition_node, context_lines=1),
        ))

    def visit(node: Node):
        # Pattern A: `function Foo() { return <...>; }`
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            if name_node is not None and body_node is not None:
                name = node_text(name_node, source)
                if _is_pascal_case(name):
                    emit(name, body_node, node)

        # Pattern B: `const Foo = (...) => { ... }` or `const Foo = (...) => (...)`
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if (
                name_node is not None
                and value_node is not None
                and value_node.type in ("arrow_function", "function_expression")
            ):
                name = node_text(name_node, source)
                if _is_pascal_case(name):
                    body = value_node.child_by_field_name("body")
                    if body is not None:
                        emit(name, body, node)

        for ch in node.children:
            visit(ch)

    visit(tree.root_node)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Registry — register all rules here
# ──────────────────────────────────────────────────────────────────────────────

RuleFn = Callable[[object, str, bytes], list[dict]]

RULES: list[tuple[str, RuleFn]] = [
    ("static.scrollview_with_long_list",       rule_scrollview_with_long_list),
    ("static.image_without_caching",           rule_image_without_caching),
    ("static.inline_arrow_in_renderitem",      rule_inline_arrow_in_renderitem),
    ("static.useeffect_no_deps",               rule_useeffect_no_deps),
    ("static.useeffect_missing_cleanup",       rule_useeffect_missing_cleanup),
    ("static.console_log_in_production_code",  rule_console_log_in_production_code),
    ("static.animated_api_usage",              rule_animated_api_usage),
    ("static.inline_object_props",             rule_inline_object_props),
    ("static.large_unmemoized_component",      rule_large_unmemoized_component),
]


def parse_file(file_path: str, source: bytes):
    """Parse a single file with the appropriate tree-sitter grammar.
    Returns the parsed tree, or None if parsing failed."""
    try:
        parser = parser_for(file_path)
        return parser.parse(source)
    except Exception:
        return None


def run_all_rules(file_path: str, source: bytes) -> list[dict]:
    """Run every rule against one file. Per-rule failures are caught and
    converted into a tooling finding so one bad rule doesn't kill the scan."""
    tree = parse_file(file_path, source)
    if tree is None:
        return [{
            "id": "tooling.ast_parse_failed",
            "layer": "tooling",
            "category": "tooling_error",
            "severity": "low",
            "confidence": "high",
            "title": f"Could not parse {os.path.basename(file_path)}",
            "description": "tree-sitter failed to parse this file; static AST rules were skipped for it.",
            "evidence": {"file": file_path},
        }]
    out: list[dict] = []
    for rule_id, fn in RULES:
        try:
            out.extend(fn(tree, file_path, source))
        except Exception as e:
            out.append({
                "id": "tooling.rule_failure",
                "layer": "tooling",
                "category": "tooling_error",
                "severity": "low",
                "confidence": "high",
                "title": f"AST rule {rule_id} raised",
                "description": f"Rule {rule_id} raised {type(e).__name__}: {e}",
                "evidence": {"file": file_path, "metric_name": "exception", "code_snippet": str(e)[:200]},
            })
    return out
