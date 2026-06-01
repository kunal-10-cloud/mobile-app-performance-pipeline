#!/usr/bin/env python3
"""
Stage 7 — Render report.

Deterministically composes report.md from:
  - ${AUDIT_DIR}/report/report.json         (slot values from synthesize.py)
  - ${AUDIT_DIR}/report/prose_fills.json    (LLM's <<PROSE>> fills; optional —
                                              if absent, fall back to default
                                              prose stubs so the report still
                                              renders for CI / smoke tests)

Outputs:
  - ${AUDIT_DIR}/report/report.md           (the deliverable)
  - Prints report.md to stdout between
    ===MOBILE_PERF_AUDIT_REPORT_START===  /  ===MOBILE_PERF_AUDIT_REPORT_END===
    fences. This is the guaranteed delivery channel per SKILL.md Hard Mandate 5.

The full LLM-driven path:
    synthesize.py → synthesis_input.json
    LLM (Claude in calling session) reads synthesis_input.json + prompts/synthesize.md
        → writes prose_fills.json into report/
    render_report.py → report.md + stdout emit

If prose_fills.json is missing, render_report.py uses deterministic stubs.

Usage:
  python3 scripts/render_report.py <audit_id>
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
from pathlib import Path

# Force UTF-8 on stdout/stderr so emoji and other non-cp1252 characters in the
# report don't crash on Windows (the default Windows console codepage can't
# encode 🟢 / 🟡 / 🔴 / em-dashes). This is the same `PYTHONIOENCODING=utf-8`
# effect but applied unconditionally.
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    else:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


SEVERITY_DISPLAY = {"critical": "🔴 CRITICAL", "high": "🟡 HIGH", "low": "🟢 LOW"}


# ──────────────────────────────────────────────────────────────────────────────
# Deterministic per-rule libraries
#
# Used as fallbacks when the LLM prose-fill step is skipped or empty. These
# are intentionally generic — the LLM is what makes a finding's prose specific
# to *this* codebase. The deterministic strings here exist so the report still
# reads as a useful deliverable in CI / smoke-test runs.
# ──────────────────────────────────────────────────────────────────────────────

DETERMINISTIC_ACTIONABLES: dict[str, list[str]] = {
    # ── Static config ──
    "static.hermes_disabled": [
        "Set `\"jsEngine\": \"hermes\"` inside `expo` in `app.json` (or remove `jsEngine: \"jsc\"` if you're on SDK ≥ 50, where Hermes is the default).",
        "Run `npx expo prebuild --clean` to regenerate the native projects with the new engine.",
        "Measure cold-start before and after on a low-end Android emulator; expect a 30–50% reduction.",
        "Audit any third-party native modules that pin a non-Hermes binary before flipping in production.",
    ],
    "static.new_architecture_disabled": [
        "Add `\"newArchEnabled\": true` to the `expo` block in `app.json`.",
        "Run `npx expo prebuild --clean`; verify the Android build first (more stable than iOS New Arch today).",
        "For each third-party native module, confirm it's listed as New-Architecture compatible. If any aren't, defer the rollout for that module.",
        "Re-run device perf measurement after enabling to capture the FPS / startup improvement.",
    ],

    # ── Static AST ──
    "static.scrollview_with_long_list": [
        "Replace `<ScrollView>` with `<FlatList>` (built-in) or `<FlashList>` from `@shopify/flash-list` for collections that can grow beyond ~20 items.",
        "Move children currently produced by `items.map(...)` into the list's `data` + `renderItem` props.",
        "Pass a stable `keyExtractor={(it) => it.id}`; never use the array index as the key for items that mutate or get inserted.",
        "Wrap `renderItem` in `useCallback` so each parent render doesn't invalidate the list's memoization.",
    ],
    "static.image_without_caching": [
        "Install `expo-image`: `npx expo install expo-image`.",
        "Replace `import { Image } from 'react-native'` with `import { Image } from 'expo-image'` at this site.",
        "Add `cachePolicy=\"memory-disk\"` and a `placeholder` / `transition` for a smoother first-paint.",
        "Optionally prefetch images you know will appear soon via `Image.prefetch([uri, uri, ...])`.",
    ],
    "static.inline_arrow_in_renderitem": [
        "Extract the arrow into a `const renderItem = useCallback(({ item }) => ..., [/* row deps */])` defined in the component body.",
        "If the row truly has no closure over component state, hoist it to a module-level constant — even better than `useCallback`.",
        "Apply the same fix to `keyExtractor`, `ItemSeparatorComponent`, and any other inline-function prop on the list.",
        "Wrap the row component itself in `React.memo` so a stable `renderItem` reference actually pays off.",
    ],
    "static.useeffect_no_deps": [
        "Add an explicit dependency array. For mount-only effects: `useEffect(() => {...}, [])`.",
        "For state-driven effects: list every variable the callback reads as a dependency.",
        "When a dep is a recreated-each-render object/function, wrap its source in `useMemo`/`useCallback` so the reference is stable.",
        "If the effect intentionally runs every render, add a brief code comment explaining why — most static analyzers (including this one) honour the intent when it's documented.",
    ],
    "static.useeffect_missing_cleanup": [
        "Return a cleanup function from the effect: `useEffect(() => { const id = setInterval(...); return () => clearInterval(id); }, [deps])`.",
        "For `setTimeout`: capture the id and `clearTimeout` in the return — even one-shot timers can fire on an unmounted component and set state.",
        "For `addEventListener` / `.addListener`: stash the subscription handle and call `.remove()` (or `removeEventListener`) in the cleanup.",
        "For `.subscribe(...)`: assign the result and call `.unsubscribe()` (RxJS) or the returned function (Firebase / Zustand).",
        "For `expo-av` audio: `await sound.unloadAsync()` (and `recording.stopAndUnloadAsync()` for recorders) in the cleanup.",
        "For Firebase listeners (`onSnapshot` / `onValue` / `onAuthStateChanged` / `onMessage`): the call returns an unsubscribe function — `return unsub`.",
    ],
    "static.console_log_in_production_code": [
        "Gate the call: `if (__DEV__) { console.log(...) }` (zero cost in release builds — Hermes constant-folds it).",
        "Or add `babel-plugin-transform-remove-console` to `babel.config.js` to strip every call in production.",
        "Centralise via a `log()` helper that internally checks `__DEV__`; replace the call here with that helper.",
    ],
    "static.animated_api_usage": [
        "Install Reanimated: `npx expo install react-native-reanimated`.",
        "Add the babel plugin (must be the LAST entry in `plugins`): `'react-native-reanimated/plugin'`.",
        "Replace `import { Animated } from 'react-native'` with `import Animated from 'react-native-reanimated'`.",
        "Migrate `Animated.timing(value, { toValue }).start()` to `value.value = withTiming(toValue)`.",
        "Rebuild the native projects (`npx expo prebuild --clean`) since Reanimated includes a native module.",
    ],
    "static.inline_object_props": [
        "Move static style objects into a `StyleSheet.create({...})` block at the module top, then reference by name.",
        "For objects that depend on render-time values, wrap them in `useMemo(() => ({...}), [deps])`.",
        "For arrays passed as props, do the same with `useMemo(() => [...], [deps])`.",
        "Audit the receiving child — if it's not wrapped in `React.memo`, the inline object is harmless; the real win is a memoized child + a stable prop reference.",
    ],
    "static.large_unmemoized_component": [
        "Wrap the export: `export default React.memo(ProfileScreen)`.",
        "If any props are callbacks/objects, pair this with `useCallback`/`useMemo` at the parent so memoization actually short-circuits.",
        "Consider splitting the component into smaller pieces: smaller prop surfaces are cheaper to compare and easier to memoize selectively.",
        "Add a custom `propsAreEqual` second argument to `React.memo` only if the default shallow compare misbehaves.",
    ],

    # ── Bundle ──
    "bundle.bundle_too_large_warning": [
        "Open `_expo/static/js/<platform>/` and run `npx source-map-explorer <bundle>.js <bundle>.js.map` to see top contributors.",
        "Lazy-load non-first-screen routes with `expo-router`'s grouped layouts + dynamic imports.",
        "Remove unused dependencies — run `npx depcheck` and clear out anything not actually imported.",
        "Prefer ESM builds (`-es` packages) over CJS so the bundler can tree-shake.",
    ],
    "bundle.bundle_too_large_critical": [
        "Treat as a launch blocker — every additional 100 KiB adds tens of ms of cold-start parse on a low-end device.",
        "Audit the heaviest dependencies first (see the bundle composition section); replace or remove the biggest offenders.",
        "Split the app into routes / features and dynamic-import them on first navigation rather than eagerly at startup.",
    ],
    "bundle.dependency_oversized": [
        "Use per-method imports instead of the package's top-level barrel where supported.",
        "Switch to a leaner alternative if one exists (see `bundle.known_bloated_dependency` recommendations).",
        "If the dep is only used in one screen, dynamic-import it at that screen's entry.",
    ],
    "bundle.known_bloated_dependency": [
        "Switch to the lighter alternative noted in the finding's description.",
        "Migrate call-sites one screen at a time; both libraries can coexist during the migration window.",
        "After removal, re-run the bundle scan to confirm the bytes are actually gone (transitive dep retention is a common surprise).",
    ],
    "bundle.duplicate_dependency_libs": [
        "Pick the one library you actually need; uninstall the other with `npm uninstall <other>`.",
        "Codemod the import sites — most editors can do a project-wide find/replace.",
        "Run `npm dedupe` afterwards to collapse common subdeps.",
    ],
    "bundle.asset_image_too_large": [
        "Resize the source image to the largest in-app render dimension at 2x density (3x for iOS hero images).",
        "Re-encode: PNG → WebP often saves 25–50%; JPEG → MozJPEG saves ~10%.",
        "If the image is decorative, consider an SVG component instead of a raster file.",
        "If it ships for one screen only, lazy-load via `expo-image`'s `source={{uri: require(...)}}` instead of bundling.",
    ],
    "bundle.png_image_could_be_webp": [
        "Convert with `cwebp -q 80 in.png -o in.webp` (or a `sharp` script if you have many).",
        "Update the asset import path in source.",
        "If transparency is required, WebP supports it; otherwise prefer JPEG for photographic content.",
    ],
    "bundle.asset_total_too_large": [
        "Audit `assets/` for files that no source file references — remove them.",
        "Move rarely-used assets to remote URLs and load via `expo-image`.",
        "Compress font files: subset to the glyphs you actually use via `pyftsubset` or `glyphhanger`.",
    ],

    # ── Reassure ──
    "reassure.excessive_render_count": [
        "Identify which prop change is driving the extra renders (React DevTools' Profiler will tell you).",
        "Wrap heavy children in `React.memo`.",
        "If a context provider's `value` is recreated each render, wrap it in `useMemo`.",
        "Audit inline arrows / objects on memoized children — they defeat the memo's shallow compare.",
    ],
    "reassure.excessive_render_duration": [
        "Split the subtree: hoist the heaviest children into separately-memoized components.",
        "Move expensive computations into `useMemo` so they don't run on every render.",
        "Lazy-hydrate non-critical sections with `<Suspense>` boundaries.",
    ],

    # ── Device ──
    "device.fps_below_threshold": [
        "Open `results/<platform>.json` and find the per-step `intervals` entry with the lowest `fps_min`.",
        "Audit that screen for inline-arrow/object props on lists.",
        "Switch heavy lists to `@shopify/flash-list` (Android sees the biggest win).",
    ],
    "device.startup_too_slow": [
        "Enable Hermes if it isn't already (see `static.hermes_disabled`).",
        "Trim the JS bundle (see bundle composition section).",
        "Lazy-load non-first-screen routes via Expo Router's grouped layouts.",
    ],
    "device.memory_growth_suspected_leak": [
        "Re-run the flow in dev mode with React DevTools' Memory profiler open; compare heap snapshots before / after.",
        "Audit `useEffect` cleanups — every `addListener` needs a matching `removeListener`; every `setInterval` needs a `clearInterval`.",
        "Bound any in-memory caches (image cache, query cache) with explicit eviction.",
    ],
    "device.cpu_thread_saturated": [
        "If the saturated thread is the JS thread (`mqt_js` on Android): audit list virtualization and effects on the current screen.",
        "If it's the UI thread: review Reanimated worklets and synchronous native module calls.",
        "Capture a CPU profile with the Android Studio Profiler or Xcode Instruments to find the call stack.",
    ],
    "device.long_blocking_interval": [
        "Capture a CPU profile during the flow to identify the offending call stack.",
        "Common culprits: synchronous `JSON.parse` of large payloads, eager flat-mapping of long arrays, native bridge round-trips on tap.",
        "Defer the work with `InteractionManager.runAfterInteractions` or move it to a worker / native module.",
    ],
    "device.step_fps_dipped": [
        "The cited Maestro step anchors the jank to a specific screen — open that screen file and look for the usual suspects (inline props, missing memo, unbounded lists).",
        "Cross-reference any static-layer finding for the same file; the static finding usually points at the root cause.",
    ],
    # ── Stage 4f — backend / DB / algorithm rules (ported from web pipeline) ──
    "backend.sync_route_handler": [
        "Change `def handler(...)` to `async def handler(...)`; add `await` to every DB / HTTP call inside.",
        "If the handler is purely CPU-bound (config readers, in-memory lookups), it's fine as `def` — keep it.",
        "For sync libraries that can't be easily ported (e.g. `pymongo` instead of `motor`), wrap with `await asyncio.to_thread(...)`.",
        "Start with the highest-traffic handlers (auth, list, search) — those dominate the thread-pool starvation.",
    ],
    "backend.n_plus_one_query": [
        "Collect the keys before the loop: `ids = [item.id for item in items]`.",
        "Issue ONE batched query: `docs = await db.coll.find({'_id': {'$in': ids}}).to_list(len(ids))`.",
        "Build a lookup dict: `by_id = {d['_id']: d for d in docs}` and read from it inside the original loop.",
        "Watch for queries that filter by multiple fields — use `$in` on each, or restructure into a single `$or`.",
    ],
    "backend.unbounded_query": [
        "Add `skip: int = 0, limit: int = 50` query parameters to the route signature.",
        "Bound the cursor: `await db.coll.find(filter).skip(skip).limit(limit).to_list(limit)`.",
        "Return `{items, total, has_more}` so the frontend can paginate; or use a hard cap of `to_list(500)` for backward compatibility.",
        "Exempt one-time scripts / migrations — those legitimately need all rows; keep them out of the request path.",
    ],
    "backend.mongo_client_not_singleton": [
        "Move the `AsyncIOMotorClient(...)` call to module scope or a FastAPI `@app.on_event('startup')` handler.",
        "Inject the shared client (or its `db` handle) into route handlers via `Depends()`.",
        "Don't change connection-string sourcing (`os.environ['MONGO_URL']`) — only change WHERE the client is constructed.",
        "Verify: hit the endpoint twice, confirm `db.serverStatus().connections` doesn't increase.",
    ],
    "database.missing_index": [
        "Identify the top-traffic collections; for each, add `create_index(...)` calls in a `@app.on_event('startup')` hook (or a dedicated `init_indexes.py`).",
        "For range / sort queries, use compound indexes: `db.coll.create_index([('symbol', 1), ('timestamp', -1)])`.",
        "Run `db.coll.find(filter).explain()['executionStats']['stage']` — after the fix it should read `IXSCAN`, not `COLLSCAN`.",
        "Don't over-index — each index slows writes; favour compound indexes over many single-field ones.",
    ],
    "backend.sequential_await_chain": [
        "Confirm the awaits are independent (no later one reads from an earlier result).",
        "Wrap with `asyncio.gather`: `a, b, c = await asyncio.gather(call_a(), call_b(), call_c())`.",
        "Don't parallelize awaits that share rate limits (third-party APIs); check provider docs first.",
        "Latency drops from `sum(awaits)` to `max(awaits)` — measurable on a load test.",
    ],
    "backend.blocking_work_in_handler": [
        "Move fire-and-forget operations to `BackgroundTasks`: add `background: BackgroundTasks` to the signature and call `background.add_task(...)` after `return`.",
        "For operations the user needs the result of, return a `job_id` and let them poll a separate `/jobs/{job_id}` endpoint.",
        "For LLM / chat endpoints, consider `StreamingResponse` so the user sees tokens as they arrive.",
        "Persist job state in the DB so jobs survive server restarts.",
    ],
    "backend.no_projection_on_query": [
        "Add a projection as the second argument: `.find(filter, {'field1': 1, 'field2': 1, '_id': 0})`.",
        "Only project the fields the caller actually uses — drop everything else.",
        "Skip optimising endpoints where documents are small (a few KB); the bandwidth win is negligible.",
        "Profile high-frequency endpoints first; this is a marginal-but-cumulative gain.",
    ],
    "backend.pydantic_complex_model": [
        "Profile the endpoint first — Pydantic v2 validates in Rust and is rarely a bottleneck.",
        "If validation shows up in flame graphs, split the model into smaller per-concern models OR switch internal-only paths to `TypedDict` / `dataclass`.",
        "Don't change external-facing endpoints' validation without a compatibility plan.",
        "Consider `model_config = ConfigDict(validate_assignment=False)` if you only need validation at the boundary.",
    ],
    "algorithms.nested_iteration": [
        "Build a `Map` / `Set` once before the outer loop, then look up inside it: `const idsToFind = new Set(targets); items.filter(i => idsToFind.has(i.id))`.",
        "Pair this with `useMemo` if the inner lookup data depends on render-time props — don't rebuild the map every render.",
        "Watch the output shape: preserve order and key uniqueness as the original code did.",
        "Verify with a 5k-item dataset — the UI should stay smooth after the change.",
    ],
    "algorithms.linear_array_lookup_in_loop": [
        "Convert the lookup array to a `Set` (JS) or `set()` (Python) before the loop: `const ids = new Set(arr); if (ids.has(x))`.",
        "Semantics are identical; only the membership check moves from O(n) to O(1).",
        "Where the data structure has to support more than membership (e.g. index lookups), use a `Map` instead.",
        "Marginal at small scale — only worth changing when the loop iterates over hundreds of items.",
    ],
    "backend.sequential_fetch_chain": [
        "If the calls are independent, run them with `Promise.all([fetch(a), fetch(b)])` — total time becomes `max(latencies)`.",
        "If they ARE dependent, leave them sequential and document why (a code comment is enough).",
        "Watch for shared rate limits with the same provider; running in parallel can trip throttles.",
        "Verify the network panel — you should see overlapping request bars, not stacked ones.",
    ],
}


DETERMINISTIC_AFTER_FIXING: dict[str, str] = {
    "static.hermes_disabled": "Cold start drops by ~30–50% on low-end Android; iOS sees ~10–25%.",
    "static.new_architecture_disabled": "JS↔native component updates synchronise without the bridge, removing a class of frame drops.",
    "static.scrollview_with_long_list": "Only the on-screen rows mount, so initial render is constant-time and memory pressure stays flat as the list grows.",
    "static.image_without_caching": "Repeat visits to the screen no longer re-download images; visual flicker on navigation disappears.",
    "static.inline_arrow_in_renderitem": "Rows stop re-rendering on every parent render; scroll FPS recovers on long lists.",
    "static.useeffect_no_deps": "The effect runs exactly when its inputs change — no more render loops, no surprise side-effects.",
    "static.useeffect_missing_cleanup": "The timer/listener/subscription is released when the component unmounts or the effect re-runs — memory stays bounded across long sessions and rapid navigations.",
    "static.console_log_in_production_code": "JSI bridge traffic drops; tiny but real CPU savings, plus production logs stop leaking serialised data.",
    "static.animated_api_usage": "Animations stay smooth when the JS thread is busy; complex gestures gain ~30% headroom.",
    "static.inline_object_props": "Memoized children short-circuit when their inputs haven't actually changed — measurable on heavy screens.",
    "static.large_unmemoized_component": "Parent re-renders no longer reconcile the whole subtree; the screen feels snappier on every interaction.",
    "bundle.bundle_too_large_warning": "Cold start improves linearly with bytes shaved; users on cheap phones feel it most.",
    "bundle.bundle_too_large_critical": "App becomes installable / launchable in regions with strict bundle-size policies; cold start gets back inside the 'instant' band.",
    "bundle.dependency_oversized": "Bundle bytes drop directly; cold start improves; tree-shaking opens up if you also adopt ESM.",
    "bundle.known_bloated_dependency": "Direct bundle-size savings plus the secondary win of the lighter library's smaller API surface.",
    "bundle.duplicate_dependency_libs": "Cleaner dependency graph; no more 'why are both installed' confusion in code review.",
    "bundle.asset_image_too_large": "First reference to the asset no longer blocks the bundle; memory pressure on the screen using it drops.",
    "bundle.png_image_could_be_webp": "Asset bytes drop by 25–50% with no perceptible quality loss.",
    "bundle.asset_total_too_large": "Install size shrinks; OTA-update payloads ship faster.",
    "reassure.excessive_render_count": "State changes only re-render the components that actually depend on them.",
    "reassure.excessive_render_duration": "Each state change completes inside one frame budget; jank disappears on the affected screens.",
    "device.fps_below_threshold": "Sustained 60 FPS during the audited flow; visible jank disappears.",
    "device.startup_too_slow": "App reaches first interactive frame inside the 1.5 s 'instant' band.",
    "device.memory_growth_suspected_leak": "Heap stabilises across iterations; app survives long sessions on memory-constrained devices.",
    "device.cpu_thread_saturated": "The bottleneck thread drops below 70% average; remaining work fits inside the frame budget.",
    "device.long_blocking_interval": "Main-thread freezes disappear; tap-to-response latency drops back into the perceptual 'instant' range.",
    "device.step_fps_dipped": "The specific screen / interaction that the audit anchored the dip to recovers to smooth 60 FPS.",
    # ── Stage 4f — backend / DB / algorithm "after fixing" lines ──
    "backend.sync_route_handler":          "The thread pool stops being the bottleneck; ~10× more concurrent users fit on the same hardware.",
    "backend.n_plus_one_query":            "Endpoint latency stops scaling linearly with payload size — N round-trips collapse to one.",
    "backend.unbounded_query":             "Response size stays bounded; memory and bandwidth costs stop growing with the database.",
    "backend.mongo_client_not_singleton":  "Connection pool count holds steady; endpoint latency drops 50–200 ms per request.",
    "database.missing_index":              "Queries on the affected fields go from full-collection scans (seconds at scale) to indexed lookups (milliseconds).",
    "backend.sequential_await_chain":      "Total latency drops from `sum(awaits)` to `max(awaits)` — visible on flame graphs and load tests.",
    "backend.blocking_work_in_handler":    "The UI returns immediately; external-service latency stops bleeding into user-visible response time.",
    "backend.no_projection_on_query":      "Response payload size drops; marginal but cumulative across high-frequency endpoints.",
    "backend.pydantic_complex_model":      "Validation CPU usage drops on the hottest endpoints — only worth doing if the profiler points here.",
    "algorithms.nested_iteration":         "Operation scales linearly with data size instead of quadratically; UI doesn't freeze on large datasets.",
    "algorithms.linear_array_lookup_in_loop": "Per-iteration cost drops from O(n) to O(1); total drops from O(n×m) to O(n+m).",
    "backend.sequential_fetch_chain":      "Page load drops from `sum(round-trips)` to `max(round-trip)` when the calls are independent.",
}


DETERMINISTIC_PLAIN_TERMS: dict[str, str] = {
    "static.hermes_disabled": "It's like shipping an app that hasn't been packed properly — every cold start re-does work that the optimised engine would have done once at build time.",
    "static.new_architecture_disabled": "Imagine a courier handing every parcel to a translator before delivery; the new architecture removes the translator for component updates.",
    "static.scrollview_with_long_list": "It's like printing every page of a book the moment you open it. A virtualised list only prints what's currently visible.",
    "static.image_without_caching": "Like a website that re-downloads every image every time you reload, even if you just saw it five seconds ago.",
    "static.inline_arrow_in_renderitem": "Every parent re-render hands the list a brand-new function and tells it 'this is different', so the list redraws every row to be safe.",
    "static.useeffect_no_deps": "A side-effect with no rules about when to run will run every single render — including ones it had no business running on.",
    "static.useeffect_missing_cleanup": "The component opened a door (timer, listener, audio session) and walked away without closing it. Every navigation opens another door; eventually the room is full of open doors and the phone runs out of air.",
    "static.console_log_in_production_code": "Each log call is a small toll the app pays every time it runs. Mostly cheap, but tolls add up on slow phones.",
    "static.animated_api_usage": "It's like animating a UI by re-rendering frames in software when the GPU is right there waiting.",
    "static.inline_object_props": "Memoization is the React equivalent of 'has anything actually changed?'. An inline object always looks new, so the check always says yes.",
    "static.large_unmemoized_component": "Without memoization, asking a 100-line component to update is like rewriting an entire page when only one paragraph changed.",
    # ── Stage 4f — backend / DB / algorithm plain-terms metaphors ──
    "backend.sync_route_handler":          "Imagine a restaurant where every waiter has to wait at the kitchen while a single dish cooks. With async, the waiter places the order and goes help other tables.",
    "backend.n_plus_one_query":            "Going to the grocery store for ONE item, driving home, then driving back for the next — repeated 100 times. Same outcome, 100× the time.",
    "backend.unbounded_query":             "Asking the librarian 'give me every book on strategy.' Works fine in a small library; breaks the librarian's back at city scale.",
    "backend.mongo_client_not_singleton":  "Building a new road from scratch every time a single car needs to drive somewhere — instead of having one road everyone shares.",
    "database.missing_index":              "Looking up a name in the phone book by reading every page — when the alphabetical index is right there.",
    "backend.sequential_await_chain":      "Three people in line for three different counters, each waiting their turn at all three — when they could just split up and finish in a third the time.",
    "backend.blocking_work_in_handler":    "Calling a takeout place but keeping the line open for 20 minutes while they cook, instead of them calling you back when it's ready.",
    "backend.no_projection_on_query":      "Ordering off the entire menu when you just wanted the soup. Most of the order gets thrown away.",
    "backend.pydantic_complex_model":      "Putting every package through a full customs inspection when most contain socks — overkill for the hot path, fine for the boundary.",
    "algorithms.nested_iteration":         "Looking for one person at a party by asking every guest, and that person also asks every guest. The whole party stalls.",
    "algorithms.linear_array_lookup_in_loop": "Checking the entire guest list every time a new guest walks in — instead of keeping a checklist of who's expected.",
    "backend.sequential_fetch_chain":      "Three deliveries arriving back-to-back when they could have shown up at the same time. No reason they shouldn't.",
}


def _line_or_blank(v) -> str:
    if v in (None, 0, ""):
        return ""
    return f":{v}"


SEVERITY_DISPLAY_FULL = {
    "critical": "🔴 CRITICAL",
    "high":     "🟡 HIGH",
    "medium":   "🟡 MEDIUM",
    "low":      "🟢 LOW",
    "info":     "ℹ️ INFO",
}


def _human_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MiB"
    if n >= 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n} B"


def _render_device_section(device: dict) -> list[str]:
    """Render the device_metrics_table dict (one row per platform) as a
    markdown table plus warning bullets."""
    out: list[str] = []
    platforms = device.get("platforms") or {}
    if not platforms:
        return out

    def _fmt(v, unit=""):
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.1f}{unit}"
        return f"{v}{unit}"

    out.append("| Metric | Android | iOS |")
    out.append("|--------|--------:|----:|")
    ax = platforms.get("android") or {}
    ix = platforms.get("ios") or {}
    out.append(f"| Device profile | {ax.get('device_profile','—')} | {ix.get('device_profile','—')} |")
    out.append(f"| Iterations | {_fmt(ax.get('iterations_count'))} | {_fmt(ix.get('iterations_count'))} |")
    out.append(f"| Cold start | {_fmt(ax.get('startup_time_ms'), ' ms')} | {_fmt(ix.get('startup_time_ms'), ' ms')} |")
    out.append(f"| Mean FPS | {_fmt(ax.get('fps_avg_mean'))} | {_fmt(ix.get('fps_avg_mean'))} |")
    out.append(f"| Peak memory | {_fmt(ax.get('memory_peak_mb_max'), ' MB')} | {_fmt(ix.get('memory_peak_mb_max'), ' MB')} |")
    out.append(f"| Memory growth (sum) | {_fmt(ax.get('memory_growth_mb_total'), ' MB')} | {_fmt(ix.get('memory_growth_mb_total'), ' MB')} |")
    out.append(f"| Tool score | {_fmt(ax.get('score'))} | {_fmt(ix.get('score'))} |")
    out.append("")

    warnings: list[str] = []
    for plat, data in platforms.items():
        for w in (data.get("warnings") or []):
            warnings.append(f"_{plat}_: {w}")
    if warnings:
        out.append("**Measurement notes:**")
        for w in warnings:
            out.append(f"- {w}")
        out.append("")
    return out


_RATING_EMOJI = {"good": "🟢", "needs": "🟡", "poor": "🔴", "not_measured": "—", "skipped": "—"}
_RELIABILITY_EMOJI = {
    "reliable":               "🟢",
    "device-class estimate":  "🟡",
    "directional":            "🟡",
    "regression-relative":    "🟠",
    "unreliable on Simulator": "🔴",
    "not modeled":            "🔴",
}


def _render_device_lighthouse_ios(dl: dict) -> list[str]:
    """Render the iOS Lighthouse-style breakdown with per-metric reliability
    labels. iOS Simulator metrics are honest-by-construction: each row says
    what you can and can't trust about its value."""
    metrics = dl.get("metrics") or []
    if not metrics:
        return []
    env = dl.get("measurement_environment") or {}
    on_as = bool(env.get("on_apple_silicon"))
    host_arch = env.get("host_arch") or "unknown"
    profile = dl.get("device_profile") or "iOS Simulator"

    out: list[str] = []
    if on_as:
        env_line = (f"_Measured on **{profile}** (Apple Silicon Mac, host arch `{host_arch}`). "
                    f"CPU + memory metrics are device-class estimates (~30% optimistic vs iPhone); "
                    f"memory growth across iterations is fully reliable. FPS is intentionally omitted — "
                    f"Mac GPU is not iPhone-comparable._")
    else:
        env_line = (f"_Measured on **{profile}** (Intel Mac, host arch `{host_arch}`). "
                    f"Cold start + peak memory are **regression-relative only** (not device-comparable); "
                    f"memory growth across iterations is reliable. FPS is intentionally omitted._")
    out.append(env_line)
    out.append("")
    out.append("| Metric | Value | Target | Rating | Reliability |")
    out.append("|--------|------:|-------:|:------:|:-----------:|")
    for m in metrics:
        rating_emoji = _RATING_EMOJI.get(m.get("rating", ""), "")
        rel = m.get("reliability", "")
        rel_emoji = _RELIABILITY_EMOJI.get(rel, "")
        out.append(f"| {m.get('metric','')} | {m.get('display','—')} | "
                   f"{m.get('target','—')} | {rating_emoji} | {rel_emoji} {rel} |")
    out.append("")

    insights = [(m.get("metric", ""), m.get("insight", ""), m.get("rating", ""))
                for m in metrics if m.get("insight")]
    if insights:
        out.append("**What each metric means here:**")
        out.append("")
        for name, insight, rating in insights:
            emoji = _RATING_EMOJI.get(rating, "")
            out.append(f"- {emoji} **{name}** — {insight}")
        out.append("")

    out.append("> **For device-quality iOS metrics** (real cold start latency, FPS, thermal "
               "behaviour, energy impact, real-device memory pressure): profile on a real iPhone "
               "via Xcode Instruments → Time Profiler + Allocations. Requires a paid Apple Developer "
               "Program account and a provisioning profile — outside this audit's scope.")
    out.append("")
    return out

