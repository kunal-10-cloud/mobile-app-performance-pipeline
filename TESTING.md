# Testing the mobile-perf-audit pipeline

This document is for **someone iterating on the pipeline itself** — not for end users running an audit against a real Expo app.

The goal of testing here is to confirm that, when the pipeline is pointed at known input, it produces the known output described in the test fixture's `expected.md`. Anything that drifts is a regression worth investigating.

---

## Prerequisites

| Required | How to get it |
|---|---|
| Python 3.11+ | `python --version` |
| Node 18.18+ | `node --version` |
| Python deps | `python -m pip install -r requirements.txt` |
| Node deps for ESLint | `npm install` (inside the repo root) |

**Optional**, only for Slice 2+:
- Working network access (the bundle stage installs the test fixture's deps and runs `expo export`).

**Optional**, only for Slice 3:
- macOS with Xcode, Android emulator (`emulator -avd ...`), Maestro CLI, Flashlight CLI, EAS CLI.

---

## Smoke test — Slice 1 (static-only, no install needed)

```bash
bash scripts/run_local_audit.sh test-fixture --slice 1 --skip-install
```

That runs:
1. `init_audit.sh` — creates `.audit-runs/local-<timestamp>/`
2. `ingest_pod.py --gather-local` — copies `test-fixture/` into the workspace (respects the denylist)
3. `gather_facts.py` — produces `facts/audit_facts.json`
4. `static_scan.py` + `config_scan.py` — produce `findings/static.json` + `findings/config.json`
5. `aggregate_findings.py` — merges into `findings/all_findings.json`
6. `pass_a_verify.py` — stamps verdicts → `evidence/evidence.json` + `decisions.log`
7. `synthesize.py` — produces `report/report.json` + `report/synthesis_input.json`
8. `render_report.py` — produces `report/report.md` and emits it between stdout fences

### Expected output (from `test-fixture/expected.md`)

After Slice 1 the static findings should include all 10 of the planted anti-patterns:

```
static.scrollview_with_long_list         × 1
static.image_without_caching             × 1
static.inline_arrow_in_renderitem        × 1
static.useeffect_no_deps                 × 1
static.console_log_in_production_code    × 1
static.animated_api_usage                × 1
static.inline_object_props               × 2 (minimum)
static.large_unmemoized_component        × 1
static.hermes_disabled                   × 1   (severity: critical)
static.new_architecture_disabled         × 1
```

### Quick sanity checks

```bash
# Where the run lives
ls .audit-runs/

# How many REAL findings did Pass A keep?
python -c "import json; d=json.load(open('.audit-runs/local-XXX/evidence/evidence.json')); print('REAL:', sum(s['real'] for s in d['summary'].values()))"

# Were all 10 rule IDs surfaced?
grep -oE 'static\.[a-z_]+' .audit-runs/local-XXX/decisions.log | sort -u

# Verify Rule 1.8 — none of these strings should appear in report.md
grep -E 'verdict|UNCERTAIN|FP|Pass A|evidence\.json|false positive' .audit-runs/local-XXX/report/report.md && echo "REGRESSION: scaffolding leaked into report" || echo "OK"
```

---

## Smoke test — Slice 2 (bundle + reassure)

Requires the fixture to actually install:

```bash
cd test-fixture && npm install && cd ..
bash scripts/run_local_audit.sh test-fixture --slice 2
```

Expect additionally:
- `bundle.known_bloated_dependency` × ~3 (moment, lodash, axios)
- `bundle.duplicate_dependency_libs` × 1 (lodash + lodash-es)
- Possibly `bundle.bundle_too_large_warning` once Expo's runtime bundle stacks up
- Reassure findings will likely include a `reassure.render_failure` for the Feed screen (mocks are minimal); that's expected.

If `expo export` fails for any reason, you should see exactly one `tooling.bundle_export_failed` finding and the rest of the pipeline should still complete.

---

## Smoke test — Slice 3 (device measurement)

Skipped on non-macOS hosts. On macOS with everything installed:

```bash
bash scripts/run_local_audit.sh test-fixture --slice 3
```

This invokes `device_perf.sh`, which:
- Calls `build_app.sh` for APK + IPA (likely fails on the fixture unless `eas.json` exists; that's fine — surfaces as a tooling finding).
- Calls `extract_screen_map.py` (this should succeed regardless — pure tree-sitter walk).
- Generates `flows/draft.yaml` and `flows/draft_intent.json`.
- Calls `refine_flow_with_llm.py --prepare` — produces `flows/refine_inputs.json` for an LLM to consume. If no LLM is in the loop, `main.yaml` stays = `draft.yaml`.

Validate that `flows/screen_map.json` matches the **Screen map** checklist in `test-fixture/expected.md`:

```bash
python -c "
import json
m = json.load(open('.audit-runs/local-XXX/flows/screen_map.json'))
print('navigation.type:', m['navigation']['type'])
print('tabs:', [t['route'] for t in (m['navigation'].get('tabs') or [])])
print('auth.detected:', m['auth']['detected'])
print('auth.email_field_label:', m['auth'].get('email_field_label'))
print('android pkg:', m['bundle_id_android'])
"
```

---

## Smoke test — Slice 4 (fix-diff round-trip)

The fix-diff slot is populated only when the LLM round-trip runs. To exercise it:

1. Run Slice 1 as above.
2. Open `.audit-runs/local-XXX/report/synthesis_input.json`.
3. For each top-N finding with `__synth_meta.source_for_fix` set, write a unified diff into `report/prose_fills.json` under the key `<rule_id>__fix_diff`.
4. Re-run `render_report.py local-XXX` — the diff appears in the rendered report under "Suggested fix" inside each top-N finding section.

Manual example (no LLM):

```bash
mkdir -p .audit-runs/local-XXX/report
cat > .audit-runs/local-XXX/report/prose_fills.json <<'EOF'
{
  "prose_fills": {
    "static.hermes_disabled__fix_diff": "",
    "static.scrollview_with_long_list__fix_diff": "--- a/app/(tabs)/index.tsx\n+++ b/app/(tabs)/index.tsx\n@@ -38,7 +38,7 @@\n-    <ScrollView\n-      contentContainerStyle={{ paddingTop: 16, paddingHorizontal: 12 }}\n-    >\n+    <FlatList\n+      data={filtered}\n+      keyExtractor={(it) => it.id}\n+      contentContainerStyle={feedStyles.container}\n+      renderItem={renderItem}\n+    />"
  }
}
EOF
python scripts/render_report.py local-XXX
```

The rendered report should now show the diff as a fenced code block under the `static.scrollview_with_long_list` finding.

---

## Regression checklist

After making changes to any rule, prompt, schema, or script, run:

```bash
# 1) Clean previous runs to avoid stale state confusion
rm -rf .audit-runs/

# 2) Slice 1 smoke
bash scripts/run_local_audit.sh test-fixture --slice 1 --skip-install

# 3) Diff against expected rule IDs
grep -oE 'static\.[a-z_]+' .audit-runs/local-*/decisions.log | sort -u > /tmp/actual.txt
diff <(grep -oE '^\- \[ \] `static\.[a-z_]+`' test-fixture/expected.md | grep -oE 'static\.[a-z_]+' | sort -u) /tmp/actual.txt
```

A clean `diff` (no output) means every planted anti-pattern was caught and no new IDs leaked through.

---

## What's NOT tested here

These are deliberate gaps; document them when discovered, don't surprise yourself with them later:

- **End-to-end MCP path** (`ingest_pod.py` against real envcore_gateway) — the local runner skips this. Test in a real session.
- **Cross-LLM equivalence** — same audit under different LLM sessions should yield identical `evidence.json` / `facts.json`. Manual check; no automated test today.
- **Bundle + reassure regression coverage** — the fixture only exercises a few cases. Real apps will surface edge cases the fixture can't.
- **Device measurement on Linux** — the device stages always degrade to skipped on Linux; nothing to test until you have macOS+emulators.
- **EAS Build failure modes** — fixture has no `eas.json`, so `build_app.sh` always fails with `tooling.eas_config_missing`. Real apps with broken eas.json will surface different paths.

---

## How to add a new AST rule and its fixture coverage

1. Implement the rule in `configs/ast_rules.py`. Register in `RULES`.
2. Add a §3 entry in `references.md` describing the rule, preconditions, FP shapes, report framing.
3. Add a Pass A verifier in `scripts/pass_a_verify.py` (use `verify_default` if structural-only).
4. Add a planted anti-pattern to `test-fixture/app/...` so the rule fires on this fixture.
5. Add the rule to `test-fixture/expected.md`'s checklist.
6. Run the Slice 1 smoke test; confirm the new rule appears in `decisions.log`.

Same flow for bundle / reassure / device rules — just swap the file you edit.
