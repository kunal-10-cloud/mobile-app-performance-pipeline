# `references.md` — Operator manual for the mobile-perf-audit pipeline

This file is the LLM's playbook for the mobile-perf-audit skill. `SKILL.md` defines **when** each step runs; this file defines **how** each analyzer hit is interpreted and **what evidence is required** for every claim that goes in the report. Load this file at the start of every audit and apply every rule.

The pipeline is LLM-agnostic. Any LLM that follows this file produces structurally equivalent output: the same evidence rows, the same facts, the same counts in the report. Prose voice may vary; substance may not.

Sections in order:
1. **Front-matter rules** — eight rules every claim in the report must satisfy.
2. **Universal verification protocol** — the procedure applied to every analyzer hit before it becomes a finding.
3. **Per-check sections** — one entry per rule the pipeline knows about (AST rules, ESLint plugin rules, bundle rules, Reassure rules, device-perf rules, tooling rules).
4. **Glossary** — terms used in the report.
5. **Output schemas** — JSON shape of evidence.json, facts.json, decisions.log inline so the LLM never has to look elsewhere.
6. **Report template** — the markdown skeleton with `{{slot}}` markers; the LLM fills only `<<PROSE>>` regions.
7. **Forbidden patterns** — phrasings the LLM must not produce.

---

## 1. Front-matter rules — apply to every claim in the report

These eight rules are non-negotiable. A draft sentence that would violate one is rewritten or dropped before it ships.

### 1.1 Citation format
- **Body prose cites `file:function` only.** Never `file:line` in the prose. Line numbers shift between commits; function names are more stable.
- **Line numbers belong in `evidence.json` only**, paired with a 3-line code snippet so a reviewer can verify the citation didn't drift.
- Example body sentence: "`app/feed/index.tsx — FeedList` renders without virtualization."

### 1.2 Count-derivation rule
- **Every count in the report equals `len(findings_by_rule[rule_id] where verdict == "REAL")`.** No round numbers, no "approximately N", no recalled numbers.
- The severity table, the top-N list, the per-finding summary line ("N inline arrow handlers across M files") — all derive from `evidence.json`'s `summary` object.
- If the count is zero, the finding is omitted entirely. No "0 sites found" entries.

### 1.3 Negative-claim rule
- **Any claim that asserts the absence of something must cite a `facts.json` field whose value is 0 / `false` / empty.** Phrases like "no components use React.memo", "Hermes is disabled", "the codebase has no <FlashList>" require a field in `audit_facts.json` whose value confirms absence.
- If the fact isn't in `facts.json`, the claim cannot be made until `gather_facts.py` is extended to gather it. Do not infer absences.
- Counterexample to avoid: "Add `expo-image` for caching" when `facts.dependencies.expo_image_present == true`. If `expo-image` is installed but `facts.source_pattern_counts.expo_image_usage_count == 0`, the correct framing is "Switch existing `<Image>` usages to `expo-image` (already installed)".

### 1.4 Stale-citation rule
- **Every citation must come from the current audit's evidence or current code reads.** Never reuse a citation from a previous audit, never carry over a function name from another codebase, never trust a path from prior context.
- Self-check before finalizing: confirm every cited identifier appears in the current `evidence.json`. If it doesn't, drop it.

### 1.5 Confidence-label rule (internal — evidence.json only)
- **Every Finding carries a `confidence` field** (`low` / `medium` / `high`) set by the worker that produced it. Static heuristics often medium; measured device issues high.
- `confidence` is part of the public ranking (it multiplies severity penalties; see `references/ranking_heuristics.md`). It is **not** surfaced as a per-finding label in the report — the ranking already reflects it.
- **`verdict`** (REAL / FP / UNCERTAIN) is INTERNAL only. Stamped by Pass A; consumed by synthesis to filter what enters the report. Never surfaces in any form.

### 1.6 Decision-log rule (internal — decisions.log only)
- **Every analyzer hit produces exactly one line in `decisions.log`**: `{rule_id} {file}:{line} {REAL|FP|UNCERTAIN} — {one-line reason}`.
- This log is for reviewers and reproducibility. **It is not referenced in the report.** Silent exclusions are forbidden in the log; absent meta-commentary is required in the report.

### 1.7 Single-pass-per-hit rule
- **Each analyzer hit is verified exactly once** during Pass A and recorded in `evidence.json`. The synthesis step reads `evidence.json` — it does not re-verify and does not re-classify.
- Re-running the audit re-derives everything from scratch. Do not carry state forward across runs.

### 1.8 Report-is-the-deliverable rule (most consequential)

**The report at `.audit-runs/{audit_id}/report/report.md` is the deliverable.** Internal pipeline state — `evidence.json`, `facts.json`, `decisions.log`, pass names, analyzer raw counts, FP-filter reasoning, verdict labels, JSON paths — exists so the LLM can produce the report accurately. **None of that scaffolding appears in the report itself.**

Concretely, the report must not contain:
- Counts of false positives or "filtered" / "excluded" hits ("analyzer raw count was 159; 10 false positives filtered"). Only the REAL count appears.
- Verified / heuristic / uncertain badges per finding. If a hit is too unclear to publish, it stays in `evidence.json` with `verdict: UNCERTAIN` and is omitted from the report.
- References to `evidence.json`, `facts.json`, `decisions.log`, "Pass A", "Pass C", or any file in `.audit-runs/{audit_id}/*` other than the report itself.
- JSON-path citations in prose (`facts.source_pattern_counts.react_memo_count = 0`, `evidence.PERF-006.real`). Use natural language: "no components use React.memo". The LLM verifies against facts.json privately; the reader sees the conclusion.
- A "Pass A uncertain hits to spot-check" section, an "Evidence files" footer link, an "internal verification method" note, or any other operational footnote.
- Mentions that the pipeline rebuilt counts from a deterministic source, that the LLM applied a verification protocol, or how the analyzer was reconciled with the codebase.

Counts in the report are still computed deterministically from `evidence.json` (Rule 1.2); negative claims still get verified against `facts.json` (Rule 1.3); citations still come from the current audit (Rule 1.4). All those rules apply silently behind a clean technical report.

---

## 2. Universal verification protocol

Apply this procedure to every analyzer hit emitted by any worker (static / bundle / reassure / device_android / device_ios) before stamping it with a verdict. The protocol is identical across checks; the per-check sections in §3 specify what to look for at each step.

```
INPUT  : one Finding emitted by a worker (matching finding.schema.json minus verdict / verification_method)
OUTPUT : the same Finding with verdict and verification_method populated, plus one line in decisions.log

STEP 1 — Read context around the cited location.
        For source-file findings: read N lines around evidence.line via the e1
        MCP execute_bash with `sed -n '{line-N},{line+5}p' {file}` (N varies
        by check; see §3). Confirm evidence.code_snippet matches what's on
        disk now (catches stale citations from edited files).

        For device findings: the "context" is the Maestro step label and its
        intervals data. No source read needed.

        For bundle findings: the "context" is the sourcemap mapping for the
        cited module. Confirm the module path resolves.

STEP 2 — Identify the enclosing function / scope.
        For source findings, walk backwards until a `function`, `const X =`,
        or `class X` declaration is found. Record the function name in
        evidence.function. If the cited line is module-level, record
        `<module>`.

STEP 3 — Confirm the check's required preconditions (see §3 per rule).
        Typical preconditions:
          - Receiver type matches (e.g. `<ScrollView>` not `<View>`)
          - Enclosing scope matches (e.g. callback is inside renderItem)
          - File belongs to source tree (not in node_modules, tests, examples)

STEP 4 — Cross-reference against facts.json where relevant.
        Example: a `static.scrollview_with_long_list` finding may be
        downgraded to UNCERTAIN if facts.dependencies.flash_list_present
        AND facts.source_pattern_counts.flashlist_count > 0 — the user
        clearly knows about virtualization libraries, so this site is
        more likely intentional.

STEP 5 — Emit verdict.
        REAL      : all preconditions satisfied; this is a real instance.
        FP        : a precondition is violated (wrong receiver, code shape
                    doesn't match the rule, etc.). Worker over-fired.
        UNCERTAIN : signals conflict (rule fires but facts suggest it may
                    be intentional, or context is incomplete).

STEP 6 — Append the verdict + a one-line `verification_method` string to
        the Finding object. Examples:
          "read 30 lines, confirmed <ScrollView> children include .map()"
          "FP: receiver is <FlashList>, not <ScrollView> — analyzer mis-fired"
          "UNCERTAIN: rule matched but flash_list_present in deps; site may be intentional"

STEP 7 — Append one line to decisions.log:
        "{rule_id} {file}:{line} {verdict} — {verification_method}"
```