_VERDICT_EMOJI = {"READY": "🟢", "AT_RISK": "🟡", "BLOCKED": "🔴"}
_VERDICT_LABEL = {"READY": "READY", "AT_RISK": "AT-RISK", "BLOCKED": "BLOCKED"}
_SEV_EMOJI = {"critical": "🔴", "high": "🟡", "medium": "🟢", "low": "🟢", "info": "ℹ️"}


def _render_publishing_section(verdict: dict, table: dict) -> list[str]:
    """Stage 4e — Pre-publish readiness.
    Verdict banner + per-store blocker tables + auto-assessed process items
    + SKU enumeration + Nutrition Label / Data Safety per detected SDK +
    static manual checklist."""
    out: list[str] = []
    apple = verdict.get("apple", "READY")
    google = verdict.get("google", "READY")
    counts = verdict.get("counts") or {}

    def _vline(plat_label: str, plat_key: str, plat_verdict: str) -> str:
        c = counts.get(f"{plat_key}_critical", 0) + counts.get("cross_critical", 0)
        h = counts.get(f"{plat_key}_high", 0) + counts.get("cross_high", 0)
        m = counts.get(f"{plat_key}_medium", 0) + counts.get("cross_medium", 0)
        return (
            f"**{plat_label}:** {_VERDICT_EMOJI.get(plat_verdict,'')} "
            f"{_VERDICT_LABEL.get(plat_verdict, plat_verdict)} · "
            f"{c} critical · {h} high · {m} medium"
        )

    out.append(_vline("Apple App Store", "apple", apple))
    out.append("")
    out.append(_vline("Google Play Store", "google", google))
    out.append("")
    out.append("> _Publishing verdict is independent of the perf score — these are publication-gate items, not runtime issues._")
    out.append("")

    def _blocker_table(title: str, rows: list[dict]) -> list[str]:
        if not rows:
            return [f"### {title}", "", "_No blockers detected._", ""]
        b: list[str] = [f"### {title}", "", "| Severity | Finding | Where |", "|----------|---------|-------|"]
        for r in rows:
            sev = (r.get("severity") or "").lower()
            emoji = _SEV_EMOJI.get(sev, "")
            ev = r.get("evidence") or {}
            where = f"`{ev.get('file','app.json')}`"
            if ev.get("function") and ev["function"] not in ("<expo>", "<config>"):
                where += f" → `{ev['function']}`"
            b.append(f"| {emoji} {sev.upper()} | {r.get('title','')} | {where} |")
        b.append("")
        return b

    out.extend(_blocker_table("Apple App Store — code/config blockers", table.get("apple_blockers") or []))
    out.extend(_blocker_table("Google Play Store — code/config blockers", table.get("google_blockers") or []))

    # Auto-assessed process items
    auto = table.get("auto_assessed_process") or []
    if auto:
        out.append("### Auto-assessed process items")
        out.append("")
        out.append("| Item | Status |")
        out.append("|------|--------|")
        for r in auto:
            sev = (r.get("severity") or "").lower()
            emoji = _SEV_EMOJI.get(sev, "")
            out.append(f"| {r.get('title','')} | {emoji} {sev.upper()} |")
        out.append("")
        out.append("**Details:**")
        out.append("")
        for r in auto:
            sev = (r.get("severity") or "").lower()
            emoji = _SEV_EMOJI.get(sev, "")
            out.append(f"- {emoji} **{r.get('title','')}** — {r.get('description','')}")
        out.append("")

    # IAP SKU enumeration
    iap = table.get("iap_skus") or []
    if iap:
        out.append("### SKUs you must create in both consoles (extracted from source)")
        out.append("")
        for r in iap:
            out.append(r.get("description", ""))
        out.append("")

    # Nutrition Labels per SDK
    labels = table.get("nutrition_labels") or []
    if labels:
        out.append("### Privacy disclosures required (driven by detected SDKs)")
        out.append("")
        for r in labels:
            out.append(f"**{r.get('title','')}**")
            out.append("")
            out.append(r.get("description", ""))
            out.append("")

    # Manual checklist
    manual = table.get("manual_checklist") or []
    if manual:
        out.append("### Manual process items (audit cannot verify)")
        out.append("")
        for item in manual:
            out.append(f"- [ ] {item}")
        out.append("")
    return out


