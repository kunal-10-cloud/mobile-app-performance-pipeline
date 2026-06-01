# test-fixture/

Regression test inputs for the mobile-perf-audit pipeline. Three sub-fixtures:

| Sub-fixture | Path | Covers |
|---|---|---|
| Expo app with planted frontend anti-patterns | `app/`, `app.json`, `package.json`, `tsconfig.json` | Static AST rules (9), config (2), bundle composition (2) |
| FastAPI backend with planted server-side anti-patterns | `backend/server.py` + `backend/helpers.js` | Backend perf rules (9), database (1), algorithms (2) — total 12 |
| Synthetic iOS IPA built on demand | `build_ios_fixture.py` → `ios-fixture/fixture.ipa` | IPA scan rules (5) |

Each anti-pattern below should trigger exactly one rule. When a rule stops
surfacing on its planted input, the rule has regressed.

## Frontend (Expo app)

| Where | Rule it triggers | Why it's planted |
|---|---|---|
| `app.json` — `jsEngine: "jsc"` | `static.hermes_disabled` | Hermes explicitly off |
| `app.json` — no `newArchEnabled` on SDK 51 | `static.new_architecture_disabled` | New Architecture not opted in |
| `app/(tabs)/index.tsx` — `<ScrollView>{items.map(...)}` | `static.scrollview_with_long_list` | Unbounded `.map()` in ScrollView |
| `app/(tabs)/index.tsx` — `<Image source={{ uri }} />` | `static.image_without_caching` | RN `<Image>` with remote URI |
| `app/(tabs)/index.tsx` — `<FlatList renderItem={() => …}>` | `static.inline_arrow_in_renderitem` | Inline arrow in `renderItem` |
| `app/(tabs)/index.tsx` — `useEffect(() => …)` no deps | `static.useeffect_no_deps` | Runs every render |
| `app/(tabs)/index.tsx` — `useEffect` with `setInterval`, no cleanup return | `static.useeffect_missing_cleanup` | Leak vector — timer keeps firing after unmount |
| `app/(tabs)/index.tsx` — `console.log(…)` outside `__DEV__` | `static.console_log_in_production_code` | Ships to release builds |
| `app/(tabs)/index.tsx` — `import { Animated } from 'react-native'` | `static.animated_api_usage` | JS-thread animations |
| `app/(tabs)/index.tsx` — `contentContainerStyle={{ … }}` etc. | `static.inline_object_props` (multiple) | Inline object literal props |
| `app/(tabs)/profile.tsx` — `ProfileScreen` >100 LOC | `static.large_unmemoized_component` | Big component, no `React.memo` |
| `package.json` — `lodash`, `moment`, `axios`, `lodash-es` | `bundle.known_bloated_dependency` × 3–4 + `bundle.duplicate_dependency_libs` | Heavy deps + duplicate pairs |
| `app/login.tsx` | (not a rule — exercises `extract_screen_map.py` auth detection) | Email + password TextInputs + "Log in" button |

## Backend (FastAPI + JS helpers)

The backend sub-fixture must be copied into the audit's `workspace/backend/`
before running `backend_scan.py`:

```bash
cp -r test-fixture/backend .audit-runs/<audit_id>/workspace/
python3 scripts/backend_scan.py <audit_id>
```

