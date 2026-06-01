# Expected audit findings

Rule IDs that must appear in `evidence.json` with `verdict: REAL` after a
clean pipeline run against this fixture. Used as the regression checklist
when modifying any rule, schema, prompt, or worker script.

## Static (must all be REAL)

- [ ] `static.scrollview_with_long_list`         × 1 — `app/(tabs)/index.tsx — FeedScreen`
- [ ] `static.image_without_caching`             × 1 — `app/(tabs)/index.tsx — FeedScreen`
- [ ] `static.inline_arrow_in_renderitem`        × 1 — `app/(tabs)/index.tsx — FeedScreen`
- [ ] `static.useeffect_no_deps`                 × 1 — `app/(tabs)/index.tsx — FeedScreen`
- [ ] `static.useeffect_missing_cleanup`         × 1 — `app/(tabs)/index.tsx — FeedScreen` (setInterval without clearInterval cleanup)
- [ ] `static.console_log_in_production_code`    × 1 — `app/(tabs)/index.tsx — FeedScreen`
- [ ] `static.animated_api_usage`                × 1 — `app/(tabs)/index.tsx — <module>`
- [ ] `static.inline_object_props`               × ≥ 2 — `FeedScreen` (contentContainerStyle, search input style, etc.)
- [ ] `static.large_unmemoized_component`        × 1 — `app/(tabs)/profile.tsx — ProfileScreen`
- [ ] `static.hermes_disabled`                   × 1 — `app.json` (severity: critical)
- [ ] `static.new_architecture_disabled`         × 1 — `app.json` (severity: medium)

## Bundle (only if `npm install` + `expo export` succeed)

- [ ] `bundle.known_bloated_dependency`          × 3–4 — moment, lodash, axios, (vector-icons would be 5th)
- [ ] `bundle.duplicate_dependency_libs`         × 1 — lodash + lodash-es

## Backend / DB / algorithm (Stage 4f — must all be REAL)

Driven by `test-fixture/backend/server.py` + `test-fixture/backend/helpers.js`.
Copy `test-fixture/backend/` into the audit run's `workspace/backend/` before
running `backend_scan.py`.

- [ ] `backend.sync_route_handler`               × 2 — `backend/server.py` (one HIGH on `list_users` — hot-path name; one MEDIUM on `admin_cleanup`)
- [ ] `backend.n_plus_one_query`                 × 1 — `backend/server.py — aggregate_orders`
- [ ] `backend.unbounded_query`                  × 1 — `backend/server.py — list_all_products` (`to_list(length=None)`)
- [ ] `backend.mongo_client_not_singleton`       × 1 — `backend/server.py — get_cart` (severity: critical)
- [ ] `backend.blocking_work_in_handler`         × 1 — `backend/server.py — notify` (stripe call inline)
- [ ] `backend.sequential_await_chain`           × 1 — `backend/server.py — dashboard` (4 sequential awaits, distinct vars)
- [ ] `backend.no_projection_on_query`           × ≥ 5 — `backend/server.py` (capped at 10 per audit)
- [ ] `backend.pydantic_complex_model`           × 1 — `backend/server.py — UserProfile` (3+ Optional/List fields)
- [ ] `database.missing_index`                   × 1 — `backend/server.py — lookups` (fields queried but no create_index)
- [ ] `algorithms.nested_iteration`              × 1 — `backend/helpers.js — findMatches`
- [ ] `algorithms.linear_array_lookup_in_loop`   × 1 — `backend/helpers.js — flagSelected`
- [ ] `backend.sequential_fetch_chain`           × 1 — `backend/helpers.js — loadDashboard` (3 consecutive awaits on fetch)

## iOS IPA scan (Stage 4c′ — must all be REAL)

Build the fixture IPA first: `python3 test-fixture/build_ios_fixture.py`. The
IPA itself is not committed; it's regenerated on demand. Then run
`scripts/ipa_scan.py <audit_id> test-fixture/ios-fixture/fixture.ipa`.

- [ ] `bundle.shipped_bundle_size_ios`           × 1 — informational; reports ~3.19 MiB
- [ ] `bundle.bundle_too_large_warning_ios`      × 1 — bundle ≥ 2 MiB warning band
- [ ] `bundle.ipa_install_footprint`             × 1 — informational
- [ ] `bundle.ipa_native_framework_count`        × 1 — 1 framework (Hermes.framework)
- [ ] `bundle.ipa_privacy_manifest_missing`      × 1 — fixture intentionally omits `PrivacyInfo.xcprivacy` (severity: high)

## Reassure (only if reassure install + jest-expo succeed)

The fixture's screens are simple — Reassure should not flag
`reassure.excessive_render_count` or `reassure.excessive_render_duration`
here. What you SHOULD see is a `reassure.render_failure` for the Feed screen
(because the mocks in the template don't include the few RN modules the
fixture uses by accident). The fixture stays modest on purpose to keep this
file short.

## Screen map (device stages only)

- [ ] `flows/screen_map.json` reports:
  - `navigation.type == "expo-router-tabs"`
  - Two tabs: `index` (label "Feed"), `profile` (label "Profile")
  - `auth.detected == true` with `login_screen == "app/login.tsx"`
  - `auth.email_field_label == "Email"`, `auth.password_field_label == "Password"`, `auth.submit_label == "Log in"`
  - `scrollable_screens` includes `app/(tabs)/index.tsx`
  - `bundle_id_android == "com.audit.testfixture"`, `bundle_id_ios == "com.audit.testfixture"`

## Report-level invariants

- `report.md` is emitted to stdout between `===MOBILE_PERF_AUDIT_REPORT_START===` and `===MOBILE_PERF_AUDIT_REPORT_END===`.
- Severity table includes a non-zero CRITICAL count (driven by `static.hermes_disabled` and `backend.mongo_client_not_singleton`).
- "What's working well" table is empty or near-empty (Hermes off, no FlashList, no expo-image).
- Per-category breakdown includes rows for `backend_perf`, `database`, and `algorithms` (Stage 4f categories).
- No mention anywhere in `report.md` of: "verified", "uncertain", "Pass A", "evidence.json", "facts.json", "false positive".
- Overall score should be in the POOR or NEEDS WORK band (under 75 / 100).