def _render_device_lighthouse(dl: dict) -> list[str]:
    """Render the per-metric, individually-rated device breakdown (the primary
    device-runtime surface). One row per metric: value, target, 🟢/🟡/🔴 rating.
    Insights for non-passing metrics are listed below the table."""
    metrics = dl.get("metrics") or []
    if not metrics:
        return []
    out: list[str] = []
    profile = dl.get("device_profile") or "unknown"
    out.append(f"_Measured on {profile}. Each metric is rated individually — "
               f"a single composite score hides which dimension is broken._")
    out.append("")
    out.append("| Metric | Value | Target | Rating |")
    out.append("|--------|------:|-------:|:------:|")
    for m in metrics:
        emoji = _RATING_EMOJI.get(m.get("rating", ""), "")
        out.append(f"| {m.get('metric','')} | {m.get('display','—')} | "
                   f"{m.get('target','—')} | {emoji} |")
    out.append("")

    insights = [(m.get("metric", ""), m.get("insight", ""), m.get("rating", ""))
                for m in metrics if m.get("insight")]
    if insights:
        out.append("**What the failing metrics mean:**")
        out.append("")
        for name, insight, rating in insights:
            emoji = _RATING_EMOJI.get(rating, "")
            out.append(f"- {emoji} **{name}** — {insight}")
        out.append("")
    return out