UNCERTAIN findings stay in `evidence.json` (Rule 1.5) but are excluded from the report (Rule 1.8). Silent exclusion is forbidden in `decisions.log` — every analyzer hit must appear there.

---

## 3. Per-check sections

One entry per rule the pipeline knows about. Each entry follows the same template:

- **Source:** which worker emits findings under this rule.
- **What the rule matches:** the structural pattern the worker detects.
- **Verification protocol additions:** per-check tweaks to the §2 protocol (lines to read, facts to cross-reference, etc.).
- **Common false-positive shapes:** patterns that match the rule but aren't real instances.
- **Report framing:** how the finding is described in body prose.

Slice 1 ships with the rules below. Subsequent slices add more entries here as new workers come online.

### `static.scrollview_with_long_list`

- **Source:** `static_scan.py` custom AST rule.
- **What the rule matches:** A `<ScrollView>` JSX element whose children include a `{collection.map(...)}` expression where the collection cannot be statically bounded.
- **Verification protocol additions:**
  - Step 1: read 40 lines around the cited line.
  - Step 4: cross-reference `facts.dependencies.flash_list_present`. If true AND `facts.source_pattern_counts.flashlist_count > 0`, downgrade to UNCERTAIN — the user has FlashList available and is using it elsewhere; this site may be a deliberate exception.
- **Common false-positive shapes:**
  - `<ScrollView horizontal>` with a static `.map()` over a fixed array of ≤ 5 elements (e.g. tab bar) — FP.
  - `<ScrollView>` whose children include `.slice(0, N).map(...)` for literal N ≤ 10 — FP.
  - The map produces non-JSX (e.g. `arr.map(item => item.id)` used for keys) — FP (not actually rendering children).
- **Report framing:**
  - Body: "`{file} — {function}` renders an unbounded `.map()` inside `<ScrollView>`, which mounts every child eagerly."
  - Severity default: `high` if the iterated collection is fetched from an API; `medium` if it's a local prop.
  - Confidence: `high` if AST detected a clear unbounded collection; `medium` otherwise.

### `static.image_without_caching`

- **Source:** `static_scan.py` custom AST rule.
- **What the rule matches:** A JSX `<Image>` element whose `Image` identifier was imported from `'react-native'` (not from `'expo-image'`), and whose `source` prop contains a `{uri:` literal (i.e. a remote URL).
- **Verification protocol additions:**
  - Step 4: cross-reference `facts.dependencies.expo_image_present`. If `false`, severity stays `high` (no caching library available — every image redownloads). If `true` but `facts.source_pattern_counts.expo_image_usage_count == 0`, severity is `high` (lib installed but unused — easy win). If `true` and other sites use it, severity is `medium` (mixed usage; possibly intentional).
- **Common false-positive shapes:**
  - `<Image source={require('./local.png')}>` (local asset, no network) — FP.
  - `<Image>` rendered inside `<Animated.Image>` wrapper where caching is handled by the wrapper — FP.
  - Image imported from a custom abstraction that wraps `expo-image` internally — UNCERTAIN (needs human spot-check).
- **Report framing:**
  - Body: "`{file} — {function}` renders a remote image via React Native's `<Image>`, which lacks memory + disk caching. Repeat loads re-download."
  - Severity: per facts.json check above.
  - Confidence: `high` if AST clearly identified the import source; `medium` if the import path was re-exported through an index file.

### `static.inline_arrow_in_renderitem`

- **Source:** ESLint plugin `eslint-plugin-react-perf` (`react-perf/jsx-no-new-function-as-prop`) + `static_scan.py` for FlatList/SectionList/FlashList specifically.
- **What the rule matches:** A `renderItem={() => ...}` or `renderItem={(args) => ...}` prop on `<FlatList>`, `<SectionList>`, or `<FlashList>` — the arrow function is created on every parent render, defeating the list's memoization.
- **Verification protocol additions:**
  - Step 1: read 15 lines around the cited line.
  - Step 3: confirm the receiver tag is one of the three; ignore if it's a custom component named `renderItem` (different semantics).
- **Common false-positive shapes:**
  - `renderItem={myStableCallback}` where `myStableCallback` is declared outside the component body — FP (analyzer misfire on a `useCallback`-wrapped reference is also FP).
  - Tag is `<View>` with a `renderItem` prop forwarded to a child — FP.
- **Report framing:**
  - Body: "`{file} — {function}` passes an inline arrow as `renderItem`, so the list re-renders every row on every parent render."
  - Severity: `high` if the list is the screen's primary content; `medium` for secondary lists.
  - Confidence: `high` (AST-confirmed).

### `static.useeffect_no_deps`

- **Source:** ESLint plugin `eslint-plugin-react-hooks` (`react-hooks/exhaustive-deps`) + `static_scan.py` for the no-array case specifically.
- **What the rule matches:** A `useEffect(callback)` call with no dependency array argument — effect runs on every render.
- **Verification protocol additions:**
  - Step 1: read 20 lines around the cited line.
  - Step 3: confirm the call is actually `useEffect(...)`, not a custom hook with similar shape.
- **Common false-positive shapes:**
  - Effect intentionally runs every render (rare, but valid — e.g. logging current props for debugging). Usually accompanied by a comment; if present, downgrade to UNCERTAIN.
  - `useEffect(fn, deps)` where `deps` is computed elsewhere and spread via `...deps` — eslint-plugin-react-hooks handles this correctly; if our AST rule misfires, treat as FP.
- **Report framing:**
  - Body: "`{file} — {function}` calls `useEffect` without a dependency array; the callback runs on every render."
  - Severity: `medium` (a few per app are usually acceptable; widespread usage indicates a deeper pattern problem).
  - Confidence: `high`.

### `static.console_log_in_production_code`

- **Source:** `static_scan.py` custom AST rule.
- **What the rule matches:** A `console.log(...)` / `console.warn(...)` / `console.error(...)` call NOT inside `if (__DEV__) { ... }` block, in a file under `src/` / `app/` / `screens/` / `components/`.
- **Verification protocol additions:**
  - Step 2: also detect enclosing `if (__DEV__)` block via AST upward traversal.
  - Step 3: confirm file path is not in a test directory.
