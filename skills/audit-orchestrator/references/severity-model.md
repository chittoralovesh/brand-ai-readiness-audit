# Severity model

One policy, applied by the orchestrator to every finding from every skill.
Analyzer skills never grade their own concern — a specialist that graded itself
would rate its own subject the most important thing on the site, and severities
would stop being comparable.

The executable version is `../scripts/auditlib/severity.py`. If this document
and that file ever disagree, **the file is right and this document is a bug**.

---

## 1. What each skill reports

A `FAIL` check supplies three orthogonal facts and nothing else.

### `impact_base` — 0 to 4

How much damage the defect can do *when it applies at all*. Fixed per check,
chosen from the mechanism, not from how common the defect is.

| Value | Meaning | Examples |
|---|---|---|
| **4** | Blocks machine access to the site, or destroys the primary path for a visitor. Nothing downstream can work. | Entry URL unreachable · bot wall on most pages · `robots.txt` disallowing search crawlers at root · a 5xx `robots.txt` (treated as disallow-all) · site-wide `noindex` |
| **3** | Prevents discovery, extraction, citation or identity resolution of core facts, or breaks a major visitor journey. | Content only exists after JavaScript · no structured data anywhere · no machine-readable identity · entry page never says what the site offers · no dates on time-sensitive pages · cross-page canonical collapse · no primary action on the entry page |
| **2** | Meaningful degradation with a workaround, or narrower scope. | Redirect chains · broken internal links · missing canonical · dead-end content pages · unlabelled forms · thin fact density · stale content |
| **1** | Localised or marginal improvement. | Missing meta description · missing `<html lang>` · no `@id` on entities · no breadcrumbs · stale copyright year · sitemap absent from `robots.txt` |
| **0** | Informational only. Never becomes a finding. | — |

### `breadth` — `{affected, applicable, unit}`

How much of the *observed sample* the defect touches. `applicable` is the
denominator after applicability gating: pages that could not have the defect are
excluded, not counted as passes.

`unit` is `pages`, `links`, `images`, `sitemap URLs`, `forms`, `signals`,
`attributes`, `routes`, or `sites`.

Use `unit: sites` with `1/1` for genuine site-level policy facts — a
`robots.txt` rule is not "2 of 22 crawlers broken", it is one deliberate
configuration. Extent there is carried by `impact_base` instead. Choosing a
denominator that flatters or exaggerates a finding is the easiest way to corrupt
this model, so the denominator is always printed in the evidence string.

### `confidence` — `high` / `medium` / `low`

How strong the evidence is *for the conclusion drawn*, not how confident the
author feels.

| Level | Use when |
|---|---|
| `high` | Directly observed, unambiguous, and the mechanism follows necessarily. A `noindex` tag is a `noindex` tag. |
| `medium` | Directly observed but inferred through a heuristic, or the sample may not represent the site. Nav-consistency, orphan pages, entity ambiguity. |
| `low` | Bounded observation that cannot establish absence. Off-site coverage from a handful of searches. |

---

## 2. The arithmetic

```
breadth_adj  = +1  if ratio >= 0.80                       (site-wide)
                0  if 0.30 <= ratio < 0.80                (partial)
               -1  if ratio < 0.30                        (isolated)
                0  if unit == "sites" or applicable <= 1   (not a proportion)

confidence_adj = 0 (high) | -1 (medium) | -2 (low)

score = clamp(impact_base + breadth_adj + confidence_adj, 0, 5)
```

| Score | Severity |
|---|---|
| ≥ 4 | `critical` |
| 3 | `high` |
| 2 | `medium` |
| ≤ 1 | `low` |

`applicable <= 1` gets no breadth bonus on purpose: "1 of 1" is not evidence of
site-wide breadth, it is the only observation that could have been made.

### The confidence cap

```
confidence == "low"    -> severity may not exceed medium
confidence == "medium" -> severity may not exceed high
```

This is the most important false-positive control in the report. **A finding
with weak evidence may never be presented as an emergency, however large its
theoretical impact.** It bounds the cost of being wrong: the worst a weak
inference can do is occupy a medium slot.

When a cap applies it is stated in `severity_rationale`, so a reader can see
that the raw score was higher and why it was held down.

### Worked examples

| Check | impact | breadth | conf. | score | severity |
|---|---|---|---|---|---|
| `noindex` on 6/6 sampled pages | 4 | +1 (1.00) | high | 5 | **critical** |
| Render gap on 4/6 pages | 4 | 0 (0.67) | high | 4 | **critical** |
| No structured data, 12/12 pages | 3 | +1 (1.00) | high | 4 | **critical** |
| AI crawlers blocked at root | 3 | 0 (site) | high | 3 | **high** |
| No canonical, 6/6 pages | 2 | +1 (1.00) | high | 3 | **high** |
| Thin content, 5/6 pages | 3 | +1 (1.00) | medium | 3 | **high** |
| Broken link, 1/5 links | 2 | −1 (0.20) | high | 1 | **low** |
| Entity ambiguity, 3/5 attributes absent | 2 | 0 (0.60) | medium | 1 | **low** |
| Thin off-site coverage | 2 | 0 (site) | low | 0 | **low** |

---

## 3. Remediation priority

`suggested_action.priority` tracks `severity`, with one narrow exception:

```
severity == medium AND effort == low AND breadth ratio >= 0.80 AND applicable > 1
    -> priority = high
```

"Cheap and everywhere" genuinely belongs at the top of a queue. The exception is
kept narrow deliberately: an earlier revision promoted *every* low-effort medium
finding and ended up marking most of the report `high`, which destroys the
signal the field exists to carry.

`priority_rank` (1..N) is the full ordering, and is what a team should work
down. It sorts by:

```
severity_score desc, severity, effort asc, pillar, check_id, first URL, title
```

Effort ranks *after* severity, so a cheap fix never outranks a serious defect —
it only breaks ties within a severity band.

---

## 4. Determinism

Everything above is a pure function of the check results. IDs are assigned only
*after* the sort, so an unchanged site produces `F-001…F-0NN` in the same order
on every run. Nothing samples randomly, and no severity depends on wall-clock
time, iteration order or the number of findings around it.

---

## 5. What never becomes a finding

- Any status other than `FAIL`. `PASS`, `NOT_APPLICABLE`, `NOT_EVALUATED` and
  `INCONCLUSIVE` go to `coverage` and `limitations`.
- Anything without an observation behind it. **No evidence, no finding.**
- Subjective preference — colour, tone, layout taste, word choice.
- A check whose applicability gate did not match. The gate is reported instead,
  so a reader can see the expectation was considered and correctly dropped.