| Where | Rule it triggers | Why it's planted |
|---|---|---|
| `backend/server.py` — `def list_users()` decorated with `@app.get("/users/list")` | `backend.sync_route_handler` (HIGH — hot-path name) | Sync handler on a `list` route |
| `backend/server.py` — `def admin_cleanup()` decorated with `@app.post("/admin/cleanup")` | `backend.sync_route_handler` (MEDIUM) | Sync handler on a non-hot-path |
| `backend/server.py` — `aggregate_orders` with `find_one()` inside `for` loop | `backend.n_plus_one_query` | Per-iteration DB round-trip |
| `backend/server.py` — `list_all_products` with `.to_list(length=None)` | `backend.unbounded_query` | Cursor with no bound |
| `backend/server.py` — `get_cart` constructs `AsyncIOMotorClient(...)` in body | `backend.mongo_client_not_singleton` (CRITICAL) | New connection pool per request |
| `backend/server.py` — `notify` calls `stripe.PaymentIntent.create` inline | `backend.blocking_work_in_handler` | Sync external call inside async route |
| `backend/server.py` — `dashboard` with 4 sequential `await`s, distinct vars | `backend.sequential_await_chain` | `asyncio.gather` candidate |
| `backend/server.py` — multiple `.find({...})` with no projection | `backend.no_projection_on_query` (capped at 10) | Over-fetch all fields |
| `backend/server.py` — `UserProfile(BaseModel)` with 3+ Optional/List fields | `backend.pydantic_complex_model` | Nested validation overhead |
| `backend/server.py` — fields queried in `.find()` but no `create_index(...)` in file | `database.missing_index` | Full collection scan |
| `backend/helpers.js` — `findMatches` with `.filter(...).filter(`) | `algorithms.nested_iteration` | O(n²) shape |
| `backend/helpers.js` — `flagSelected` with `.includes(` inside `for` | `algorithms.linear_array_lookup_in_loop` | O(n×m) — use a `Set` |
| `backend/helpers.js` — `loadDashboard` with 3 consecutive `await fetch(...)` | `backend.sequential_fetch_chain` | `Promise.all` candidate |

## iOS IPA scan

The synthetic IPA isn't committed (it's a binary the builder produces in
under a second from the spec in `build_ios_fixture.py`). Build it before
running the IPA scanner:

```bash
python3 test-fixture/build_ios_fixture.py
python3 scripts/ipa_scan.py <audit_id> test-fixture/ios-fixture/fixture.ipa
```

| Spec in `build_ios_fixture.py` | Rule it triggers | Why it's planted |
|---|---|---|
| `_expo/static/js/ios/index-*.hbc` sized ~3.2 MiB | `bundle.shipped_bundle_size_ios` (informational) + `bundle.bundle_too_large_warning_ios` (MEDIUM) | Bundle ≥ 2 MiB warning band |
| Total app contents | `bundle.ipa_install_footprint` (informational) | Always emitted |
| One `Frameworks/Hermes.framework/` | `bundle.ipa_native_framework_count` (informational) | Native framework count |
| `PrivacyInfo.xcprivacy` intentionally omitted | `bundle.ipa_privacy_manifest_missing` (HIGH) | Apple privacy-manifest requirement since May 2024 |

## Running the audit against the fixture

From the `mobile-perf-audit/` repo root:

```bash
bash scripts/run_local_audit.sh test-fixture
```

That:
1. Skips the MCP ingest stage (source is already on disk).
2. Copies `test-fixture/` into `.audit-runs/<audit_id>/workspace/`.
3. Runs the static + config + bundle + reassure stages.
4. Synthesizes + renders the report.

The expected outcome lives in [`expected.md`](./expected.md) — rule IDs that
must appear in `evidence.json` after a clean run. Diff that file against the
real `decisions.log` to find regressions.

To exercise Stage 4f (backend) and IPA scan, run the additional workers
manually after step 2 above:

```bash
cp -r test-fixture/backend .audit-runs/<audit_id>/workspace/
python3 scripts/backend_scan.py <audit_id>
python3 test-fixture/build_ios_fixture.py
python3 scripts/ipa_scan.py <audit_id> test-fixture/ios-fixture/fixture.ipa
python3 scripts/aggregate_findings.py <audit_id>
python3 scripts/pass_a_verify.py    <audit_id>
python3 scripts/synthesize.py       <audit_id>
python3 scripts/render_report.py    <audit_id>
```

## Why not `npm install` first?

The static and config stages don't need `node_modules`. The bundle stage does
(it runs `expo export`); if you skip `npm install`, the bundle stage will
emit a single `tooling.bundle_export_failed` finding and the rest of the
pipeline continues — that's the same fail-soft path real audits take when a
project can't install.

## Files in this directory

```
test-fixture/
├── README.md                 (this file)
├── expected.md               regression checklist — rule IDs that must fire
├── app/                      Expo app source with planted frontend issues
├── app.json
├── package.json
├── tsconfig.json
├── backend/                  Stage 4f fixture
│   ├── server.py             FastAPI app with 9 backend + 1 database plant
│   └── helpers.js            JS helpers with 2 algorithm + 1 fetch-chain plant
└── build_ios_fixture.py      Produces ios-fixture/fixture.ipa on demand
                              (the .ipa itself is gitignored — regenerate as needed)
```