def _render_bundle_section(bundle: dict) -> list[str]:
    """Render the bundle_table dict as a series of markdown sub-sections."""
    out: list[str] = []
    platforms = bundle.get("platforms") or {}
    if platforms:
        out.append("### Per-platform JS bundle size")
        out.append("")
        out.append("| Platform | Bytes |")
        out.append("|----------|------:|")
        for plat in sorted(platforms.keys()):
            out.append(f"| {plat} | {_human_bytes(platforms[plat])} |")
        out.append("")

    heavy = bundle.get("heavy_dependencies") or []
    if heavy:
        out.append("### Heaviest dependencies in bundle")
        out.append("")
        out.append("| Package | Bytes |")
        out.append("|---------|------:|")
        for h in heavy:
            out.append(f"| `{h['package']}` | {_human_bytes(h['bytes'])} |")
        out.append("")

    dups = bundle.get("duplicate_pairs") or []
    if dups:
        out.append("### Duplicate-purpose libraries")
        out.append("")
        for pair in dups:
            out.append(f"- {pair}")
        out.append("")

    large_assets = bundle.get("large_assets") or []
    if large_assets:
        out.append("### Oversized image assets")
        out.append("")
        out.append("| Path | Bytes |")
        out.append("|------|------:|")
        for a in large_assets:
            out.append(f"| `{a['path']}` | {_human_bytes(a['bytes'])} |")
        out.append("")

    pngs = bundle.get("png_candidates_for_webp") or []
    if pngs:
        out.append("### PNGs that could be WebP")
        out.append("")
        out.append("| Path | Bytes |")
        out.append("|------|------:|")
        for a in pngs:
            out.append(f"| `{a['path']}` | {_human_bytes(a['bytes'])} |")
        out.append("")

    nit = bundle.get("non_image_asset_total_bytes")
    if isinstance(nit, (int, float)) and nit:
        out.append(f"_Non-image assets total: {_human_bytes(int(nit))}_")
        out.append("")

    return out