- **Common false-positive shapes:**
  - `console.error(...)` for genuine error reporting (production error logs) — UNCERTAIN. The convention "errors are okay, logs are not" is a judgment call.
  - Console call inside a `try/catch` for error logging — UNCERTAIN.
  - File is under `scripts/` or `tools/` — FP (these don't ship).
- **Report framing:**
  - Body: "`{file} — {function}` calls `console.log` outside a `__DEV__` guard. Each call serialises arguments and crosses the JSI bridge in production."
  - Severity: `low` (individual calls are cheap; flagged because they accumulate).
  - Confidence: `medium`.

### `static.animated_api_usage`

- **Source:** `static_scan.py` custom AST rule.
- **What the rule matches:** An `import { Animated, ... } from 'react-native'` statement, in a file that imports JSX (not a test file).
- **Verification protocol additions:**
  - Step 4: cross-reference `facts.dependencies.reanimated_present`. If `false`, severity stays `medium` and the recommendation is to install. If `true`, severity is `high` (Reanimated is available; mixing both is worse than picking one).
- **Common false-positive shapes:**
  - `Animated` imported but only used for `Animated.Value` shorthand without animation — UNCERTAIN.
  - File is a polyfill / shim — FP.
- **Report framing:**
  - Body: "`{file} — {function}` imports `Animated` from `react-native`, which runs animations on the JS thread."
  - Severity: per facts.json check above.
  - Confidence: `high` (import statements are unambiguous).

### `static.hermes_disabled`

- **Source:** `config_scan.py`, derived from `gather_facts.py`'s direct read of `app.json` / `app.config.{js,ts}`.
- **What the rule matches:** `facts.project_signature.hermes_enabled` is `false` (explicitly set to `jsc`) OR is `null` on SDK ≤ 49 where Hermes was opt-in.
- **Verification protocol additions:**
  - This is a facts-derived finding, not a worker hit. Pass A's verdict is fully determined by the facts.json field.
- **Common false-positive shapes:**
  - SDK ≥ 50 with `jsEngine` unset (Hermes is default) — FP.
- **Report framing:**
  - Body: "Hermes is disabled. JavaScriptCore is significantly slower at cold start than Hermes."
  - Severity: `critical` if `false` was set explicitly; `high` if unset on older SDK.
  - Confidence: `high` (config check).

### `static.new_architecture_disabled`

- **Source:** `config_scan.py`.
- **What the rule matches:** `facts.project_signature.new_architecture_enabled != true` on Expo SDK ≥ 51.
- **Verification protocol additions:**
  - Pass A re-reads the fact at verify time; if it now shows `true`, mark FP (stale config scan).
- **Common false-positive shapes:**
  - SDK < 51 (New Architecture isn't stable enough on older versions; not flagged).
  - User intentionally opted out due to known library incompatibility — UNCERTAIN if a comment in `app.json` says so (manual spot-check required; we cannot detect this automatically).
- **Report framing:**
  - Body: "The New Architecture (Fabric + TurboModules) is not enabled. On SDK ≥ 51 it is the recommended path and removes the JS↔native bridge for component updates."
  - Severity: `medium` (it's a perf upside, not a blocker).
  - Confidence: `high`.
  - Always add the caveat in Actionables: "Verify each third-party native module is New-Arch-compatible before enabling."

### `static.inline_object_props`

- **Source:** `static_scan.py` custom AST rule, supplemented by ESLint's `react-perf/jsx-no-new-object-as-prop` for the cases the AST rule misses.
- **What the rule matches:** A JSX attribute whose value is a literal `{...}` (`ObjectExpression`). E.g. `style={{ flex: 1 }}`, `contentContainerStyle={{ paddingTop: 16 }}`.
- **Verification protocol additions:**
  - Step 1: read 8 lines around the cited line.
  - Step 4: no facts cross-reference — the rule is structural.
- **Common false-positive shapes:**
  - Object literal that's intentionally re-created (e.g. a layout style that genuinely depends on prop-derived values that change every render) — UNCERTAIN, judgment call.
  - Object literal whose parent component is a leaf with no memoized children — REAL, but lower severity than the same pattern on a heavy subtree.
- **Report framing:**
  - Body: "`{file} — {function}` passes a fresh inline object as the `{prop_name}` prop, breaking shallow-equality memoization in any memoized child."
  - Severity: `medium` for `style` / `contentContainerStyle` (often passed to memoized RN primitives); `low` for arbitrary props.
  - Confidence: `high`.

### `static.large_unmemoized_component`

- **Source:** `static_scan.py` custom AST rule.
- **What the rule matches:** A function component (PascalCase name, body contains JSX) longer than 100 lines whose export is NOT wrapped in `React.memo` / `memo(...)`.
- **Verification protocol additions:**
  - Step 4: cross-reference `facts.source_pattern_counts.react_memo_count`. If `> 0` (author uses memo elsewhere), downgrade to UNCERTAIN — the omission may be deliberate.
- **Common false-positive shapes:**
  - Top-level layout/route component that never receives prop updates anyway (its parent is the router) — UNCERTAIN.
  - Component already wrapped at the import site by a HOC that includes memoization (`connect(state)(Component)`, `withSafeArea(Component)` for some HOCs) — UNCERTAIN.
- **Report framing:**
  - Body: "`{file} — {function}` is {lines} lines and is not memoized; any parent re-render reconciles the entire subtree."
  - Severity: `medium`.
  - Confidence: `medium`.

### `bundle.bundle_too_large_warning` / `bundle.bundle_too_large_critical`

- **Source:** `bundle_scan.py` — direct measurement of bytes under `_expo/static/js/{platform}/` after `expo export`.
- **What the rule matches:** Total platform JS bundle exceeds 2 MiB (warning) or 4 MiB (critical).
- **Verification protocol additions:**
  - Step 1 ("read N lines") does not apply — context is the byte measurement itself. Pass A re-checks `evidence.metric_value >= evidence.metric_threshold`; if not, FP (stale measurement, e.g. someone trimmed the bundle and re-ran without re-bundling).
- **Common false-positive shapes:**
  - Bundle includes asset registry strings (Expo encodes asset references into the JS bundle); for asset-heavy apps the 2 MiB threshold is easily hit without it being a real problem. Operator may downgrade in writeup.
- **Report framing:**
  - Body: "The {platform} JS bundle is {size} ({over_pct}% over the {threshold} budget)."
  - Severity: deterministic, per threshold.
  - Confidence: `high`.

### `bundle.dependency_oversized`

- **Source:** `bundle_scan.py` via `source-map-explorer`.
- **What the rule matches:** A single npm package contributes ≥ 100 KiB to the bundle.
- **Verification protocol additions:**
  - Step 4: cross-reference `facts.dependencies.known_heavy_deps`. If the package is listed there, severity stays as emitted; the report framing should also mention the recommended alternative from §3's `bundle.known_bloated_dependency` table.
- **Common false-positive shapes:**
  - `@babel/runtime` (auto-included by Babel; cannot be removed) — keep as `info`-only finding, do not surface as actionable.
  - `react`, `react-native` (mandatory) — never emit findings for these. The bundle scanner already excludes them.
- **Report framing:**
  - Body: "`{pkg}` contributes {bytes} to the bundle."
  - Severity: `medium` < 250 KiB; `high` ≥ 250 KiB.
  - Confidence: `high`.

### `bundle.known_bloated_dependency`

- **Source:** `bundle_scan.py`.
- **What the rule matches:** Bundle contains a package on a curated list of known-heavy libraries with established lighter alternatives (Moment, full lodash, axios, RxJS, jQuery, etc.).
- **Verification protocol additions:**
  - Severity comes from the curated map, not from byte count. The bundle scanner emits the recommendation text verbatim.
- **Common false-positive shapes:**
  - Library used because a transitive dep needs it (e.g. moment because `react-native-calendars` depends on it). Operator may downgrade — but should still note the cost in the report.
- **Report framing:**
  - Body: explicit, includes the suggested alternative.
  - Severity: from the curated table.
  - Confidence: `high`.

### `bundle.duplicate_dependency_libs`

- **Source:** `bundle_scan.py`.
- **What the rule matches:** Two packages with overlapping purpose installed simultaneously (`lodash` + `lodash-es`, `moment` + `dayjs`, etc.).
- **Verification protocol additions:** None.
- **Common false-positive shapes:**
  - Two icon libraries because the project uses one set from each — UNCERTAIN; converging is still typically recommended.
- **Report framing:**
  - Body: "Two libraries with overlapping APIs are installed: {a} and {b}. {why}"
  - Severity: `medium`.
  - Confidence: `high`.

### `bundle.asset_image_too_large` / `bundle.png_image_could_be_webp`

- **Source:** `bundle_scan.py` asset walk over `workspace/assets/`.
- **What the rule matches:** Image > 500 KiB (asset_image_too_large); PNG > 100 KiB (png_image_could_be_webp).
- **Verification protocol additions:**
  - Pass A re-stats the asset; if it shrank below threshold (or no longer exists), mark FP.
- **Common false-positive shapes:**
  - Splash/launch image that genuinely needs full resolution — UNCERTAIN.
  - Image used as PDF/print export, not in-app rendering — FP if the operator knows.
- **Report framing:**
  - Body: include the asset path, its size, and the resize/re-encode action.
  - Severity: `high` ≥ 1 MiB, `medium` 500 KiB–1 MiB, `low` for WebP candidates.
  - Confidence: `high` for size, `medium` for WebP-savings estimate.

### `bundle.asset_total_too_large`

- **Source:** `bundle_scan.py`.
- **What the rule matches:** Total non-image assets (fonts, JSON, video, audio under `assets/`) exceed 5 MiB.
- **Verification protocol additions:** None.
- **Report framing:**
  - Body: include the total and break down by extension type when possible.
  - Severity: `medium`.

### `reassure.excessive_render_count`

- **Source:** `transform_reassure.py` over Reassure JSON.
- **What the rule matches:** Mean render count per state change > 5 for a measured screen.
- **Verification protocol additions:**
  - Pass A re-checks `metric_value >= metric_threshold`; if Reassure was re-run and the value dropped, mark FP.
- **Common false-positive shapes:**
  - First-mount renders (mount + initial layout) on simple screens can register as 2–4 renders; threshold of 5 deliberately ignores those.
  - Test environment artefacts (mocked navigation that re-renders extra times) — UNCERTAIN; recommend a hand-written test if the same screen registers high on multiple runs.
- **Report framing:**
  - Body: "`{component}` renders {N} times per state change. {N - 1} of those are wasted reconciliation."
  - Severity: from `transform_reassure.py` per the count thresholds.
  - Confidence: `high`.

### `reassure.excessive_render_duration`

- **Source:** `transform_reassure.py`.
- **What the rule matches:** Mean render duration > 16 ms (one 60-Hz frame) for a measured screen.
- **Verification protocol additions:** Same as render-count; re-check measurement.
- **Common false-positive shapes:**
  - Reassure runs under jest-expo, which is slower than the real device. A 20 ms measurement may translate to < 16 ms on a real phone with Hermes. Operator may downgrade for borderline cases.
- **Report framing:**
  - Body: "`{component}` takes {ms} ms per render — exceeds the {threshold} ms frame budget."
  - Severity: per the duration thresholds.
  - Confidence: `high`.

### `device.fps_below_threshold`

- **Source:** `transform_device_metrics.py` over `results/android.json` or `results/ios.json` (iOS today does NOT measure FPS — this rule fires Android-only until iOS gains parity).
- **What the rule matches:** Mean FPS across iterations is below 55 (medium), 45 (high), or 30 (critical) on the configured emulator profile.
- **Verification protocol additions:**
  - This is a measurement-derived rule. Pass A re-checks `metric_value < metric_threshold`; FP only when a later run replaced the measurement with a higher value.
- **Common false-positive shapes:**
  - Maestro flow happened to include heavy synthetic interactions (e.g. fast successive scrolls) that don't represent real user pacing. Operator may downgrade.
  - Cold-start was captured inside the iteration window, pulling FPS down — should be a separate finding (`device.startup_too_slow`), not a runtime-jank one. Re-check by reading per-interval data in the same `results/{platform}.json`.
- **Report framing:**
  - Body: "{Platform} sustained an average of {fps} FPS on {device_profile}, below the {threshold} FPS smooth-motion floor."
  - Severity: from the threshold ladder.
  - Confidence: `high`.

### `device.startup_too_slow`

- **Source:** `transform_device_metrics.py`.
- **What the rule matches:** Cold-start time exceeds 1.5 s (medium), 2.5 s (high), or 4 s (critical).
- **Verification protocol additions:**
  - Cross-reference `facts.project_signature.hermes_enabled`. If `false`, increase confidence in the recommendation to enable Hermes — but the finding itself stays REAL regardless.
- **Common false-positive shapes:**
  - First-ever launch after install always includes one-time Hermes precompile cost (Android) or codesign verification (iOS). Re-run on a second iteration; if the second run's startup is significantly lower, downgrade severity.
- **Report framing:**
  - Body: "Cold start measured at {ms} ms on {device_profile}."
  - Severity: per the threshold ladder.
  - Confidence: `high`.

### `device.memory_growth_suspected_leak`

- **Source:** `transform_device_metrics.py`.
- **What the rule matches:** A single iteration ended with memory > 10 MiB (medium) or > 30 MiB (high) above its starting baseline.
- **Verification protocol additions:**
  - When growth is observed in only one iteration but not subsequent ones (e.g. iteration 0 only), downgrade to UNCERTAIN — likely one-time cache fill, not a leak.
- **Common false-positive shapes:**
  - Image-heavy screens that warm a caching library (expo-image, FastImage) on first access. Growth stabilises on iteration 2+.
  - Background queue / analytics buffer that flushes at intervals — looks like growth between flushes.
- **Report framing:**
  - Body: "{Platform} iteration {N} ended {MB} MB above baseline."
  - Severity: per the growth ladder.
  - Confidence: `high`.

### `device.cpu_thread_saturated`

- **Source:** `transform_device_metrics.py` (Android only; iOS doesn't capture per-thread CPU in v1).
- **What the rule matches:** A named thread averaged > 70% (medium) or > 90% (high) CPU during an iteration.
- **Verification protocol additions:**
  - The thread name is preserved in `evidence.function`. Pass A treats `mqt_js`-style names as authoritative (JS thread on RN); UI thread saturation is a different mitigation than JS thread saturation.
- **Common false-positive shapes:**
  - Short bursts above threshold averaged over a longer iteration window can read low. Conversely, single long bursts (CPU spike during startup) can dominate. Cross-check with `device.long_blocking_interval`.
- **Report framing:**
  - Body: "{Platform} thread `{thread}` averaged {pct}% CPU during the measured flow."
  - Severity: per the percentage threshold.
  - Confidence: `high`.

### `device.long_blocking_interval`

- **Source:** `transform_device_metrics.py` (Android only).
- **What the rule matches:** Any single main-thread blocking interval > 500 ms (medium) or > 1000 ms (high) anywhere in the flow.
- **Verification protocol additions:**
  - Cross-reference per-step intervals to anchor the freeze to a specific Maestro step.
- **Common false-positive shapes:**
  - Splash-screen handoff to JS thread can register as one large blocking interval on first launch. Treat as UNCERTAIN if the only large interval is at the start of iteration 0.
- **Report framing:**
  - Body: "{Platform} main-thread froze for {ms} ms during the flow."
  - Severity: per the duration ladder.
  - Confidence: `high`.

### `device.step_fps_dipped`

- **Source:** `transform_device_metrics.py` per-interval data.
- **What the rule matches:** During a single Maestro step, minimum FPS fell below 55 (medium), 45 (high), or 30 (critical).
- **Verification protocol additions:**
  - Step-level findings often correlate to a single screen. Cross-reference with static-layer findings for the same screen file before writing the report — the dedupe in `synthesize.py` will collapse them when the (file, function) match.
- **Common false-positive shapes:**
  - Maestro's tap-then-screenshot timing can include the keyboard-dismiss animation; the FPS dip there isn't user-perceived as jank. Downgrade if the step is a `take_screenshot` or `back`.
- **Report framing:**
  - Body: "{Platform}: during step `{step_label}`, minimum FPS dropped to {fps}."
  - Severity: per the FPS ladder.
  - Confidence: `high`.

---

### Stage 4f — backend / DB / algorithm rules

The rules below are ported verbatim (semantics + detection patterns) from the web pipeline at `c:\Users\adity\Desktop\audit\perf-audit\perf_audit.py`. They run against backend Python (FastAPI / Motor / Pydantic) and JS that the operator ingests under `workspace/backend/` (Stage 2 allowlist already covers `backend/`, `server/`, `api/`). If no backend tree is ingested, Stage 4f emits one `tooling.backend_source_missing` finding and exits clean — the rest of the pipeline is unaffected.

Findings from this stage contribute to the perf score via three categories (`backend_perf`, `database`, `algorithms`) with weights summing to ~0.30 of the overall total — backend perf is load-bearing because it gates every request.

### `backend.sync_route_handler`

- **Source:** `configs/backend_rules.py` — Python AST walk for `ast.FunctionDef` (sync) nodes with a FastAPI route decorator.
- **What the rule matches:** A function declared with `def` (not `async def`) that has any of `@app.get` / `.post` / `.put` / `.delete` / `.patch` as a decorator. FastAPI runs sync handlers in a thread pool; under concurrency the pool becomes the bottleneck.
- **Verification protocol additions:**
  - Step 1: read 20 lines around the cited handler.
  - Step 4: cross-reference `facts.backend.async_handler_count` — if it's near zero across the whole codebase, the team may be intentionally sync; downgrade severity to MEDIUM but keep REAL.
- **Common false-positive shapes:**
  - Purely CPU-bound handlers (config readers, in-memory lookups) — UNCERTAIN. Sync is fine when the work is < 1 ms and doesn't block on I/O.
  - Routes that intentionally call sync libraries via `await asyncio.to_thread(...)` — FP if the call is the only body.
- **Report framing:**
  - Body: "Route handler `{name}` uses `def` instead of `async def` — blocks the thread pool under concurrency."
  - Severity: `high` if name contains `get|list|search|auth|login|fetch` (hot paths); else `medium`.
  - Confidence: `high`.

### `backend.n_plus_one_query`

- **Source:** `configs/backend_rules.py` — line-by-line scanner; tracks `for`/`while`/`async for` indent depth, flags any `.find(` / `.find_one(` / `.aggregate(` / `.count_documents(` / `.distinct(` call while inside.
- **What the rule matches:** A Mongo query call inside a loop body. Each iteration makes a separate round-trip; latency scales linearly with N.
- **Verification protocol additions:**
  - Step 1: read 30 lines centered on the cited line so the loop header is in scope.
  - Step 4: if the loop body is itself a list comprehension *building* the IDs for a future batch query, the call is intentional — downgrade to UNCERTAIN.
- **Common false-positive shapes:**
  - `.find_one(...)` inside a loop where the call is bounded to a small fixed N (e.g. iterating over a tuple of 2-3 known keys) — FP.
  - Test fixtures / migration scripts not in the request path — UNCERTAIN (still worth flagging).
- **Report framing:**
  - Body: "MongoDB query inside loop in `{fn_name}` — each iteration is a separate round-trip; batch with `$in`."
  - Severity: `high`.
  - Confidence: `high` for direct `db.coll.X(...)` shape; `medium` if the receiver is opaque.

### `backend.unbounded_query`

- **Source:** `configs/backend_rules.py` — regex pairs: any `.find(...)` whose forward 3-line window has no `.limit(N)` / `.to_list(N)` / `limit=` marker.
- **What the rule matches:** A `.find()` call that returns ALL matching documents — either via `.to_list()` / `.to_list(None)` / `.to_list(length=None)` or via unbounded `async for` iteration.
- **Verification protocol additions:**
  - Step 1: read 10 lines after the cited line — the bound may be further down a chain.
  - Step 4: if the surrounding function name contains `migrate|backfill|export|admin|cron`, downgrade to UNCERTAIN — admin paths legitimately need all rows.
- **Common false-positive shapes:**
  - `.find({...}).limit(...).to_list(...)` chain split across multiple lines beyond the 3-line window — FP.
  - `db.coll.find({}, {"_id": 1}).to_list(None)` returning ID-only projection for batched index work — UNCERTAIN.
- **Report framing:**
  - Body: "Unbounded query in `{fn_name}` — response size grows with the collection."
  - Severity: `high`.
  - Confidence: `high`.

### `backend.mongo_client_not_singleton`

- **Source:** `configs/backend_rules.py` — Python AST: for each function with a route decorator, scan the body source segment for `MongoClient(...)` / `AsyncIOMotorClient(...)` instantiation.
- **What the rule matches:** A Mongo client constructor called **inside a route handler** (instead of at module scope or a startup hook). Every request opens a fresh connection pool.
- **Verification protocol additions:**
  - Step 1: no source spot-check beyond confirming the handler is still in the file. Detection is structural.
- **Common false-positive shapes:**
  - Lazy initialisation wrapped in a module-level `_client = None` then `_client = _client or AsyncIOMotorClient(...)` — UNCERTAIN if the assignment is to a module global, FP if encapsulated in a `Depends()`.
- **Report framing:**
  - Body: "`MongoClient` / `AsyncIOMotorClient` instantiated inside route handler `{name}`."
  - Severity: `critical` (causes connection exhaustion under any concurrency).
  - Confidence: `high`.

### `database.missing_index`

- **Source:** `configs/backend_rules.py` — for each file, collect: (a) field names appearing as keys in `.find({...})` filters; (b) field names passed to `create_index('...')`. Flag the set difference (queried - indexed).
- **What the rule matches:** Fields used in `.find()` filters with no matching `create_index(...)` call anywhere in the same file. File-local — does NOT cross-reference indexes declared in a different `init_indexes.py`, so this can over-flag when index registration is centralised.
- **Verification protocol additions:**
  - Step 4: cross-reference `facts.backend.indexed_collections` (gathered file-wide). If the missing field IS indexed elsewhere, downgrade to FP and skip.
- **Common false-positive shapes:**
  - Operator keys (`$in`, `$or`, `$and`, `$gt`, `$lt`, `$gte`, `$lte`, `$ne`, `$exists`) — already excluded by the rule.
  - `_id` queries — auto-indexed, already excluded.
  - Indexes declared in a separate `migrations/` or `init_indexes.py` file — FP, mitigated by the facts cross-ref.
- **Report framing:**
  - Body: "Fields `{a, b, c}` are queried in `{file}` but no `create_index` call exists for them in the same file."
  - Severity: `high`.
  - Confidence: `medium` (file-local detection is intentionally narrow; verify before acting).

### `backend.sequential_await_chain`

- **Source:** `configs/backend_rules.py` — line scanner that accumulates consecutive `await ` / `= await ` lines and flags when ≥3 in a row, distinct assigned variables, no inter-await reference.
- **What the rule matches:** Three or more sequential `await` statements at the same scope that assign distinct variables, suggesting they're independent and could parallelise via `asyncio.gather`.
- **Verification protocol additions:**
  - Step 1: read 10 lines around the cited block.
  - Step 4: if any later await line uses a variable from an earlier one, this is FP (true dependency, not parallelisable).
- **Common false-positive shapes:**
  - `a = await x(); b = await y(a)` — sequential because `y` needs `a`; FP.
  - `await db.coll.insert_one(...); await db.coll.update_one(...)` — sequential due to ordering semantics; FP.
  - Awaits that share a rate limit with the same provider; flagging is correct but the fix needs care.
- **Report framing:**
  - Body: "{N} sequential `await`s in `{fn_name}` — likely `asyncio.gather` candidate."
  - Severity: `medium`.
  - Confidence: `medium` (independence is heuristic).

### `backend.blocking_work_in_handler`

- **Source:** `configs/backend_rules.py` — for each route handler, scan the function source for any of these tokens (case-insensitive): `send_email|send_mail|smtp|sendgrid|ses\.send|resend\.`; `send_notification|push_notification|fcm\.|firebase_admin\.messaging`; `webhook|httpx\.post|requests\.post|aiohttp.*post`; `analytics|track_event|segment\.`; `stripe\.|paypal\.|razorpay\.`. Skip if the body references `BackgroundTasks` or `background_task`.
- **What the rule matches:** A route handler that calls an external service (email / push / webhook / payment / analytics) inline before returning — adds the provider's latency to every request.
- **Verification protocol additions:**
  - Step 1: read 30 lines around the route handler signature.
  - Step 4: confirm the matched call is actually invoked (not in a comment, not behind `if False`).
- **Common false-positive shapes:**
  - Comments containing the word `BackgroundTasks` — historical legacy bug. The rule's exclusion checks `if "BackgroundTasks" in func_src`; a comment like `# move to BackgroundTasks later` will suppress detection. Worth a manual re-grep if you suspect a miss.
  - Stripe webhook **receiver** endpoints (they MUST be sync to return 200 fast) — FP; these are correct as-is.
- **Report framing:**
  - Body: "Route `{name}` calls external service inline — move to `BackgroundTasks` or a job queue."
  - Severity: `high`.
  - Confidence: `high`.

### `backend.no_projection_on_query`

- **Source:** `configs/backend_rules.py` — regex `\.find\(\s*\{[^}]*\}\s*\)` (single-argument find with object filter).
- **What the rule matches:** `.find(filter)` with no projection second argument — MongoDB returns every field of every matching document. Marginal at small document sizes, meaningful when documents are large.
- **Verification protocol additions:**
  - Step 1: read 5 lines around the cited line.
  - Step 4: capped at 10 findings per audit to keep the report scannable; the remainder are deliberately not surfaced.
- **Common false-positive shapes:**
  - Aggregations that legitimately need the whole document (audit logs, full-record export) — FP.
  - Models that store < 10 fields — FP (the bandwidth win is negligible).
- **Report framing:**
  - Body: "`.find(filter)` without projection — returns every field. Marginal; capped at 10."
  - Severity: `low`.
  - Confidence: `medium`.

### `backend.pydantic_complex_model`

- **Source:** `configs/backend_rules.py` — Python AST: for each `class X(BaseModel)`, count `AnnAssign` fields whose annotation dump contains `List` or `Optional`. Flag when count ≥ 3.
- **What the rule matches:** Pydantic models with ≥3 nested / optional fields. On v1, deep validation runs per request and can show up on flame graphs; v2 (Rust-backed) is rarely a bottleneck.
- **Verification protocol additions:**
  - Step 4: cross-reference `facts.backend.pydantic_major_version` — if v2, downgrade to INFO. If v1 and the model is used by a hot endpoint, keep at LOW.
- **Common false-positive shapes:**
  - Models defined for one-off batch jobs / migrations — UNCERTAIN, not on the request path.
  - Models used only for response serialization (output) — slightly less critical than request validation.
- **Report framing:**
  - Body: "`{model_name}` has {N} nested/optional fields. Profile before refactoring."
  - Severity: `low` (informational).
  - Confidence: `medium`.

### `algorithms.nested_iteration`

- **Source:** `configs/backend_rules.py` — line-level regex: `\.filter\(.*\.filter\(`, `\.find\(.*\.find\(`, `\.some\(.*\.some\(`, `\.includes\(.*\.includes\(`, `for\s.*for\s.*\.includes\(`. Runs across both backend `.py` AND backend `.js/.ts`.
- **What the rule matches:** Nested array operations (filter inside filter, etc.) — O(n²) complexity. Performance degrades quadratically with data size.
- **Verification protocol additions:**
  - Step 1: read 10 lines around the cited line so the data sources are visible.
  - Step 4: if both iterations are over fixed-size arrays (≤ 10 elements provably from a literal), downgrade to FP.
- **Common false-positive shapes:**
  - `.filter(...).filter(...)` chain on the SAME array (sequential filters, not nested) — FP.
  - One iteration over a small constant array embedded in the other — UNCERTAIN.
- **Report framing:**
  - Body: "Nested iteration pattern at `{file}:{line}` — O(n²) shape; convert the inner lookup to a `Set`."
  - Severity: `medium`.
  - Confidence: `medium`.

### `algorithms.linear_array_lookup_in_loop`

- **Source:** `configs/backend_rules.py` — line scanner: a `for ` / `for(` / `.forEach(` / `.map(` / `.filter(` line opens a "loop scope"; while inside, any `.includes(` / `.indexOf(` / `.find(` call flags. Capped at 10. Runs across `.py` and `.js/.ts`.
- **What the rule matches:** `.includes()` / `.indexOf()` / `.find()` called on an array inside a loop — O(n×m) where converting the lookup array to a `Set`/`dict` would make it O(n+m).
- **Verification protocol additions:**
  - Step 1: read 10 lines around the cited line.
  - Step 4: confirm the receiver of `.includes()` is provably a list/array. If it's a string, the optimisation is identical but smaller; if it's a `Set`/`Map`, this is already O(1) and FP.
- **Common false-positive shapes:**
  - `str.includes(substr)` — FP (substring search, not membership).
  - `set.has(...)` — already O(1); FP.
  - The legacy rule requires the loop construct at line start (`^\s*(for\s|for\(|.forEach\(|.map\(|.filter\()`), so inline chained patterns like `items.map(i => arr.includes(i))` are NOT detected.
- **Report framing:**
  - Body: "`{op}` inside loop at `{file}:{line}` — O(n×m); convert the lookup array to a `Set`."
  - Severity: `low`.
  - Confidence: `medium`.

### `backend.sequential_fetch_chain`

- **Source:** `configs/backend_rules.py` — line scanner accumulating consecutive `await fetch(...)` / `await axios.X(...)` calls; flags at ≥ 2 in a row.
- **What the rule matches:** Sequential `await fetch` / `await axios` calls that could parallelise via `Promise.all`. JS variant of `backend.sequential_await_chain` (so it runs over `.js/.ts` files only).
- **Verification protocol additions:**
  - Step 4: same as sequential_await_chain — confirm independence before recommending `Promise.all`.
- **Common false-positive shapes:**
  - The second fetch consumes the first's response — true dependency; FP.
  - Both fetches target the same upstream with strict rate limiting — flagging is correct but parallelisation may trip throttles.
- **Report framing:**
  - Body: "{N} sequential `await fetch/axios` calls — Promise.all candidate."
  - Severity: `medium`.
  - Confidence: `medium`.

---

## 4. Glossary

Terms used in the report. Both the LLM and the reader must read them the same way.

- **Cold start** — time from app launch to first interactive frame. Dominated by JS bundle parse + module evaluation on first run.
- **Frame budget** — 16.6 ms on a 60 Hz display. A render taking longer than one frame budget drops a frame.
- **Hermes** — Meta's JS engine optimised for React Native. Reduces cold start by 30–50% vs JavaScriptCore through bytecode precompilation.
- **JSI bridge** — the boundary between JS and native code. Calls across the bridge are expensive; minimising them is a perf goal.
- **N+1 render** — a parent component re-rendering causes children to re-render even though their props didn't change (e.g. inline arrow handlers as props).
- **Render duration** — wall-clock time the React reconciler spends rendering a component subtree on a state change.
- **TBT (Total Blocking Time)** — sum of main-thread blocking intervals > 50 ms during a Maestro flow. Each blocking interval is a frame the user sees as frozen.
- **FPS** — frames per second sustained during animation / scrolling. Below 50 fps is perceptibly janky.
- **Memory pressure** — sustained or growing heap usage during a flow. Growth across iterations indicates a leak.
- **Virtualization** — rendering only the on-screen rows of a long list (`FlatList` / `FlashList`) instead of mounting all rows up front.
- **Reanimated** — `react-native-reanimated`, the preferred animation library. Runs animations on the UI thread (native), not the JS thread.
- **FlashList** — `@shopify/flash-list`, a high-performance replacement for `FlatList` with better memory characteristics for long lists.
- **expo-image** — Expo's image component with built-in memory + disk caching; preferred over React Native's `<Image>` for remote URIs.
- **New Architecture (Fabric + TurboModules)** — RN's rewrite that removes the bridge, enables synchronous JS↔native calls. Significantly improves render perf when enabled (SDK 51+).
- **Verdict** (internal) — Pass A's REAL / FP / UNCERTAIN classification. Used to filter what enters the report; never surfaces.
- **Confidence** — the worker's assessment of how likely this is a real issue (low / medium / high). Multiplies severity in the score formula.
- **Pinned fact** — a value in `audit_facts.json` gathered deterministically (AST query or JSON parse). Used to ground negative claims so they don't depend on LLM inference.

---

## 5. Output schemas

The pipeline writes three machine-readable artefacts per audit. Their authoritative JSON Schemas live in `schemas/`. The shapes are summarised inline here so the LLM never has to look elsewhere.

### 5.1 `.audit-runs/{audit_id}/evidence/evidence.json`

See `schemas/evidence.schema.json` for the authoritative contract. In brief:

```json
{
  "audit_id": "<uuid>",
  "pass_a_completed_at": "<ISO-8601>",
  "findings_by_rule": {
    "static.scrollview_with_long_list": [
      {
        "id": "static.scrollview_with_long_list",
        "layer": "static",
        "category": "runtime_jank",
        "severity": "high",
        "confidence": "high",
        "title": "...",
        "description": "...",
        "evidence": {
          "file": "app/feed/index.tsx",
          "function": "FeedList",
          "line": 42,
          "code_snippet": "..."
        },
        "verdict": "REAL",
        "verification_method": "read 40 lines, confirmed ScrollView children include .map() over fetched feed"
      }
    ]
  },
  "summary": {
    "static.scrollview_with_long_list": {
      "total": 3, "real": 2, "fp": 1, "uncertain": 0, "distinct_files": 2
    }
  },
  "category_counts": {
    "runtime_jank": { "critical": 0, "high": 2, "medium": 1, "low": 0, "info": 0 }
  }
}
```

### 5.2 `.audit-runs/{audit_id}/facts/audit_facts.json`

See `schemas/facts.schema.json`. In brief:

```json
{
  "audit_id": "<uuid>",
  "facts_gathered_at": "<ISO-8601>",
  "workspace_root": "<abs path>",
  "project_signature": {
    "expo_sdk_version": "51",
    "hermes_enabled": true,
    "new_architecture_enabled": false,
    "typescript_present": true,
    "expo_router_present": true
  },
  "dependencies": {
    "production_count": 47,
    "expo_image_present": true,
    "flash_list_present": false,
    "reanimated_present": true,
    "known_heavy_deps": ["moment"]
  },
  "source_pattern_counts": {
    "react_memo_count": 0,
    "flatlist_count": 4,
    "flashlist_count": 0,
    "scrollview_count": 12,
    "rn_image_usage_count": 8,
    "expo_image_usage_count": 0,
    "console_log_count": 31,
    "console_log_dev_guarded_count": 2,
    "use_effect_count": 22,
    "use_effect_with_deps_count": 18,
    "inline_arrow_renderitem_count": 5
  },
  "assets": {
    "image_asset_count": 42,
    "image_asset_total_bytes": 8421337,
    "images_over_500kb_count": 3,
    "png_count": 28,
    "webp_count": 0
  },
  "presence": { "...": "..." },
  "tooling_status": { "...": "..." }
}
```

### 5.3 `.audit-runs/{audit_id}/decisions.log`

Plain text, one line per analyzer hit:

```
static.scrollview_with_long_list app/feed/index.tsx:42 REAL — read 40 lines, confirmed ScrollView children include .map() over fetched feed
static.scrollview_with_long_list app/profile/header.tsx:18 FP — children include .slice(0, 3).map(...), bounded by design
static.image_without_caching app/profile/avatar.tsx:7 REAL — Image from 'react-native', source uri prop, expo_image_present=true but unused here
```

Reviewers read this when they want to know why a hit was kept or dropped.

### 5.4 `.audit-runs/{audit_id}/report/report.md` — the final report

Generated from §6's template by filling `{{slot}}` markers from `evidence.json` and `audit_facts.json`. The LLM writes only the `<<PROSE>>` slots (impact paragraphs, plain-terms analogies, executive summary). All counts, citations, and facts are template substitutions — the LLM does not write them by hand.

---

## 6. Report template

Below is the markdown skeleton the LLM fills. `{{slot}}` markers are filled deterministically from `evidence.json` / `audit_facts.json` by `render_report.py`. `<<PROSE>>` regions are the only places the LLM generates free text.

````markdown
# Mobile performance audit — `{{slug_or_audit_id}}`

**Verdict:** {{verdict_emoji}} **{{verdict}}** · **Overall Score:** {{overall_score}} / 100
**Project:** Expo SDK {{expo_sdk_version}} · {{typescript_or_javascript}} · {{router_label}}
**Audit ID:** `{{audit_id}}`
**Generated:** {{audit_date}}

---

## Executive Summary

<<PROSE: 2 paragraphs. First paragraph: what's working architecturally — cite facts.json fields in natural language (e.g. "Hermes is enabled, the new architecture is opted in, and react-native-reanimated is used for animations"). Second paragraph: the top concerns from the report. Cite counts via {{count_perf_006}}-style slots — never narrate numbers.>>

### Severity counts

| Priority | Count | When to fix |
|----------|-------|-------------|
| 🔴 CRITICAL | {{critical_count}} | Fix before launch |
| 🟡 HIGH | {{high_count}} | Fix as you scale |
| 🟢 LOW | {{low_count}} | Nice to have |

### Per-category breakdown

| Category | Critical | High | Medium | Low | Score |
|----------|---------:|-----:|-------:|----:|------:|
{{per_category_rows}}

### Top {{top_n}} highest-impact findings

{{top_n_list}}

### Measured device metrics (per-platform breakdown)

{{metrics_dashboard_table}}

<<PROSE: 3–5 short bullets explaining what each metric means in plain terms. Only emit bullets for metrics that have actual values (skip device metrics if the device stages were skipped).>>

---

## Technical findings

### CRITICAL — Fix before launch

{{for each finding in evidence where severity == CRITICAL and verdict == REAL:}}
#### **CRITICAL** · {{rule_id}} · {{category}} · {{title}}

{{count_label}}  <!-- e.g. "3 sites across 2 files" — REAL count only -->

- **Location:**
{{locations_grouped_by_directory}}  <!-- file : [function, ...] tree -->
- **Evidence:** <<PROSE: 1–3 sentences describing what the analyzer detected. Use natural language for codebase facts. Cite file:function, never file:line in body prose.>>
- **Impact:** <<PROSE: 2–3 sentences on technical + business impact.>>

> **In plain terms:** <<PROSE: 1–2 sentence analogy.>>

- **Actionables:**
  - <<PROSE: 3–5 bullet recommendations specific to this finding.>>

- **After fixing:** <<PROSE: 1 sentence on user-visible improvement.>>

---

{{end-for}}

### HIGH — Fix as you scale
{{same format as CRITICAL}}

### LOW — Nice to have
{{same format, but "In plain terms" optional for trivial findings}}

---

### What's working well

| Check | Status | Notes |
|-------|:------:|-------|
{{passing_checks_rows}}

---

## Bundle composition

{{bundle_table_if_bundle_stage_ran}}

<<PROSE: 1 paragraph commenting on the bundle composition. Omit if bundle stage was skipped.>>

---

## Device performance

{{device_metrics_table_if_device_stage_ran}}

<<PROSE: 1 paragraph on device-perf takeaways. Omit if device stage was skipped.>>

---

## Remediation roadmap

### Fix before launch (Critical)

| # | Finding | What to do | Expected improvement |
|---|---------|------------|----------------------|
{{remediation_critical_rows}}

### Fix as you scale (High)
{{remediation_high_rows}}

### Nice to have (Low)
{{remediation_low_rows}}

---

## Summary

| Metric | Value |
|--------|-------|
| Overall Score | {{overall_score}} / 100 |
| Findings in This Report | {{total_real_findings_count}} |
| CRITICAL / HIGH / LOW | {{critical_count}} / {{high_count}} / {{low_count}} |
| Bundle Size | {{bundle_size_or_na}} |
| Highest-Impact Action | <<PROSE: 1 sentence on the single biggest lever.>> |

---
*Report generated by `mobile-perf-audit` pipeline · {{audit_date}}*
````

### Template-fill rules

- Every `{{slot}}` is filled from `evidence.json` or `audit_facts.json` deterministically by `render_report.py`. Empty rows (zero REAL findings in a category) drop the entire section.
- `<<PROSE>>` blocks are the ONLY places the LLM writes free text. The LLM must not invent numbers, file paths, or citations in prose blocks.
- The "Top N" list is sorted by `rank_weight` (see `references/ranking_heuristics.md`).
- `{{count_label}}` for each finding shows `"N sites across M files"` where N is `summary[rule_id].real` and M is `summary[rule_id].distinct_files`. **Never expose FP / uncertain counts in the report.** If `uncertain > 0` and those hits genuinely need human follow-up, fold a single short note into the finding's Actionables ("a few sites need a human spot-check before the fix lands"), not as a separate section or count.
- **No footer references to evidence/facts/decisions files.** The report ends with the standard generation timestamp. Internal artefacts stay internal.
- The "Measured metrics" dashboard, "Bundle composition", and "Device performance" sections are conditionally rendered — if the relevant stage was skipped, the section is omitted entirely (not shown as "N/A").

---

## 7. Forbidden patterns

Explicit examples of phrasings the LLM must not produce. If a draft contains one of these, rewrite or drop the claim.

| Forbidden | Why | Allowed alternative |
|---|---|---|
| "approximately 12 findings" | Rounded narrated count | "12 findings" — exact count from evidence.json, or omit the number |
| "the analyzer flagged X false positives, excluded after review" | Meta-commentary about analyzer noise — internal scaffolding | Just do not include the FPs. They are already excluded in evidence.json. The reader does not need to know they ever existed. |
| "(analyzer raw count: 47; 9 false positives filtered in Pass A: ScrollView-with-bounded-slice ...)" | Same meta-commentary, parenthetical form | Drop the parenthetical entirely. The finding count IS the real count. |
| "8 verified · 1 uncertain · 5 distinct files" | Verdict labels surfaced to the report reader | "8 sites across 5 files". Verdict labels are internal to evidence.json. |
| "`facts.source_pattern_counts.react_memo_count = 0` confirms no memoization" | JSON-path citation in report prose | "No components use `React.memo`." Plain language; the LLM verified privately. |
| "(`evidence.PERF-006.real`, all `verified` confidence)" | Internal artefact reference in prose | Drop. The number is the finding; the source artefact is not relevant to the reader. |
| "Pass A flagged 5 sites as UNCERTAIN" / "Pass A's automated rules were too conservative" | Pipeline-internal mechanics surfaced | If the hits really need human follow-up, write one sentence in Actionables ("a handful of sites need a human spot-check"). Do not reference "Pass A". |
| "## Pass A — uncertain hits to spot-check" (as a section) | Whole section of internal scaffolding | Delete the section. Either drop the uncertain hits or fold a short note into the relevant finding. |
| Footer: "evidence in .audit-runs/{id}/evidence/evidence.json, facts in audit_facts.json" | Exposing internal artefacts | Footer is just the generation timestamp. Internal files do not link from the report. |
| "the codebase has no `expo-image`" (without checking facts) | Negative claim from inference | The LLM verifies against facts.json privately. Report sentence: "`expo-image` is installed but not used anywhere in the source." — only when both facts are present. |
| "add `react-native-reanimated`" (when reanimated_present == true) | Recommendation without checking facts | "Migrate the remaining `Animated` usages to `react-native-reanimated` (already installed)." |
| "verified by reading every cited line" | Trust-me language about the LLM's process | Drop. The reader does not need reassurance about method. |
| "see `MainScreen.tsx` (likely existed in v1)" | Speculative / unverified citation | Every citation must come from current evidence. Grep before including. |
| "the codebase does not follow modern React Native best practices" | Unfalsifiable assertion | Cite the specific structural finding. "Eight of twelve list-rendering sites use `<FlatList>`; switching to `<FlashList>` would reduce memory pressure on long lists." |
| "this is a well-known anti-pattern" | Hand-wavy authority | Describe what the analyzer detected and the structural fix. No appeals to authority. |
| "we found N issues" / "our analysis shows…" | First-person plural | Third-person: "The audit detected N issues." The pipeline is not a "we". |
| Citing `App.js:468` | Line-number in prose body | Cite `App.js — <function name>` in body; line numbers stay in evidence.json. |
| "the agent verified this" / "Claude analysed the bundle" | Agent-vendor mention | Use neutral phrasing: "The audit verified…". The pipeline is LLM-agnostic. |
| "in our experience" / "from past audits" | Cross-audit context leakage | Every audit stands alone. No reference to other audits. |

---

## End of references.md

If you are the LLM running this skill: load this file once at the start of every audit. Apply §1's rules to every claim. Apply §2's verification protocol to every analyzer hit. Use §3 to know what each check is checking for. Use §4 to ground terminology. Use §5 to know the output shape. Use §6 to know how to render the report. Use §7 to know what to avoid.

The pipeline's correctness depends on this file being followed exactly. Skipping a step or rewriting a rule mid-audit invalidates the resulting report.
