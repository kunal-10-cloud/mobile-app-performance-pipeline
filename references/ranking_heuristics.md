# Ranking heuristics

This file defines how findings are weighted, ranked, and grouped in the final report. **Loaded by `scripts/synthesize.py` (deterministic Python), not by the LLM.** The LLM never decides ranking — it only writes prose into slots whose ranking has already been computed.

---

## Overall score

The headline score (0–100) shown at the top of the report. Computed deterministically as a **penalty summation over the dedup'd lead findings**, NOT a per-category average.

> **Why not a per-category average?** An average lets categories with *no* findings (which sit at 100) wash out the categories that actually have problems. On a real audit, 28 REAL findings concentrated in two categories scored 90/EXCELLENT — clearly wrong. Summation makes total finding burden drive the headline.

```
overall_score = clamp(0, 100, round(100 - Σ_leads penalty(lead)))

penalty(lead) =
    severity_penalty(lead.severity)
  * confidence_multiplier(lead.confidence)
  * coverage_factor(lead)

coverage_factor(lead) = min(2.0, 1 + 0.15 * (distinct_files(lead.id) - 1))
```

- Scored over **lead** findings (one per rule after dedup), so a rule firing 13× across 5 files is one lead — but its `coverage_factor` makes it hurt more than a single-site rule.
- A single rule can't sink the whole score (coverage capped at 2.0); cumulative burden can.
- `config` and `tooling_error` findings don't contribute. `tooling_error` (a worker failed; results partial) belongs in a "Limitations" note, not as a perf issue.

### Per-category score (breakdown table only)

Each category still gets its own score for the per-category breakdown table — this is informational and **independent** of the overall (the overall is the summation above, not a roll-up of these):

```
category_score(c) = max(0, 100 - sum(
    severity_penalty(f.severity) * confidence_multiplier(f.confidence)
    for f in leads if f.category == c
))
```

### Severity penalties

| Severity  | Penalty |
|-----------|--------:|
| critical  | 30      |
| high      | 15      |
| medium    | 6       |
| low       | 2       |
| info      | 0       |

### Confidence multipliers

The worker's `confidence` field scales the penalty so high-confidence findings hit the score harder than low-confidence ones.

| Confidence | Multiplier |
|------------|-----------:|
| high       | 1.00       |
| medium     | 0.60       |
| low        | 0.30       |

### Score grading

| Score range | Grade        | Emoji |
|------------:|--------------|:-----:|
| 90–100      | EXCELLENT    | 🟢    |
| 75–89       | GOOD         | 🟢    |
| 50–74       | NEEDS WORK   | 🟡    |
| 0–49        | POOR         | 🔴    |

---

## Per-finding ranking

After Pass A, the synthesis step ranks REAL findings to populate the "Top N" list and the per-finding order within each severity bucket.

```
rank_weight(f) =
    severity_weight(f.severity) * 100 +
    confidence_weight(f.confidence) * 10 +
    coverage_weight(f) +
    layer_weight(f.layer)
```

### Severity weight

| Severity  | Weight |
|-----------|-------:|
| critical  | 5      |
| high      | 4      |
| medium    | 3      |
| low       | 2      |
| info      | 1      |

### Confidence weight

| Confidence | Weight |
|------------|-------:|
| high       | 3      |
| medium     | 2      |
| low        | 1      |

### Coverage weight

How widespread the issue is — a `medium`-severity finding that affects 20 files outranks the same finding affecting 1 file.

```
coverage_weight(f) =
    min(5, distinct_files_with_rule(f.id))
```

Capped at 5 to prevent one widespread low-confidence rule from dominating.

### Layer weight

Where the evidence came from. Direct measurement (device, reassure) outranks inference (static, bundle) at equal severity / confidence because measured ground-truth is more credible than predicted ground-truth.

| Layer          | Weight |
|----------------|-------:|
| device_android | 4      |
| device_ios     | 4      |
| reassure       | 3      |
| bundle         | 2      |
| static         | 1      |
| tooling        | 0      |

---

## Top-N selection

The report's "Top highest-impact findings" section lists the top 5 findings by `rank_weight`, with ties broken by `evidence.metric_value` (when present, larger metric = higher rank) then alphabetically by `id`.

---

## Category grouping

Findings within each severity bucket (CRITICAL / HIGH / LOW) are grouped by `category` in this display order: `startup`, `runtime_jank`, `bundle_size`, `memory`, `code_quality`, `config`. Findings of the same severity + category are ordered by `rank_weight` descending.

---

## Dedup heuristics for synthesis

When two REAL findings from different layers point at the same root cause, the synthesis step collapses them into one "lead" finding with the others in its `related_finding_ids` array. Collapse rules:

1. **Same file, overlapping evidence.** A `static.scrollview_with_long_list` finding at `screens/Feed.tsx` and a `device_android.low_fps_on_screen` finding labelled "Feed" → collapse to the device finding as lead (device is higher-rank), `related_finding_ids` includes the static one.

2. **Cause-and-symptom pair.** A `bundle.dependency_oversize: lottie-react-native` finding + a `device_android.long_main_thread_block` finding whose stack trace mentions Lottie → collapse to the device finding as lead.

3. **Same rule, multiple sites in adjacent files.** Multiple `static.inline_arrow_in_renderitem` findings in files sharing a directory → keep separate but increase `coverage_weight` proportionally for ranking.

Never collapse across categories unless a clear cause-and-symptom pair is established. Don't collapse `config.hermes_disabled` with `device.slow_startup` blindly — they're correlated but not necessarily causal.

---

## What the LLM does NOT decide

- Severity (set by the rule that emitted the finding).
- Confidence (set by the worker that produced the finding).
- Category counts (computed in `synthesize.py` from `evidence.summary`).
- Top-N selection (`rank_weight` is deterministic).
- Overall score (formula above).
- Per-finding ordering within sections.

The LLM only writes:
- The `description` (Impact) paragraph for each finding, when the rule's default description is too generic.
- The plain-terms analogy.
- The `suggested_fix.summary` and `suggested_fix.diff` for top-ranked findings (Slice 4+).
- The executive summary's prose (1–2 paragraphs).

Everything numerical, every citation, every count flows from `evidence.json` + `audit_facts.json` through `synthesize.py` into slot-fills.