def default_prose(slot_id: str, finding: dict | None = None) -> str:
    """Fallback prose used when the LLM didn't fill a region. Sourced from
    deterministic per-rule libraries above + the worker's own description, so
    the report reads as a useful deliverable even with no LLM round-trip."""
    if slot_id == "exec_summary":
        return (
            "_Executive summary prose was not generated by an LLM in this run — the per-finding "
            "breakdown below carries the substantive content from each analyzer. To get a tailored "
            "summary, run the audit with the synthesis prompt enabled._"
        )
    if slot_id == "highest_impact_action":
        # Top finding's title is usually the right answer.
        return "_See the top finding above for the single highest-impact action._"
    if slot_id == "metrics_plainterms":
        return "_(Metrics dashboard prose was not generated.)_"
    if finding is None:
        return ""

    rule_id = finding.get("id", "")
    loc = finding.get("location") or {}
    file_path = loc.get("file", "?")
    function = loc.get("function") or "<module>"

    if slot_id.endswith("__evidence_prose"):
        title = finding.get("title", "")
        # Prefer the rule's title; fall back to "matches the detection rule".
        if title:
            return f"The analyzer detected `{title}` at `{file_path} — {function}`."
        return f"`{file_path} — {function}` matches the `{rule_id}` rule."

    if slot_id.endswith("__impact_prose"):
        desc = finding.get("description") or ""
        return desc or "_(Impact prose was not generated.)_"

    if slot_id.endswith("__plain_terms"):
        return DETERMINISTIC_PLAIN_TERMS.get(rule_id, "")

    if slot_id.endswith("__actionables"):
        bullets = DETERMINISTIC_ACTIONABLES.get(rule_id)
        if bullets:
            return "\n".join(f"- {b}" for b in bullets)
        # Last-resort: pull the last sentence of the rule's description.
        desc = finding.get("description") or ""
        if desc:
            tail = [s.strip() for s in desc.strip().split(".") if s.strip()]
            if tail:
                return f"- {tail[-1]}."
        return "- _(No deterministic actionables for this rule yet.)_"

    if slot_id.endswith("__after_fixing"):
        return DETERMINISTIC_AFTER_FIXING.get(rule_id, "")

    return ""


