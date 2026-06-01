# Repair the failing Maestro flow

A Maestro dry-run reported one or more failing steps. Your job is to patch the structured `flow_intent` JSON so the next run succeeds — without re-introducing the bug that caused the failure.

**You never write YAML.** Your output is a single JSON object matching `schemas/flow_intent.schema.json`, written to `flows/repaired_intent.json`.

## Inputs

From `flows/repair_inputs.json`:

- `current_intent`: the flow intent that was just run. Start from this; do not rebuild from scratch.
- `validation_results`: per-step `status` (`PASS` / `FAIL`) with a short failure `message` from Maestro.
- `ui_dumps`: a handful of UI hierarchy XML dumps captured by Maestro at the moment each step failed. Element labels and text values are inside `<UIHierarchy>` / `<accessibilityLabel>` / `<text>` / `<resource-id>` nodes.

## Procedure

For each step where `status == "FAIL"`:

1. **Open the corresponding UI dump.** Look for an element whose visible text, accessibility label, or content-description matches the *intent* of the original step (a button to log in, a tab to open Settings, a row to tap on a list).

2. **Patch the step's `label` / `text`** to match an attribute that's actually present in the dump. Prefer in order:
   - The element's visible text (between `>...<` or in `text=` / `text:` attributes).
   - The element's `accessibilityLabel`.
   - The element's `resource-id` (rare on RN apps but valid as a fallback).

3. **If no matching element exists in any dump**, the step is unreachable on this app. Remove that step from `steps`. Removing a step is always preferable to leaving a step that will fail every run.

4. **Do NOT add new step kinds.** Keep all steps within the `kind` enum defined in `flow_intent.schema.json`.

5. **Keep `optional` semantics.** Patched steps stay `required: false`. Only the initial `launch` should be `required: true`.

6. **If a `login` step failed, do not loop:** try once with the corrected labels. If the UI dump shows no email/password fields at the cited point in the run, set `login.required = false` and remove the login steps — assume the app is in a no-login state (e.g. demo mode).

## Hard constraints

- **Single JSON object output.** No prose, no YAML, no diff format. Just the full repaired intent matching the schema.
- **Only use labels that appear in the UI dumps.** Never re-introduce labels that just failed.
- **Inherit `app_id` / `platform_overrides` from `current_intent`.** Do not change the app target.
- **One repair attempt.** If your patched intent fails validation again, the orchestrator will accept partial coverage rather than loop.

Write the repaired intent to `flows/repaired_intent.json`. The orchestrator will call `repair_flow_with_llm.py --render` to produce the updated `main.yaml`.
