---
name: mobile-perf-audit
description: Performance audit pipeline for Expo (React Native) mobile apps. Takes a pod ID; ingests the source, runs static analysis (ESLint + tree-sitter AST + dependency graph), bundle composition analysis (frontend + backend), component-level perf tests, pre-publish readiness (Apple + Google), and on-device measurement (Maestro + Flashlight on Android; xcrun simctl on iOS Simulator). Produces a per-metric, severity-ranked report with full coverage of static, runtime, and publishing signals.
---

# Mobile performance audit

## Purpose

Audit an Expo (React Native) application from a hosted environment and produce a severity-ranked report covering startup, runtime jank, memory, bundle size, backend perf, database, algorithms, code quality, and pre-publish readiness on Android and iOS.

This file specifies orchestration. Per-rule semantics, verification protocol, output schemas, the report template, and forbidden phrasings live in `references.md` (loaded at the start of every audit). All deterministic work — static analysis, bundle inspection, fact gathering, device measurement — lives in `scripts/` and runs via real libraries (ESLint, tree-sitter, source-map-explorer, Reassure, Maestro, Flashlight, `xcrun simctl`). The LLM's role is per-hit verification (Pass A) and prose generation for `<<PROSE>>` slots in the report template (Pass C). Counts, citations, and codebase facts come from machine-readable artefacts and are never narrated from memory.

---

## Hard mandates

These rules are non-negotiable. The pipeline is only correct when all six hold.

1. **Load `references.md` at the start of every audit.** Apply §1's front-matter rules to every claim; apply §2's universal verification protocol to every analyzer hit; render the report from §6's template.
2. **Produce `.audit-runs/{audit_id}/evidence/evidence.json` before any prose is written.** Every analyzer hit gets one row with a verdict (REAL / FP / UNCERTAIN) and a one-line `verification_method`.
3. **Counts in the report equal `len(findings_by_rule[id] where verdict == "REAL")`.** No narrated numbers, no round figures, no "approximately N". If the count is zero, omit the finding entirely.
4. **Negative claims must cite an `audit_facts.json` field.** Phrases like "no X exists", "the codebase has no Y", "Z is missing" require a field in facts whose value confirms absence. If the fact is not there yet, extend `gather_facts.py` to gather it first.
5. **Print the full report to stdout at Step 7.** The host runner's file-persistence behaviour is not guaranteed; stdout is. The complete `report.md` is emitted between `===MOBILE_PERF_AUDIT_REPORT_START===` and `===MOBILE_PERF_AUDIT_REPORT_END===` fences so the calling agent / operator always receives the deliverable, regardless of whether `.audit-runs/` survives skill termination.
6. **Internal scaffolding stays internal.** FP counts, verdict labels, JSON paths, pass names, decisions.log references — none of these appear in the report. See `references.md` §1.8 + §7 for the forbidden-pattern table.

---

## How to run

Provide a **pod ID**, and optionally a config flag set:

```
Run mobile performance audit for pod <pod_id>
Run mobile performance audit for pod <pod_id> --quick
Run mobile performance audit for pod <pod_id> --platform android
```

If no pod ID is given, ask for one.

Audits run one at a time. Per-audit state lives entirely under `.audit-runs/{audit_id}/`. No shared infra, no cross-audit state.

---

## Prerequisites

The pipeline's prerequisites split into four classes. Items in §1 and §2 are required for every audit. §3 lists the system toolchain required for the **device runtime** stage (Android by default; iOS is opt-in and macOS-only). §4 covers items needed only on the target environment.

### 1. MCP gateway

| MCP | Purpose |
|---|---|
| **e1** | Pod environment access — read source, list directories, run shell on the pod. Tool surface is discovered at runtime via `mcp__e1__search_tools(env_key=<slug>)`. The pod-level tools we depend on: `execute_bash`, `execute_bash_streaming`, `read`, `view_file`, `view_bulk`, `glob_files`, `create_file`. |

### 2. Authentication

Two layers; the SKILL never prompts the operator. Both must be present in the environment at the start of an audit.

- **`EMERGENT_AUTH_TOKEN`** — platform-API token used by `wake_pod.py` to wake sleeping pods. The host runner injects it. Missing → abort.
- **MCP session** — `mcp__e1__execute_tool` carries its own gateway-level session auth. No additional env var needed.

### 3. Operator-host toolchain

Two install layers:

**Python deps** (`requirements.txt`):
```bash
python3 -m pip install -r requirements.txt
```
Installs `tree-sitter`, `tree-sitter-typescript`, `jsonschema`, `packaging`. Required for every audit; no system tooling beyond Python ≥ 3.11.

**Node deps** (`package.json`):
```bash
npm install
```
Installs ESLint (with `react-hooks`, `react-perf`, `react-native` plugins), `madge`, `depcheck`, `knip`, `npm-check-updates`, `source-map-explorer`, `ajv`. The pipeline's on-pod stages shell out to these — Node ≥ 18.18 required on whichever host runs them.

**System toolchain — required for the device runtime stage:**

This is the toolchain the Astrova audit was run on. Without it, the device-runtime stage skips cleanly and the rest of the report still renders.

| Tool | Why | Install (macOS) | Install (Linux / Windows WSL) |
|---|---|---|---|
| JDK 17 | `sdkmanager` and Gradle | `brew install --cask temurin@17` | Adoptium JDK 17 (`apt install temurin-17-jdk` on Ubuntu) |
| Android cmdline-tools + sdkmanager | Drives the rest of the Android install | `brew install --cask android-commandlinetools` | Download from developer.android.com |
| Android platform-tools (`adb`) | Talks to the emulator | `brew install android-platform-tools` | `sdkmanager "platform-tools"` |
| Android SDK platform 34 + emulator + system image | The AVD's runtime | `sdkmanager "platforms;android-34" "emulator" "system-images;android-34;google_apis;<arch>"` (where `<arch>` is `arm64-v8a` on Apple Silicon, `x86_64` on Intel / Linux) | same |
| `audit_a10s` AVD | The low-end profile per `configs/android-emulator.json` | `avdmanager create avd -n audit_a10s -k "system-images;android-34;google_apis;<arch>" -d pixel_4` | same |
| Maestro CLI | The user-journey runner | `brew install maestro` | `curl -Ls "https://get.maestro.mobile.dev" \| bash` |
| Flashlight CLI | The Android FPS / memory / CPU profiler | `npm install -g @perf-profiler/flashlight` | same |

Accept the Android SDK licenses once (`yes \| sdkmanager --licenses`). On Apple Silicon, the system image is ARM64-native — no x86 translation layer.

**Mac-only addition (iOS measurement):**

| Tool | Why | Install |
|---|---|---|
| Xcode + Command Line Tools | `xcrun simctl`, iOS Simulator runtimes | App Store: Xcode, then `xcode-select --install && sudo xcodebuild -license accept` |
| iOS Simulator runtime | Booted Simulator the audit installs the IPA into | Inside Xcode → Settings → Platforms |
| EAS CLI (optional) | Cloud IPA builds when local Xcode archive isn't preferred | `npm install -g eas-cli` |

The full Mac install sequence is in `RUNBOOK.md` under "Running on a Mac".

### 4. Target environment (on the pod)

The target's `node_modules` is required for Stage 4c (bundle export + dep hygiene) — the operator host does not install the customer's deps. These run on the pod via the MCP `execute_bash` tool:

- `npx expo` (bundle export) — provided by the customer's Expo SDK install.
- `npx depcheck`, `npx madge`, `npx npm-check-updates` — provided by the customer's `node_modules`.
- `npx source-map-explorer` — installed transitively or via npx-on-demand.