def render_finding_block(f: dict, prose: dict[str, str], count_label: str, priority_label: str) -> str:
    fid = f.get("id", "unknown")
    title = f.get("title", fid)
    category = f.get("category", "")
    severity = f.get("severity", "")
    confidence = f.get("confidence", "")
    layer = f.get("layer", "")
    loc = f.get("location") or {}
    file_path = loc.get("file") or ""
    function = loc.get("function") or ""
    line_num = loc.get("line") or 0
    code_snippet = (f.get("code_snippet") or "").rstrip()
    metric_name = f.get("metric_name")
    metric_value = f.get("metric_value")
    metric_threshold = f.get("metric_threshold")

    def get(slot, default_finding=None):
        key = f"{fid}__{slot}"
        if key in prose and prose[key].strip():
            return prose[key].strip()
        return default_prose(key, default_finding or f)

    lines: list[str] = []
    lines.append(f"#### **{priority_label}** · {fid} · `{category}` · {title}")
    lines.append("")
    if count_label:
        lines.append(f"_{count_label}_")
        lines.append("")

    # Metadata strip — severity / confidence / layer / count are all metadata
    # the reader wants at a glance.
    meta_pieces = [f"**Severity:** {severity}", f"**Confidence:** {confidence}"]
    if layer:
        meta_pieces.append(f"**Layer:** {layer}")
    lines.append(" · ".join(meta_pieces))
    lines.append("")

    # Location with line number when available
    if file_path:
        line_suffix = _line_or_blank(line_num)
        lines.append(f"**Location:** `{file_path}{line_suffix}` — `{function}`")
        lines.append("")

    # Measurement (for bundle / device / reassure findings)
    if metric_name and metric_value is not None:
        if metric_threshold is not None:
            lines.append(f"**Measurement:** `{metric_name}` = `{metric_value}` (threshold: `{metric_threshold}`)")
        else:
            lines.append(f"**Measurement:** `{metric_name}` = `{metric_value}`")
        lines.append("")

    # Evidence prose
    lines.append(f"**Evidence.** {get('evidence_prose')}")
    lines.append("")

    # Code snippet — the AST rule captured this; show it so the reader sees
    # the actual problematic code rather than just a file:function citation.
    if code_snippet:
        lang_hint = _lang_from_ext(file_path)
        lines.append("**Where in the code:**")
        lines.append("")
        lines.append(f"```{lang_hint}")
        lines.append(code_snippet)
        lines.append("```")
        lines.append("")

    # Impact prose
    lines.append(f"**Impact.** {get('impact_prose')}")
    lines.append("")

    plain = get("plain_terms")
    if plain and plain.strip():
        lines.append(f"> **In plain terms:** {plain}")
        lines.append("")

    # Actionables — guaranteed non-empty when DETERMINISTIC_ACTIONABLES has
    # the rule.
    actionables = get("actionables")
    lines.append("**What to do:**")
    lines.append("")
    for ln in actionables.splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        if ln.lstrip().startswith(("-", "*")):
            lines.append(ln.lstrip())
        else:
            lines.append(f"- {ln.lstrip()}")
    lines.append("")

    after = get("after_fixing")
    if after:
        lines.append(f"**After fixing.** {after}")
        lines.append("")

    # Fix-diff (Slice 4) — only emitted for top-N findings whose source was
    # bundled and where the LLM produced a non-empty diff.
    if f.get("has_fix_diff_slot"):
        diff_key = f"{fid}__fix_diff"
        diff_text = (prose.get(diff_key) or "").strip()
        if diff_text:
            lines.append("**Suggested fix:**")
            lines.append("")
            lines.append("```diff")
            lines.append(diff_text)
            lines.append("```")
            lines.append("")

    # Related findings (when dedupe rolled siblings under this lead)
    related = f.get("related_finding_ids") or []
    if related:
        lines.append(f"_Related: {', '.join(f'`{r}`' for r in related)}_")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _yes_no(v) -> str:
    if v is True: return "✅ Yes"
    if v is False: return "❌ No"
    return "—"


GLOSSARY_ENTRIES = [
    ("Cold start", "Time from app launch to the first interactive frame. Dominated by JS bundle parse + module evaluation on first run."),
    ("Frame budget", "16.6 ms on a 60 Hz display. A render taking longer than one frame budget drops a frame."),
    ("Hermes", "Meta's JS engine optimised for React Native. Reduces cold start by 30–50% vs JavaScriptCore through bytecode precompilation."),
    ("JSI bridge", "The boundary between JS and native code. Calls across the bridge are expensive; minimising them is a perf goal."),
    ("New Architecture", "Fabric + TurboModules — React Native's rewrite that removes the bridge for component updates and enables synchronous JS↔native calls. Recommended on Expo SDK ≥ 51."),
    ("Virtualization", "Rendering only the on-screen rows of a long list (`FlatList`/`FlashList`) instead of mounting all rows up front."),
    ("Reanimated", "`react-native-reanimated` — the preferred animation library. Runs animations on the UI thread, not the JS thread."),
    ("FlashList", "`@shopify/flash-list` — a high-performance replacement for `FlatList` with better memory characteristics for long lists."),
    ("expo-image", "Expo's image component with built-in memory + disk caching; preferred over React Native's `<Image>` for remote URIs."),
    ("Render duration", "Wall-clock time the React reconciler spends rendering a component subtree on a state change."),
    ("FPS", "Frames per second sustained during animation / scrolling. Below 50 FPS is perceptibly janky."),
    ("Memory growth", "Sustained or growing heap usage across iterations of the same flow. A positive trend suggests a leak."),
    ("Bundle size", "Bytes of compiled JavaScript shipped per platform. Every additional 100 KiB adds tens of ms of parse on low-end devices."),
]


