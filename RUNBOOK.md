# RUNBOOK — running an audit end-to-end

Operator-facing instructions for executing the pipeline against an Emergent-hosted pod. Pairs with `SKILL.md` (orchestration spec) and `architecture.md` (design rationale).

**Execution model.** The operator host runs the pipeline files locally and reaches the target environment over an MCP gateway. Work is split across three tiers:

- **Agent-side** (operator host, on ingested source): static AST, config, facts, store readiness, backend. No `node_modules` required.
- **On-pod** (via the MCP gateway's bash-execution tool): anything needing the project's installed `node_modules` — bundle export, source-map-explorer, dependency hygiene tooling, optional Reassure. Only small JSON results return to the operator host.
- **Separate runner** (local Mac / Linux box with device tooling): device runtime. Never the pod — pods cannot run emulators (no nested virtualisation) and their `hermesc` is the wrong CPU architecture.

---

## Inputs

| Input | Required | Notes |
|---|---|---|
| `job_id` | yes | the pod's job UUID |
| `slug` | yes | the pod's slug (used as `env_key` for the MCP) |
| `EMERGENT_AUTH_TOKEN` | yes | platform token for wake; injected by host, never prompted |
| APK (+ `applicationId`) | device layer only | from infra; preview/dev profile, Hermes on |
| `FLASHLIGHT_API_KEY` | device-cloud only | free, app.flashlight.dev/api-key |

---

## Step 0 — Wake the pod

```bash
python3 scripts/wake_pod.py <job_id>
```

- Uses `curl` under the hood. The platform API is behind a Cloudflare-class WAF that 403-challenges Python `urllib`'s default User-Agent; `curl`'s UA passes.
- The restart POST returns `{"status":"success",...}`; then it waits ~15 s for boot.

## Step 1 — Reach the pod, handle the 4 access states

Probe with a cheap command and interpret the gateway's response:

```
mcp__e1__execute_tool(env_key=<slug>, tool_name="execute_bash",
                      arguments={"command":"echo POD_OK && ls /app"})
```

| Response | Meaning | Action |
|---|---|---|
| returns `POD_OK` | awake | proceed |
| `... empty IP` | **asleep** | re-run `wake_pod.py`, wait ~18 s, retry |
| `dial tcp …:8010: connect: connection refused` | IP up, **MCP service still booting** | wait ~20 s, retry (do NOT re-wake) |
| `MCP server "e1" is not connected` | **gateway client down** (not pod) | abort; reconnect the e1 MCP |

**Pods sleep mid-run** during gaps — any later call returning `empty IP` triggers an inline re-wake + retry.

## Step 2 — Locate the app + ingest source

```
# find the Expo project (Emergent layout = /app/frontend):
execute_bash: cd /app/frontend && cat package.json && ls app src components screens 2>/dev/null
```

Ingest the source to a local workspace. **The MCP caps stdout at ~38 KB**, so:

```bash
# ON POD: tar the allowlist (NO node_modules, NO yarn.lock — it blows the cap), base64, pad to force auto-save:
cd /app/frontend && tar czf /tmp/src.tgz app store utils components hooks lib screens \
  package.json app.json app.config.* tsconfig.json metro.config.js eslint.config.js babel.config.js 2>/dev/null
sha256sum /tmp/src.tgz
# emit: printf 'B64START:'; base64 -w0 /tmp/src.tgz; printf ':B64END'; printf 'PAD%.0s' $(seq 1 9000)
```

The padded output exceeds the harness's inline limit, so it **auto-saves to a local tool-result file**. Decode from that file with Python, **verify the sha256**, extract into `.audit-runs/<id>/workspace/`. (If source > ~25 KB gzipped, chunk it or just rely on the auto-save trick.) Create an empty `workspace/yarn.lock` (or copy the real one) so package-manager detection sees yarn.

## Step 3 — Agent-side static analysis

```bash
export MOBILE_AUDIT_RUNS_DIR="$PWD/.audit-runs"
python3 scripts/gather_facts.py   <id>     # facts FIRST (config_scan + Pass A read it)
python3 scripts/static_scan.py    <id>     # AST rules (+ ESLint if plugins resolvable)
python3 scripts/config_scan.py    <id>     # Hermes / New Arch from facts
```

## Step 4 — On-pod heavy stages (need node_modules)

Run the tools **on the pod**, pull the small JSON, transform agent-side.

**4a. Bundle size + composition** (handles the ARM/hermesc mismatch with `--no-bytecode`):
```
# ON POD:
cd /app/frontend && npx expo export --platform android --no-bytecode --source-maps --output-dir /tmp/bx
#   → note the printed bundle size (bytes)
BUNDLE=$(find /tmp/bx -name '*.js' -path '*android*' | head -1)
npx source-map-explorer --json "$BUNDLE" "$BUNDLE.map" > /tmp/sme.json
#   → pull /tmp/sme.json (small), then agent-side:
python3 scripts/bundle_scan.py <id> --consume-sme /tmp/sme.json --bundle-bytes <N> --no-bytecode --platform android
```

**4b. Dependency hygiene** (on pod, fold into facts/findings):
```
# ON POD: npx depcheck --json ; npx madge --circular --extensions ts,tsx,js,jsx app ; npx npm-check-updates --jsonAll
```

**4c. Component render perf (Reassure)** — best-effort on the pod (`run_reassure.sh` logic needs jest-expo); skip if it doesn't bootstrap cleanly. Don't block the report on it.

**4f. Backend / DB / algorithm perf (FastAPI, Stage 4f)** — agent-side, no `node_modules` needed:
```bash
python3 scripts/backend_scan.py <id>
```
Runs the 11 ported checks against `workspace/backend/` (or `workspace/server/` / `workspace/api/`):
- `backend.sync_route_handler`, `backend.n_plus_one_query`, `backend.unbounded_query`,
  `backend.mongo_client_not_singleton`, `backend.blocking_work_in_handler`,
  `backend.sequential_await_chain`, `backend.no_projection_on_query`,
  `backend.pydantic_complex_model`
- `database.missing_index`
- `algorithms.nested_iteration`, `algorithms.linear_array_lookup_in_loop`, `backend.sequential_fetch_chain`

If no backend tree was ingested, the stage emits a single `tooling.backend_source_missing` finding and exits clean. For Emergent's split layout (`/app/frontend/` + `/app/backend/`), ingest the backend separately in Step 2: tar `/app/backend/{server.py,routers,services,models,...}` and land it under `workspace/backend/`. The allowlist in `ingest_pod.py` already covers the `backend/`, `server/`, `api/` directory names.

## Running on a Mac (full iOS path)

Apple Silicon Macs run the iOS Simulator natively (no x86 translation), so CPU + memory measurements are within ~30% of a recent iPhone. Memory growth across iterations is fully reliable; FPS, thermal, and battery metrics are omitted by design (see the reliability table below).

**One-time Mac setup:**

```bash
# Toolchains
brew install --cask temurin@17                  # JDK
brew install --cask android-commandlinetools    # Android SDK + cmdline-tools
brew install android-platform-tools             # adb, fastboot
brew install maestro                            # cross-platform e2e
npm install -g @perf-profiler/flashlight        # Android FPS / CPU

# Accept Android SDK licenses (works cleanly on Mac — no Windows BOM workaround)
yes | sdkmanager --licenses

# Install Android platform + ARM64 system image (native on Apple Silicon)
sdkmanager "platforms;android-34" \
           "system-images;android-34;google_apis;arm64-v8a" \
           "emulator"
avdmanager create avd -n audit_a10s \
  -k "system-images;android-34;google_apis;arm64-v8a" -d pixel_4
emulator -avd audit_a10s -no-snapshot -no-audio &

# Xcode for iOS Simulator (App Store: Xcode, then once installed:)
xcode-select --install
sudo xcodebuild -license accept
xcrun simctl list runtimes   # confirm an iOS 17+ runtime is installed
```

**Build an IPA** (one of two paths):

- **EAS cloud build**: `eas build --platform ios --profile preview --non-interactive` then download from the EAS dashboard.
- **Local Xcode**: open the prebuild output (`npx expo prebuild --platform ios`), archive in Xcode, export as `.ipa`.

**Run the audit (both platforms in one pass):**

```bash
export MOBILE_AUDIT_RUNS_DIR="$PWD/.audit-runs"
AID="<audit_id>"

# Static + config + facts (agent-side, identical to other hosts)
python3 scripts/static_scan.py $AID
python3 scripts/config_scan.py $AID
python3 scripts/gather_facts.py $AID

# Bundle (frontend + on-pod export)
python3 scripts/bundle_scan.py $AID --consume-sme /tmp/sme.json --bundle-bytes <N> --platform android
# iOS bundle: re-run on the pod with --platform ios; bundle_scan supports both
python3 scripts/ipa_scan.py $AID incoming/<app>.ipa

# Stage 4f backend (if backend ingested)
python3 scripts/backend_scan.py $AID

# Stage 4e store readiness
python3 scripts/store_readiness_scan.py $AID

# Device runtime — Android (Flashlight local emulator) + iOS (Simulator)
bash scripts/device_perf.sh $AID --apk incoming/<app>.apk --ipa incoming/<app>.ipa \
                                 --runner local --platform all --consent

# Aggregate → verify → synthesize → render
python3 scripts/aggregate_findings.py $AID
python3 scripts/pass_a_verify.py      $AID
python3 scripts/synthesize.py         $AID
python3 scripts/render_report.py      $AID
```

**iOS metric reliability on Mac Simulator:**

| Signal | Reliability |
|---|---|
| Memory growth across iterations (leak detection) | 🟢 fully reliable — same on Sim as on device |
| Cold start (Simulator launch) | 🟡 device-class estimate on Apple Silicon (~30% optimistic vs iPhone); regression-relative only on Intel |
| Peak memory | 🟡 device-class estimate on Apple Silicon |
| Crashes / errors during flow | 🟢 reliable |
| Mean FPS / worst-frame FPS | 🔴 omitted by design — Mac GPU is not iPhone-comparable even on Apple Silicon |
| Thermal throttling | 🔴 not modeled — Mac has active cooling, iPhone throttles |
| Battery / energy | 🔴 not modeled |

For device-quality FPS / thermal / energy: profile on a real iPhone via Xcode Instruments → Time Profiler + Allocations. That requires a paid Apple Developer Program account and a provisioning profile — explicitly out of audit scope.

The renderer surfaces these labels in the iOS per-metric device breakdown so each row carries its own reliability tag.

---

## Step 5 — Device runtime (STANDARD LAYER — every report includes it)

Device runtime is **not optional** — every full audit ships cold-start / FPS / memory via **Flashlight Cloud** (real devices, free). This is the default device runner.

```bash
# STANDARD invocation (Flashlight Cloud, real devices):
export FLASHLIGHT_API_KEY="fl_..."
bash scripts/device_perf.sh <id> --apk incoming/<app>.apk --runner cloud --consent
# Local emulator only when the binary must stay in-house (FPS approximate):
bash scripts/device_perf.sh <id> --apk incoming/<app>.apk --runner local
```

**Required inputs each run** (no device layer without them): the **APK** (preview/dev, Hermes), its **`applicationId`**, and **`FLASHLIGHT_API_KEY`**. `--consent` acknowledges the APK uploads to Flashlight Cloud (standing authorization for these customer audits).

If the app has an OAuth login wall, supply a **deep-link `session_id`** so the flow lands authenticated — otherwise the device metrics cover only the login screen (see the login gotcha). For no-login apps this is a non-issue and full runtime coverage is automatic.

`device_perf.sh` runs `apk_scan.py` (exact shipped sizes from the APK) → extracts screen map → generates the Maestro flow → measures.

**Login gotcha:** if the app uses OAuth-in-system-browser (e.g. "Continue with Google" via `WebBrowser.openAuthSessionAsync`), Maestro **cannot** automate it. Get a deep-link with a valid `session_id` (or a demo mode) to land authenticated, or the runtime audit only covers the login screen. Check `flows/screen_map.json`'s `auth` + the login source before running.

## Step 6 — Aggregate → verify → synthesize → render

```bash
python3 scripts/aggregate_findings.py <id>
python3 scripts/pass_a_verify.py      <id>   # stamps verdicts, writes evidence.json + decisions.log
python3 scripts/synthesize.py         <id>   # dedup, penalty-summation score, slot prep + coverage
# (optional) LLM fills report/prose_fills.json from synthesis_input.json + prompts/synthesize.md
python3 scripts/render_report.py      <id>   # report.md + stdout fences
```

---

## Learnings baked in (so you don't rediscover them)

| Gotcha | Resolution in pipeline |
|---|---|
| urllib 403 at the WAF | `wake_pod.py` shells out to curl |
| pod sleeps mid-run (`empty IP`) | re-wake + retry inline (Step 1 table) |
| pod IP up but MCP refused | wait + retry, don't re-wake |
| e1 client disconnects | abort, reconnect — waking won't help |
| MCP stdout 38 KB cap | tar+base64+pad → auto-save file → decode + sha256 verify |
| ARM pod, x86 `hermesc` | `bundle_scan.py` auto-retries `--no-bytecode`; label "pre-Hermes JS" |
| `.map()` over non-JSX flagged | scrollview rule requires JSX-returning callback |
| big-screen ScrollView dropped as FP | verifier reads whole file; finding anchored at `<ScrollView>` line |
| 28 findings scored 90/EXCELLENT | penalty-summation score (coverage-weighted), not category average |
| OAuth login blocks Maestro | need deep-link `session_id` / demo mode; else cold-start + login only |
| customer APK → 3rd-party cloud | `--consent` gate + data-residency note |

## Full coverage layers

Static (9 AST rules + ESLint) · Config (Hermes/New Arch) · Codebase facts · Bundle size + per-dependency composition + assets (Android + iOS both via `--platform all`) · APK scan (Android shipped bundle + native libs) · **IPA scan** (iOS shipped bundle + Info.plist + PrivacyInfo.xcprivacy + frameworks + architectures) · Dependency hygiene · Component render perf (Reassure) · Android runtime (FPS/startup/memory/jank — Flashlight) · **iOS runtime** (cold start / memory growth / peak memory — `xcrun simctl` + Maestro; FPS omitted by design; rendered with per-metric reliability labels — device-class on Apple Silicon, regression-relative on Intel) · **Backend / DB / algorithm perf** (FastAPI: 12 ported checks — Stage 4f) · Pre-publish readiness (Apple + Google + cross-cutting + process — Stage 4e). The report's **Coverage & limitations** table states which of these actually ran for a given audit.