If the pod lacks `node_modules` (the customer hasn't installed deps), Stage 4c emits a single `tooling.bundle_export_failed` finding and the report renders without the per-dependency byte attribution.

---

## Pipeline — 8 steps

### Step 0a — Assert auth token + probe gateway

```python
assert os.environ.get("EMERGENT_AUTH_TOKEN")   # abort if missing — never prompt
mcp__e1__search_tools(env_key=<slug_or_job_id>)
```

`search_tools` returns the pod's tool inventory at the gateway-metadata level. **A successful return here does NOT mean the pod is awake** — only that the gateway has the pod registered. The actual pod might still be sleeping.

Confirm the expected pod-level tools are present: `execute_bash`, `read`, `glob_files`, `create_file`. If the surface is unrecognisable, abort.

### Step 0b — Probe pod reachability, wake only if needed

**The authoritative reachability signal is the MCP gateway, not any HTTP status.** Probe it with a cheap command:

```
mcp__e1__execute_tool(env_key=<slug>, tool_name="execute_bash",
                      arguments={"command": "echo POD_OK"})
```

Interpret the result:
- Returns `POD_OK` → pod is awake. Skip the wake; go to Step 1.
- Returns `failed to resolve env_key ... empty IP` → pod is registered but sleeping. Wake it (below).
- Returns `MCP server "e1" is not connected` → the gateway client connection itself is down. This is NOT a pod-state problem — abort with a clear message telling the operator to reconnect the e1 MCP. Waking won't help.

To wake a sleeping pod:

```bash
python3 scripts/wake_pod.py <job_id>
```

`wake_pod.py` POSTs `https://api.emergent.sh/jobs/v0/{job_id}/restart-environment?upgrade=false&source=manual_wakeup` with `Content-Length: 0` and the bearer token, up to 3 retries (2 s delay), then waits 15 s for boot. A `200 {"status":"success",...}` means the pod is booting. It prints one JSON status line on stdout.

After waking, **re-probe via the MCP** (the `echo POD_OK` call). If still `empty IP`, sleep 10 s and probe once more before giving up.

**Do NOT use the `/trajectories/v0/stream` endpoint to detect sleep** — it returns 403 even for valid pod-scoped tokens. The MCP probe is the only reliable awake signal.

**Failure modes the SKILL must handle:**
- `EMERGENT_AUTH_TOKEN` missing → abort, surface the env-var name.
- Restart POST returns 401 → token is invalid/expired; abort, do not retry.
- Restart POST succeeds but MCP still `empty IP` after the second probe → abort with "pod failed to boot in time".
- MCP `not connected` at any point → abort with "reconnect the e1 gateway"; this is a client-config issue, not a pod issue.

### Step 1 — Initialize the audit

```
bash scripts/init_audit.sh <pod_id>
```

Generates a UUID for `audit_id`. Creates `.audit-runs/{audit_id}/` and its sub-directories (`workspace/`, `evidence/`, `facts/`, `flows/`, `results/`, `artifacts/`, `report/`). Writes `audit.json` with run metadata.

If `MOBILE_AUDIT_RUNS_DIR` is set in the env, the script uses that as the base directory instead of `.audit-runs/`. Honours the host runner's preferred output location.

### Step 2 — Ingest source from the pod

```
python scripts/ingest_pod.py --print-manifest
# … LLM fetches the listed paths via the MCP gateway …
python scripts/ingest_pod.py --validate <workspace>
```

`ingest_pod.py` is a manifest owner and validator, not an active fetcher. The LLM (orchestrator) performs the actual file fetching via the MCP gateway's `execute_bash` / `read` / `glob_files` tools. The script's role is to **own the allowlist + denylist** (one source of truth) and **validate the resulting workspace** (every required allowlist path present, no denylist file leaked in).

**Allowlist** (the paths to pull): `package.json` + lockfiles, `app.json` + `app.config.{js,ts}`, `eas.json`, `tsconfig.json`, `babel.config.js`, `metro.config.js`, `.eslintrc.*`, frontend source dirs (`app/`, `src/`, `components/`, `screens/`, `lib/`, `hooks/`, `utils/`, `constants/`, `types/`), `assets/`, optional `.audit/`. Stage 4f (backend) additionally allows `backend/`, `server/`, `api/`, `requirements.txt`, `pyproject.toml`, `poetry.lock`, `uv.lock`.

**Denylist** (paths to never pull): `node_modules/`, `.expo/`, `.next/`, build outputs (`build/`, `dist/`, `ios/build/`, `ios/Pods/`, `android/build/`, `android/.gradle/`, `android/app/build/`), test dirs (`__tests__/`, `coverage/`), Python caches (`__pycache__/`, `.venv/`, `venv/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`), secrets (`.env*`, `*secret*`, `*credential*`, `*.pem`, `*.key`, `*.p12`, `*.keystore`, `GoogleService-Info.plist`, `google-services.json`, `.npmrc`, `.yarnrc.yml`, SSH keys).

**Two ingest paths in practice.** The MCP gateway caps a single tool-call's stdout at ~38 KiB. Source ≤ that cap can be pulled via direct `read` / `view_file` calls. Anything larger uses the documented tar + base64 + pad workflow (see `RUNBOOK.md` Step 2): tar the allowlist on the pod, base64-encode, pad the output past the harness's inline limit so it auto-saves to a local tool-result file, decode and sha256-verify locally, then extract into `workspace/`.

If ingest fails (MCP unreachable mid-run, sha256 mismatch on the tar, missing required allowlist file): abort the audit with the failure path included in the message.

### Step 3 — Bootstrap workspace

```
bash scripts/bootstrap_workspace.sh <audit_id>
```

Detects the package manager (yarn / pnpm / npm) from the lockfile, runs `<pm> install --frozen-lockfile` in `workspace/`, runs `npx expo-doctor --json`, parses `package.json` + `app.json` / `app.config.*` (via `@expo/config` for the JS variants), populates `.audit-runs/{audit_id}/facts/audit_meta.json` with project-signature fields.

If install fails: write a top-priority finding to `findings/static.json` ("project does not install") and short-circuit the audit — skip stages 4b/4c/4d (anything needing a working build) but still run stage 4a (static scan on raw source) and Step 5.

### Step 4 — Analyze in parallel

Workers run concurrently. They write to per-worker files under `.audit-runs/{audit_id}/findings/` and exit zero on success or non-fatal failure. They never abort each other.

**Step 4a — facts (must complete before 4-bis and Pass A):**

```
python scripts/gather_facts.py <audit_id>
```

`gather_facts.py` populates `facts/audit_facts.json` — the source of truth for every negative claim. Uses real parsers and AST queries, never grep. See `references.md` §3 and `schemas/facts.schema.json`. This runs first because `config_scan.py` and Pass A both read from it.

**Step 4b — workers (run in parallel):**

```bash
# Agent-side workers — read workspace/ only; no on-pod calls, no node_modules.
python scripts/static_scan.py             <audit_id>  &  # 4b.1 — ESLint + tree-sitter AST rules
python scripts/config_scan.py             <audit_id>  &  # 4b.2 — Hermes / New Architecture from facts
python scripts/store_readiness_scan.py    <audit_id>  &  # 4b.3 — App Store + Play Store readiness (Stage 4e)
python scripts/backend_scan.py            <audit_id>  &  # 4b.4 — FastAPI / DB / algorithm checks (Stage 4f)

# On-pod work — needs the customer's node_modules. Driven by the LLM via MCP.
python scripts/bundle_scan.py             <audit_id>  &  # 4b.5 — expo export + source-map-explorer + asset audit
bash   scripts/run_reassure.sh            <audit_id>  &  # 4b.6 — Reassure per-screen render perf

# Operator-provided artefacts (if available)
python scripts/apk_scan.py                <audit_id> <path.apk>  &  # 4b.7 — APK contents (Android)
python scripts/ipa_scan.py                <audit_id> <path.ipa>  &  # 4b.8 — IPA contents (iOS)

# Device runtime — runs on a separate runner, never on the pod.
bash   scripts/device_perf.sh             <audit_id> --apk <path.apk> [--ipa <path.ipa>] --runner local --platform all &  # 4b.9 — device measurement
wait
```

If an optional layer's input is absent (no backend tree ingested for 4f; no APK/IPA for 4b.7–4b.9), the worker emits one `tooling.*` finding noting the skip and exits clean. The report's coverage table reflects what actually ran.

The next subsections detail each worker's responsibility.

#### 4b.1 — Static AST + ESLint

```bash
python scripts/static_scan.py <audit_id>
```

Runs the 9 tree-sitter rules in `configs/ast_rules.py` against every `.ts`, `.tsx`, `.js`, `.jsx` file under `workspace/`, plus the ESLint perf ruleset in `configs/eslint.perf.config.js` (when its plugins are resolvable on the operator host's `node_modules`). Writes `findings/static.json`. Rules cover unbounded `<ScrollView>` + `.map()`, uncached remote `<Image>`, missing `useEffect` cleanup, JS-thread `Animated`, inline JSX props, missing memoization, `console.log` in production, etc. The per-rule semantics live in `references.md` §3.

#### 4b.2 — Config (Hermes / New Architecture)

```bash
python scripts/config_scan.py <audit_id>
```

Reads `facts/audit_facts.json` (populated in Step 4a) and emits `findings/config.json` flagging Hermes-disabled and New-Architecture-disabled when the project's Expo SDK supports them. Severity scales with the SDK floor — explicit opt-out is `critical`; SDK-default-implicit-off (on older SDKs) is `high`.

#### 4b.3 — Pre-publish readiness (Stage 4e)

```bash
python scripts/store_readiness_scan.py <audit_id>
```

Reads `app.json` / `app.config.*`, `package.json`, the source index, and `facts/audit_facts.json`. Runs 26 rules across four namespaces:

- **`store.ios.*`** — Apple App Store: bundle identifier placeholder, missing `NSUsageDescription` for permission APIs called in source, missing `PrivacyInfo.xcprivacy` for detected SDKs, App Tracking Transparency description, Universal Links (`associatedDomains`), ATS, encryption export, deployment target, background modes, IAP setup reminder.
- **`store.android.*`** — Google Play: package name placeholder, `targetSdkVersion` floor, declared-but-unused permissions, used-but-undeclared permissions, `POST_NOTIFICATIONS` for Android 13+, `BILLING` for IAP libs, adaptive icon, cleartext traffic, App Links `autoVerify`, foreground service type, `versionCode`.
- **`store.cross.*`** — both stores: app icon, display name, version, hardcoded dev URLs in shipped source, unguarded test-mode API keys.
- **`store.process.*`** — auto-assessed process items: privacy policy URL declaration, `google-services.json` / `GoogleService-Info.plist` presence, push credential wiring, IAP SKU enumeration (parses source for SKU literals), per-SDK Privacy Nutrition Label + Play Data Safety category requirements.

Writes `findings/store.json`. `synthesize.py` then computes a `READY / AT_RISK / BLOCKED` verdict per store from the severity counts. This verdict is **independent of the perf score** — publishing-readiness findings drive a separate panel; they do not move the 0–100 perf number.

#### 4b.4 — Backend / DB / algorithm (Stage 4f)

```bash
python scripts/backend_scan.py <audit_id>
```

Runs only if `workspace/backend/` (or `workspace/server/` or `workspace/api/`) was ingested in Step 2. Otherwise emits a single `tooling.backend_source_missing` finding and exits clean.

Twelve rules ported from the web `perf-audit` pipeline:

- **`backend.*`** — `sync_route_handler`, `n_plus_one_query`, `unbounded_query`, `mongo_client_not_singleton`, `sequential_await_chain`, `blocking_work_in_handler`, `no_projection_on_query`, `pydantic_complex_model`, `sequential_fetch_chain`.
- **`database.*`** — `missing_index`.
- **`algorithms.*`** — `nested_iteration`, `linear_array_lookup_in_loop`.

Detection uses Python `ast` for AST-shaped patterns (route decorators, function bodies) and regex for line-level patterns (consecutive `await`s, nested `.filter().filter()` chains). Writes `findings/backend.json`. These findings DO contribute to the perf score via the `backend_perf`, `database`, and `algorithms` category buckets.

#### 4b.5 — Bundle composition + dependency hygiene

The bundle and dep-hygiene work is the only Step-4 work that must run on the pod (the operator host does not install the customer's `node_modules`). The orchestrator issues these via the MCP `execute_bash` tool:

```bash
# ON POD:
cd /app/frontend && npx expo export --platform android --no-bytecode --source-maps --output-dir /tmp/bx
BUNDLE=$(find /tmp/bx -name '*.js' -path '*android*' | head -1)
npx source-map-explorer --json "$BUNDLE" "$BUNDLE.map" > /tmp/sme.json
# Pull /tmp/sme.json back to the operator host, then agent-side:
python scripts/bundle_scan.py <audit_id> --consume-sme /tmp/sme.json --bundle-bytes <N> --no-bytecode --platform android

# ON POD (dependency hygiene):
npx depcheck --json
npx madge --circular --extensions ts,tsx,js,jsx app
npx npm-check-updates --jsonAll
```

The `--no-bytecode` flag works around an ARM/`hermesc` mismatch on the pod (the pod's `hermesc` is the wrong CPU architecture for its host); the produced bytes are labelled as "pre-Hermes minified JS — a code/dependency-weight proxy, not the shipped bytecode" in the report. Faithful shipped-bundle sizes come from `apk_scan.py` (Step 4b.7) when an APK is supplied.

For iOS bundle composition, repeat the `expo export` step with `--platform ios`; `bundle_scan.py` accepts a second `--consume-sme` invocation with `--platform ios`.

#### 4b.6 — Component render perf (Reassure)

```bash
bash scripts/run_reassure.sh <audit_id>
```

Best-effort. Requires `jest-expo` to bootstrap on the pod; when it can't (missing peer deps, version conflicts) the script emits a `tooling.reassure_unavailable` finding and exits zero. When it does run, `gen_reassure_tests.py` instantiates `configs/reassure-test-template.tsx` for every screen-shaped default export under `workspace/app/**`, `npx reassure` measures render counts + durations, `transform_reassure.py` lifts thresholded results into findings.

#### 4b.7 — APK scan

```bash
python scripts/apk_scan.py <audit_id> <path-to.apk>
```

Cross-platform (stdlib only). Unzips the APK, reads `assets/index.android.bundle` (the **actual** shipped JS bytecode — more accurate than the pod-side `--no-bytecode` export), enumerates `lib/<arch>/*.so` for the native libs + architecture coverage, sums the total install footprint. Writes `findings/apk.json` and `artifacts/apk_scan.json`.

#### 4b.8 — IPA scan

```bash
python scripts/ipa_scan.py <audit_id> <path-to.ipa>
```

Cross-platform (stdlib only). Unzips the IPA, parses `Info.plist` (handles binary plist via `plistlib`), enumerates `Payload/<App>.app/Frameworks/*.framework`, sums the install footprint, detects the main binary's architectures (uses `lipo` when on macOS; falls back to Mach-O magic-byte heuristic elsewhere), checks for `PrivacyInfo.xcprivacy` and `embedded.mobileprovision`. Writes `findings/ipa.json` and `artifacts/ipa_scan.json`. Flags arm64-missing as CRITICAL and missing privacy manifest as HIGH.

#### 4b.9 — Device runtime

See "Stage 4d details" below.

#### Stage 4d details (device measurement)

**Device measurement does not run on the pod.** Pods cannot run emulators (no nested virtualization) and their `hermesc` is the wrong CPU architecture to compile a faithful Hermes build. Stage 4d runs on a separate runner.

`device_perf.sh` orchestrates the device sub-stages:

```
4d.1  obtain build              # PREFERRED: --apk / --ipa from infra (real Hermes binary).
                                # FALLBACK: build_app.sh (EAS local build).
                                # APK present → apk_scan.py runs; IPA present → ipa_scan.py runs.
4d.2  extract_screen_map.py     # tree-sitter walk over app/ → flows/screen_map.json
4d.3  generate_draft_flow.py    # deterministic baseline → flows/draft.yaml + draft_intent.json
4d.4  refine_flow_with_llm.py   # LLM fills flow_intent JSON (not YAML); renderer produces main.yaml
4d.5  validate_flow.sh          # LOCAL runner only — dry-run maestro on emulator, capture UI dumps
4d.6  repair_flow_with_llm.py   # LOCAL runner only — if 4d.5 failed; one attempt
4d.7  measure Android           # --runner local → run_android_perf.sh (emulator + Flashlight) — DEFAULT
                                # --runner cloud → run_flashlight_cloud.sh (Flashlight Cloud, real devices)
4d.8  run_ios_perf.sh           # LOCAL macOS only — xcrun simctl + Maestro (Flashlight Cloud is Android-only)
4d.9  compute_device_metrics.py + transform_device_metrics.py  # per-metric breakdown + findings
```

**Default runner is `local`.** `device_perf.sh` sets `RUNNER="local"` by default. The local runner uses the operator-host's Android emulator (the `audit_a10s` AVD per `configs/android-emulator.json`) and Flashlight CLI; on macOS hosts it additionally drives the iOS Simulator via `xcrun simctl`. This is the path the Astrova audit used and the path the documented toolchain in §3 supports.

**Invocation shapes:**

```bash
# DEFAULT: local emulator + Flashlight (Android) + local Simulator (iOS, macOS only)
bash scripts/device_perf.sh <audit_id> --apk <path.apk> [--ipa <path.ipa>] --platform all

# Same default, with explicit runner flag
bash scripts/device_perf.sh <audit_id> --apk <path.apk> --runner local --platform android

# Alternate: Flashlight Cloud (Android-only, real devices, requires external upload consent)
bash scripts/device_perf.sh <audit_id> --apk <path.apk> --runner cloud --consent
```

**When to choose Cloud over local.** Flashlight Cloud runs the flow on real devices in a hosted lab. Use it when:
- The operator host doesn't have the Android toolchain installed (no emulator / SDK / Flashlight CLI).
- Real-device fidelity (vs. an emulator) is needed for the customer.

Cloud's tradeoffs: queue depth varies (we've seen 300+ position queues during peaks), Android-only (no iOS path), and **uploads the customer's APK to a third party** — requires explicit `--consent` (or `DEVICE_CLOUD_CONSENT=1`) and `FLASHLIGHT_API_KEY` in the env. Local emulator avoids both — the default is to keep the binary in-house.

**iOS measurement (4d.8).** Runs only on macOS hosts; the script gracefully emits a `tooling.ios_unsupported_host` finding on Linux / Windows and exits clean. The iOS path:

- Boots the iOS Simulator (`xcrun simctl boot <UDID>`), installs the IPA (`xcrun simctl install`), launches the app once for cold-start timing, then loops N iterations (default 3) of the Maestro flow capturing per-iteration RSS deltas.
- Detects host architecture (`uname -m`) — `arm64` Apple Silicon Macs run the iOS binary natively, so memory and cold-start measurements are within ~30% of a recent iPhone; on Intel Macs the same numbers are regression-relative only. The host arch is stamped into the perf_result so the renderer applies the right reliability label per metric.
- **FPS is intentionally omitted** from iOS Simulator runs — Mac GPU is not iPhone-comparable even on Apple Silicon. Cold start, memory growth across iterations, and crash detection are the reliable signals.

**Provided artefacts vs. `build_app.sh`.** When the operator supplies `--apk` / `--ipa`, the orchestrator skips the EAS build and runs `apk_scan.py` / `ipa_scan.py` against the provided binary. This is preferred: the supplied binary is the artefact the customer actually ships, so the reported shipped-bundle / native-lib / install-footprint figures are exact. `build_app.sh` is the fallback when no binary is supplied and EAS is configured.

**Maestro generation is structured, not freeform.** The LLM fills `flows/refined_intent.json` (matching `schemas/flow_intent.schema.json`); `scripts/render_flow_yaml.py` translates the validated intent into Maestro YAML. The LLM never writes YAML directly — this prevents invented selectors and indentation drift.

**LLM hand-off at 4d.4:**
1. The orchestrator runs `refine_flow_with_llm.py --prepare`, producing `flows/refine_inputs.json`.
2. The calling LLM (this skill, in your session) reads `flows/refine_inputs.json` + `prompts/refine_flow.md` and writes `flows/refined_intent.json`.
3. The orchestrator runs `refine_flow_with_llm.py --render` to produce `flows/main.yaml`.

If the LLM hand-off is skipped (e.g. in CI without an LLM in the loop), the renderer falls back to `flows/draft_intent.json` so Maestro always has something to run.

The same hand-off pattern applies at 4d.6 (repair) when validation reports failures.

**Single repair attempt.** If validation fails twice, the orchestrator accepts partial coverage rather than looping. A `tooling.flow_partial_coverage` finding documents the gap; the user can drop a hand-written `.audit/maestro-flow.yaml` for the next run.

**Per-stage degradation (4d):**

| Condition | Effect |
|---|---|
| `--quick` | Skip 4d entirely; report renders without device sections. |
| `--platform android` / `--platform ios` | Run one side only; the other side's findings + measurement table column are omitted. |
| EAS CLI / eas.json missing | Skip 4d.1 → no APK/IPA → 4d.5–4d.8 also skip; surfaced as `tooling.eas_unavailable`. |
| No Android emulator on host | Skip 4d.5 + 4d.7; iOS path unaffected. |
| Non-macOS host | Skip 4d.8 (iOS); Android path unaffected. |
| Maestro / Flashlight / xcrun missing | Surfaced as individual `tooling.*_unavailable` findings; affected sub-stages skip. |
| Validation fails twice | Accept partial coverage; `tooling.flow_partial_coverage` finding documents it. |

#### Pod-access states (apply to EVERY MCP call, all steps)

The gateway returns one of four distinct signals. The skill handles each differently — do not conflate them:

| Signal from the MCP call | Meaning | Action |
|---|---|---|
| Tool runs, returns output | Pod awake & reachable | proceed |
| `failed to resolve env_key … empty IP` | Pod is **asleep** (registered, no running IP) | run `wake_pod.py <job_id>`, wait ~18 s, re-probe with `echo POD_OK`; retry the call once |
| `dial tcp …:8010: connect: connection refused` | Pod has an IP but its **MCP service is still booting** | wait ~20 s and retry the same call (do NOT re-wake — it's already coming up) |
| `MCP server "e1" is not connected` | The **gateway client** in this session is down (not a pod-state issue) | abort with "reconnect the e1 gateway"; waking won't help |

**Mid-run sleep is expected** on long audits — pods nap during gaps. Any MCP call that returns `empty IP` partway through triggers a single re-wake + retry inline, then continues. The wake uses curl (not urllib) because the platform WAF 403-challenges urllib's user-agent.

### Step 5 — Aggregate findings

```
python scripts/aggregate_findings.py <audit_id>
```

Loads every `findings/*.json` file, validates each Finding against `schemas/finding.schema.json`, concatenates into `findings/all_findings.json`. Fails loudly on schema mismatch — a worker that produces invalid findings has a bug worth catching.

### Step 6 — Pass A (verify) and synthesize

```
python scripts/pass_a_verify.py <audit_id>
python scripts/synthesize.py    <audit_id>
```

**Pass A** applies the universal verification protocol from `references.md` §2 to every analyzer hit in `findings/all_findings.json`. For each finding:
- Reads N lines of context (via `envcore_gateway`'s file-read tool — or from the local `workspace/` if the file was ingested) per the rule's `verification_protocol_additions` in references.md §3.
- Confirms preconditions.
- Cross-references `audit_facts.json` where the rule requires it.
- Stamps `verdict` (REAL / FP / UNCERTAIN) and a one-line `verification_method`.
- Appends one line to `decisions.log`.

Output: `evidence/evidence.json` with all findings, verdicts populated, per-rule `summary` counts, and `category_counts` aggregated.

**Synthesize** then performs deterministic dedupe + rank in Python using `references/ranking_heuristics.md`, computes the overall score and per-category scores, and produces a partially-filled `report_slots.json` + `report_skeleton.md` for the LLM to fill `<<PROSE>>` regions in.

The LLM is invoked with `prompts/synthesize.md` and the inputs above. Its response is validated against the prose-fills JSON schema; on schema failure, it's re-prompted with the validation error.

### Step 7 — Render and emit the report

```
python scripts/render_report.py <audit_id>
```

Substitutes the LLM's `<<PROSE>>` fills into the skeleton, writes `report/report.md` (and `report/report.json`).

**Then prints the full `report.md` to stdout** between delimiter fences as the guaranteed delivery channel (Hard Mandate 5):

```
===MOBILE_PERF_AUDIT_REPORT_START===
{full report.md content — every byte, no truncation}
===MOBILE_PERF_AUDIT_REPORT_END===
```

Followed by a short operator summary block (5–7 lines):
- Overall score.
- Severity counts (Critical / High / Low) from `evidence.json`.
- Top 3 finding titles (one line each).
- Local file paths for the artefacts (`evidence.json`, `audit_facts.json`, `decisions.log`, `report.md`) — noted as "if persisted by the host runner" so it is clear these are reference paths, not guarantees.

---

## Error handling

| Scenario | What to do |
|---|---|
| `envcore_gateway` unreachable at Step 0 | Abort with clear configuration error. Never prompt, never retry indefinitely. |
| Pod ingest fails mid-run | Abort with a clear message including the failed path. |
| `npm install` / `yarn install` fails at Step 3 | Continue — write a top-priority `tooling.project_install_failed` finding, skip stages 4b / 4c / 4d, still run Stage 4a + facts + aggregation + report. |
| One Stage-4 worker fails | Continue — that worker writes a single `tooling_error` finding and exits zero. The other workers proceed. |
| Device build fails for one platform | Continue — produce a single-platform report, mark the other platform as not measured. |
| Both device builds fail | Continue — write one `tooling.device_measurement_unavailable` finding and skip Stage 4d entirely. Report renders with no device section. |
| Host machine lacks iOS Simulator (Linux host) | Skip iOS sub-stages. Render Android-only report. |
| Host machine lacks Android emulator | Skip Android sub-stages. Render iOS-only report (if iOS available). |
| User passes `--quick` | Skip Stages 4c and 4d. Static + bundle only. Faster, less complete. |
| User passes `--platform android` or `--platform ios` | Run one device side only. |
| Pass A produces zero REAL findings | Render a "no issues detected" report — this is a valid outcome. |
| LLM synthesis produces schema-invalid JSON | Re-prompt with validation error attached. Three retries; on third failure, fall back to deterministic prose stubs from `references.md` §6's default prose patterns. |

---

## Escape hatches

- `.audit/maestro-flow.yaml` in the user's project → bypass Stage 4d's auto-generated flow.
- `.audit/audit-config.json` in the user's project → override severity thresholds, exclude paths from static scan, override the heavy-deps list, etc.
- `--quick` → skip device stages.
- `--platform android` or `--platform ios` → run one side only.
- `MOBILE_AUDIT_RUNS_DIR` env var → override the `.audit-runs/` base directory.

---

## Constraints

- Only report what is directly observable in `findings/*.json`, `audit_facts.json`, the per-platform device JSON in `results/`, or code read live via `envcore_gateway`. No assumptions, no inferences from naming conventions.
- If a file cannot be read, the hit's verdict is `UNCERTAIN` (internal) and it is not surfaced as a finding (per references.md Rule 1.8).
- Do not recommend architectural changes beyond pre-launch scope.
- The audit is read-only on the target environment. No source modifications.