def _render_glossary() -> list[str]:
    out: list[str] = []
    for term, definition in GLOSSARY_ENTRIES:
        out.append(f"- **{term}** — {definition}")
    return out


def _lang_from_ext(path: str) -> str:
    p = path.lower()
    if p.endswith(".tsx"): return "tsx"
    if p.endswith(".jsx"): return "jsx"
    if p.endswith(".ts"):  return "ts"
    if p.endswith(".js"):  return "js"
    if p.endswith(".json"): return "json"
    return ""


def render(report_json: dict, prose: dict[str, str]) -> str:
    sig = report_json.get("project_signature", {}) or {}
    sev = report_json.get("severity_totals", {}) or {}
    out: list[str] = []

    # Header
    out.append(f"# Mobile performance audit — `{report_json.get('audit_id','?')}`")
    out.append("")
    out.append(
        f"**Verdict:** {report_json.get('verdict_emoji','')} **{report_json.get('verdict','')}** · "
        f"**Overall Score:** {report_json.get('overall_score',0)} / 100"
    )
    proj_parts = [
        f"Expo SDK {sig.get('expo_sdk_version','unknown')}",
        "TypeScript" if sig.get("typescript_present") else "JavaScript",
    ]
    if sig.get("expo_router_present"):
        proj_parts.append("Expo Router")
    elif sig.get("react_navigation_present"):
        proj_parts.append("React Navigation")
    out.append(f"**Project:** {' · '.join(proj_parts)}")
    out.append(f"**Audit ID:** `{report_json.get('audit_id','?')}`")
    out.append(f"**Generated:** {report_json.get('audit_date','')}")
    out.append("")
    out.append("---")
    out.append("")

    # Executive summary
    out.append("## Executive Summary")
    out.append("")
    out.append(prose.get("exec_summary", default_prose("exec_summary")))
    out.append("")

    # Project snapshot (NEW) — drives the reader's mental model before findings.
    snap = report_json.get("project_snapshot") or {}
    deps = report_json.get("dependencies_snapshot") or {}
    if snap or deps:
        out.append("### Project snapshot")
        out.append("")
        out.append("| Property | Value |")
        out.append("|----------|-------|")
        out.append(f"| Expo SDK | {snap.get('expo_sdk_version','—')} |")
        if snap.get("react_native_version"):
            out.append(f"| React Native | {snap['react_native_version']} |")
        if snap.get("react_version"):
            out.append(f"| React | {snap['react_version']} |")
        out.append(f"| Language | {'TypeScript' if snap.get('typescript_present') else 'JavaScript'} |")
        out.append(f"| Routing | {'Expo Router' if snap.get('expo_router_present') else ('React Navigation' if snap.get('react_navigation_present') else 'unidentified')} |")
        out.append(f"| Hermes | {_yes_no(snap.get('hermes_enabled'))} |")
        out.append(f"| New Architecture | {_yes_no(snap.get('new_architecture_enabled'))} |")
        if snap.get("android_package"):
            out.append(f"| Android package | `{snap['android_package']}` |")
        if snap.get("ios_bundle_identifier"):
            out.append(f"| iOS bundle ID | `{snap['ios_bundle_identifier']}` |")
        if snap.get("production_dependency_count") is not None:
            extra = f" (+ {snap.get('dev_dependency_count', 0)} dev)" if snap.get("dev_dependency_count") is not None else ""
            out.append(f"| Production dependencies | {snap['production_dependency_count']}{extra} |")
        if snap.get("package_manager"):
            out.append(f"| Package manager | {snap['package_manager']} |")
        out.append("")
        # Key perf libraries summary
        perf_lib_rows = [
            ("`react-native-reanimated`", deps.get("reanimated_present")),
            ("`react-native-gesture-handler`", deps.get("gesture_handler_present")),
            ("`react-native-screens`", deps.get("screens_present")),
            ("`@shopify/flash-list`", deps.get("flash_list_present")),
            ("`expo-image`", deps.get("expo_image_present")),
        ]
        if any(present is not None for _name, present in perf_lib_rows):
            out.append("**Perf-relevant libraries:**")
            out.append("")
            out.append("| Library | Installed |")
            out.append("|---------|:---------:|")
            for name, present in perf_lib_rows:
                out.append(f"| {name} | {_yes_no(present)} |")
            out.append("")
        if deps.get("known_heavy_deps"):
            out.append(f"**Known-heavy dependencies present:** {', '.join('`' + d + '`' for d in deps['known_heavy_deps'])}")
            out.append("")

    # Codebase snapshot (NEW) — counts the analyzers observed, so the reader
    # sees what `0 React.memo` / `12 ScrollView` etc. actually look like in
    # this project.
    cs = report_json.get("codebase_snapshot") or {}
    if cs:
        out.append("### Codebase snapshot")
        out.append("")
        out.append("| Pattern | Count |")
        out.append("|---------|------:|")
        rows = [
            ("`React.memo` / `memo()` wrappers", "react_memo_usages"),
            ("`useMemo` calls", "use_memo_usages"),
            ("`useCallback` calls", "use_callback_usages"),
            ("`useEffect` calls (total)", "use_effect_calls"),
            ("`useEffect` calls with dependency array", "use_effect_with_deps"),
            ("`useEffect` calls with empty dependency array", "use_effect_empty_deps"),
            ("`<ScrollView>` usages", "scrollview_instances"),
            ("`<FlatList>` usages", "flatlist_instances"),
            ("`<SectionList>` usages", "sectionlist_instances"),
            ("`<FlashList>` usages", "flashlist_instances"),
            ("React Native `<Image>` usages", "rn_image_usages"),
            ("`expo-image` `<Image>` usages", "expo_image_usages"),
            ("`console.log` calls (production paths)", "console_log_calls"),
            ("`console.log` calls inside `__DEV__` guard", "console_log_dev_guarded"),
            ("`Animated` from `react-native` imports", "animated_rn_imports"),
            ("`react-native-reanimated` imports", "reanimated_imports"),
            ("Inline arrow `renderItem` usages", "inline_arrow_renderitems"),
            ("Inline-object JSX prop usages", "inline_object_jsx_props"),
        ]
        for label, key in rows:
            if key in cs:
                out.append(f"| {label} | {cs[key]} |")
        out.append("")

    # Coverage & limitations (NEW) — what was actually tested vs skipped.
    coverage = report_json.get("coverage") or []
    if coverage:
        out.append("### Coverage & limitations")
        out.append("")
        out.append("| Aspect | Status | Notes |")
        out.append("|--------|--------|-------|")
        for r in coverage:
            out.append(f"| {r.get('aspect','')} | {r.get('status','')} | {r.get('note','')} |")
        out.append("")

    # Severity counts
    out.append("### Severity counts")
    out.append("")
    out.append("| Priority | Count | When to fix |")
    out.append("|----------|-------|-------------|")
    out.append(f"| 🔴 CRITICAL | {sev.get('critical',0)} | Fix before launch |")
    out.append(f"| 🟡 HIGH | {sev.get('high',0)} | Fix as you scale |")
    out.append(f"| 🟢 LOW | {sev.get('low',0)} | Nice to have |")
    out.append("")

    # Per-category breakdown
    rows = report_json.get("per_category_rows") or []
    if rows:
        out.append("### Per-category breakdown")
        out.append("")
        out.append("| Category | Critical | High | Medium | Low | Score |")
        out.append("|----------|---------:|-----:|-------:|----:|------:|")
        for r in rows:
            out.append(
                f"| {r['category']} | {r['critical']} | {r['high']} | {r['medium']} | {r['low']} | {r['score']} |"
            )
        out.append("")

    # Top-N
    top = report_json.get("top_n_list") or []
    if top:
        out.append(f"### Top {len(top)} highest-impact findings")
        out.append("")
        for entry in top:
            out.append(f"{entry['rank']}. **{entry['title']}** — {entry['location']}")
        out.append("")

    # Metrics dashboard (Slice 3+). Suppressed when the richer per-metric
    # Lighthouse breakdown is present (rendered in the Device performance
    # section) to avoid two competing "Measured metrics" tables.
    metrics = report_json.get("metrics_dashboard") or []
    if metrics and not report_json.get("device_lighthouse"):
        out.append("### Measured metrics")
        out.append("")
        out.append("| Metric | Android | iOS | Threshold | Status |")
        out.append("|--------|--------:|----:|----------:|:------:|")
        for m in metrics:
            out.append(f"| {m.get('name','')} | {m.get('android','—')} | {m.get('ios','—')} | {m.get('threshold','—')} | {m.get('status','')} |")
        out.append("")
        out.append(prose.get("metrics_plainterms", default_prose("metrics_plainterms")))
        out.append("")

    out.append("---")
    out.append("")
    out.append("## Technical findings")
    out.append("")

    lf = report_json.get("lead_findings_by_priority", {}) or {}

    if lf.get("CRITICAL"):
        out.append("### CRITICAL — Fix before launch")
        out.append("")
        for f in lf["CRITICAL"]:
            out.append(render_finding_block(f, prose, f.get("count_label", ""), "CRITICAL"))

    if lf.get("HIGH"):
        out.append("### HIGH — Fix as you scale")
        out.append("")
        for f in lf["HIGH"]:
            out.append(render_finding_block(f, prose, f.get("count_label", ""), "HIGH"))

    if lf.get("LOW"):
        out.append("### LOW — Nice to have")
        out.append("")
        for f in lf["LOW"]:
            out.append(render_finding_block(f, prose, f.get("count_label", ""), "LOW"))

    # Working well
    ww = report_json.get("working_well_rows") or []
    if ww:
        out.append("### What's working well")
        out.append("")
        out.append("| Check | Status | Notes |")
        out.append("|-------|:------:|-------|")
        for r in ww:
            out.append(f"| {r['check']} | 🟢 {r['status']} | {r['notes']} |")
        out.append("")

    # Bundle composition (Slice 2+)
    bundle = report_json.get("bundle_table")
    if bundle:
        out.append("---")
        out.append("")
        out.append("## Bundle composition")
        out.append("")
        out.extend(_render_bundle_section(bundle))
        out.append("")

    # Pre-publish readiness (Stage 4e) — separate verdict, perf score untouched
    pub_verdict = report_json.get("publishing_verdict")
    store_table = report_json.get("store_readiness_table")
    if pub_verdict and store_table:
        out.append("---")
        out.append("")
        out.append("## Pre-publish readiness")
        out.append("")
        out.extend(_render_publishing_section(pub_verdict, store_table))

    # Device performance (Slice 3+)
    device = report_json.get("device_metrics_table")
    device_lh = report_json.get("device_lighthouse")
    device_lh_ios = report_json.get("device_lighthouse_ios")
    if device or device_lh or device_lh_ios:
        out.append("---")
        out.append("")
        out.append("## Device performance")
        out.append("")
        if device_lh:
            out.append("### Measured device metrics — Android")
            out.append("")
            out.extend(_render_device_lighthouse(device_lh))
        if device_lh_ios:
            out.append("### Measured device metrics — iOS (Simulator)")
            out.append("")
            out.extend(_render_device_lighthouse_ios(device_lh_ios))
        if device:
            if device_lh or device_lh_ios:
                out.append("### Per-run summary")
                out.append("")
            out.extend(_render_device_section(device))
            out.append("")

    # Remediation roadmap
    if any(lf.values()):
        out.append("---")
        out.append("")
        out.append("## Remediation roadmap")
        out.append("")
        for priority, header in (("CRITICAL", "Fix before launch (Critical)"), ("HIGH", "Fix as you scale (High)"), ("LOW", "Nice to have (Low)")):
            if not lf.get(priority):
                continue
            out.append(f"### {header}")
            out.append("")
            out.append("| # | Finding | Category | File · function |")
            out.append("|---|---------|----------|-----------------|")
            for i, f in enumerate(lf[priority], 1):
                loc = f.get("location") or {}
                out.append(f"| {i} | {f.get('title','')} | {f.get('category','')} | `{loc.get('file','?')} — {loc.get('function','?')}` |")
            out.append("")

    # Glossary (NEW) — port of references.md §4 so the report stands alone
    # without the reader needing to open the operator manual.
    out.append("---")
    out.append("")
    out.append("## Glossary")
    out.append("")
    out.extend(_render_glossary())
    out.append("")

    # Summary footer
    out.append("---")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append("| Metric | Value |")
    out.append("|--------|-------|")
    out.append(f"| Overall Score | {report_json.get('overall_score',0)} / 100 |")
    _types = report_json.get('total_issue_types', report_json.get('total_real_findings_count', 0))
    _sites = report_json.get('total_sites_count', _types)
    out.append(f"| Findings in This Report | {_types} issue type{'s' if _types != 1 else ''} across {_sites} site{'s' if _sites != 1 else ''} |")
    out.append(f"| CRITICAL / HIGH / LOW | {sev.get('critical',0)} / {sev.get('high',0)} / {sev.get('low',0)} |")
    out.append(f"| Highest-Impact Action | {prose.get('highest_impact_action', default_prose('highest_impact_action'))} |")
    out.append("")
    out.append("---")
    out.append(f"*Report generated by `mobile-perf-audit` pipeline · {report_json.get('audit_date','')}*")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render the final report markdown and emit to stdout.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    report_dir = audit_dir / "report"
    report_json_path = report_dir / "report.json"
    prose_fills_path = report_dir / "prose_fills.json"

    if not report_json_path.is_file():
        print(f"ERROR: report.json not found: {report_json_path}", file=sys.stderr)
        return 2

    report_json = load_json(report_json_path)
    prose: dict[str, str] = {}
    if prose_fills_path.is_file():
        try:
            data = load_json(prose_fills_path)
            if isinstance(data, dict):
                prose = data.get("prose_fills", data) or {}
        except Exception as e:
            print(f"WARN: could not load prose_fills.json ({e}); using defaults.", file=sys.stderr)
    else:
        print("(No prose_fills.json — rendering with deterministic stubs.)", file=sys.stderr)

    md = render(report_json, prose)
    out_md = report_dir / "report.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"wrote {out_md} ({len(md)} bytes)", file=sys.stderr)

    # Hard Mandate 5 — emit the full report content to stdout between fences.
    print("===MOBILE_PERF_AUDIT_REPORT_START===")
    print(md, end="" if md.endswith("\n") else "\n")
    print("===MOBILE_PERF_AUDIT_REPORT_END===")

    # Short operator summary
    sev = report_json.get("severity_totals", {}) or {}
    print("", file=sys.stderr)
    print("─── Operator summary ───", file=sys.stderr)
    print(f"Overall score: {report_json.get('overall_score',0)} / 100 ({report_json.get('verdict','')})", file=sys.stderr)
    print(f"Severity: CRITICAL={sev.get('critical',0)}  HIGH={sev.get('high',0)}  LOW={sev.get('low',0)}", file=sys.stderr)
    top = report_json.get("top_n_list") or []
    if top:
        print("Top findings:", file=sys.stderr)
        for entry in top[:3]:
            print(f"  {entry['rank']}. {entry['title']}  ({entry['location']})", file=sys.stderr)
    print(f"Artefacts (if persisted by host runner): {audit_dir}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
