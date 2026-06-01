# Refine the draft Maestro flow

You are filling a structured JSON contract (`schemas/flow_intent.schema.json`) so that a deterministic Python renderer can emit a Maestro YAML for performance measurement. **You never write YAML.** Your output is a single JSON object matching the schema.

## Inputs

You have been given (via `flows/refine_inputs.json`):

- `screen_map`: the structured navigation surface extracted from the workspace.
- `draft_intent`: a baseline flow intent the deterministic generator produced. Tabs only, no login, generic interactions.
- `screen_sources`: the actual source of (a) the detected login screen if any, and (b) the first tab's screen. Use these to pick *real* button labels and placeholder strings.

## Your job

Produce one JSON object matching `schemas/flow_intent.schema.json` and write it to the `response_target_path` from the inputs (`flows/refined_intent.json`).

**Start from `draft_intent`**, then:

1. **Login pre-flight.** If `screen_map.auth.detected == true`, set `login.required = true` and fill `email_field_label`, `password_field_label`, `submit_label` using the *visible* labels from the login screen source (not the variable names). If the labels in `screen_map.auth.*` already look right, reuse them. Use the default `email_value` / `password_value`.

2. **Realistic per-tab interaction.** After each `tap_label` for a tab, add ONE of:
   - `tap_list_item` with `index: 0` — if the first tab screen contains a `<FlatList>`/`<FlashList>`/`<SectionList>`/scrollable map of items.
   - `type_search` with a short query — if the first tab screen contains a `TextInput` with `placeholder` text matching `search` or `find`.
   - `open_detail_then_back` — if the tab screen renders cards / list items that navigate on tap.
   - Skip the per-tab interaction entirely for tabs whose source you do NOT have — never invent labels.

3. **Keep every step optional.** Do NOT set `required: true` on any step except `launch`. A mis-guessed label must NOT abort the run; optional steps let Maestro continue past them.

4. **Add `wait_for_animation` between major transitions.** Each tab tap and each detail navigation should be followed by `wait_for_animation` (200–1500 ms) so screen-load work is captured in the metrics, not the navigation transition itself.

5. **`post_login_assertions`** — when login is set, populate this with one or two labels you expect to see after login completes (e.g. the first tab's label, or "Home"). Used as a heartbeat so a silent login bounce surfaces immediately.

## Hard constraints

- **No labels you didn't see.** Every `label` / `text` / placeholder string must appear verbatim in either `screen_map` or one of the `screen_sources`. If you don't have evidence, omit that step.
- **No fields outside the schema.** Extra fields will be rejected and the script will fall back to the draft.
- **Output a single JSON object, nothing else.** No YAML, no markdown, no preamble. Just `{...}`.
- **Inherit `app_id` and `platform_overrides` from the draft.** Do not invent new bundle IDs.

## Example shape (illustrative — do not copy literally)

```json
{
  "app_id": "com.example.app",
  "platform_overrides": { "android_app_id": "com.example.app", "ios_app_id": "com.example.app" },
  "login": {
    "required": true,
    "email_field_label": "Email",
    "password_field_label": "Password",
    "submit_label": "Sign in",
    "email_value": "test@example.com",
    "password_value": "Test1234!"
  },
  "steps": [
    { "kind": "launch", "required": true, "screen_label": "Launch" },
    { "kind": "wait_for_animation", "duration_ms": 3000 },
    { "kind": "tap_label", "label": "Home", "screen_label": "Tab: Home" },
    { "kind": "wait_for_animation", "duration_ms": 800 },
    { "kind": "scroll", "scroll_count": 3, "screen_label": "Scroll Home" },
    { "kind": "open_detail_then_back", "index": 0, "screen_label": "Open first card on Home" },
    { "kind": "tap_label", "label": "Search", "screen_label": "Tab: Search" },
    { "kind": "wait_for_animation", "duration_ms": 800 },
    { "kind": "type_search", "label": "Search", "text": "coffee" },
    { "kind": "wait_for_animation", "duration_ms": 600 },
    { "kind": "tap_label", "label": "Profile", "screen_label": "Tab: Profile" }
  ],
  "post_login_assertions": ["Home"],
  "metadata": { "source": "llm_refined" }
}
```

Write the refined intent to `flows/refined_intent.json`. The orchestrator will re-invoke `refine_flow_with_llm.py --render` to produce `main.yaml` from your output.
