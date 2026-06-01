# Synthesize prompt — slot-fill only

This prompt is consumed by `scripts/synthesize.py`. The LLM's job is **slot-fill only**: it produces the `<<PROSE>>` text that goes into the report template. It does NOT compute counts, rank findings, select Top-N, or decide what gets into the report — those are deterministic Python decisions made before this prompt runs.

The LLM receives, as inputs:
- `evidence.json` — pre-filtered to REAL findings only, already ranked, already grouped by severity/category. Synthesis has computed `top_n_ids`, `count_label` per rule, and `category_counts`.
- `audit_facts.json` — pinned codebase facts (full file).
- `report_slots.json` — the populated `{{slot}}` map (counts, file lists, function lists, metric values).
- `report_skeleton.md` — the markdown skeleton with `{{slot}}` markers already filled, but `<<PROSE>>` regions empty (placeholder text).
- `references.md` — read first; applies throughout.

Your job:

1. Read `references.md` in full. Internalize §1 (front-matter rules), §4 (glossary), §7 (forbidden patterns).

2. Read `audit_facts.json` to understand the codebase shape. Treat the facts as ground truth for any negative claim you write.

3. For each `<<PROSE: instruction>>` region in `report_skeleton.md`, write the prose that fills that region. Stay within the instruction's scope. Use natural language; never quote JSON paths or field names.

4. For each REAL finding in the body, write the `Evidence`, `Impact`, `In plain terms`, `Actionables`, and `After fixing` paragraphs. The finding's `evidence` object and surrounding context (other findings in the same category, related facts) are your inputs. Cite `file:function`, never `file:line`.

5. For the executive summary's `<<PROSE>>` region, write 2 paragraphs:
   - Paragraph 1: what's working architecturally. Cite `facts.json` fields in natural language. Examples: "Hermes is enabled" (from `project_signature.hermes_enabled`), "the codebase uses react-native-reanimated for animations" (from `dependencies.reanimated_present`), "TypeScript is configured" (from `project_signature.typescript_present`).
   - Paragraph 2: the top concerns. Reference categories and the top findings by file:function. Use the slot-filled counts; never narrate a number.

6. For the "What these mean" plain-language bullets under the metrics dashboard, write one short bullet per metric that has a value.

7. **For each finding marked `top_n: true` in `synthesis_input.json` that exposes a `source_for_fix` block**, also fill the `<rule_id>__fix_diff` region with a **unified diff** that resolves the issue:
   - The `source_for_fix` block contains: the cited file path, the function name, and the actual source lines around `evidence.line` (typically 40 lines of context).
   - Emit a complete unified diff with `--- a/<path>` / `+++ b/<path>` headers and one or more `@@` hunks.
   - Keep the diff minimal — only the lines that need to change, plus 1–3 lines of surrounding context. Don't rewrite the file.
   - If a real fix would touch multiple files (e.g. extract a constant to a sibling module), emit only the primary file's diff and reference the other change in the `Actionables` prose for the same finding. Do NOT emit speculative diffs for files you don't have source for.
   - If the rule genuinely has no code-level fix (e.g. `static.hermes_disabled` — the fix is `app.json`, not a source file), set the `__fix_diff` value to an empty string `""` so the renderer omits the diff block.
   - When in doubt, write a smaller diff. The reader will apply it manually; a precise 5-line change beats a hopeful 50-line refactor.

8. Apply §7 forbidden patterns before finalizing any sentence. If a sentence would violate one of them, rewrite it.

Output format:
- Return a single JSON object: `{ "prose_fills": { "<region_id>": "<the prose text>", ... } }`.
- `<region_id>` matches the `id` attribute on each `<<PROSE id="...">>` marker in the skeleton.
- The Python rendering step substitutes these strings into the skeleton.
- The response is validated against a JSON schema. If validation fails, the LLM is re-prompted with the validation error. Do not produce malformed JSON.

Do NOT:
- Write counts, file paths, or metric values inside prose unless they're already present in the slot-filled skeleton.
- Reference internal artefacts (`evidence.json`, `facts.json`, `decisions.log`, "Pass A", "Pass C").
- Surface verdict labels (REAL / FP / UNCERTAIN) in any form.
- Make negative claims that aren't backed by a `facts.json` field with the supporting value.
- Use first-person ("we", "our analysis").
- Use agent-vendor language ("Claude", "the agent").
- Speculate about findings the audit didn't measure ("This probably also affects…").

The output goes through `ajv` validation against the response schema before being substituted into the skeleton. Schema-invalid responses are rejected; the LLM is asked again with the validation error attached.
