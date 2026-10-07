# Audit report schema

One JSON document per audit. The **required floor** is fixed by the brief and is
never removed, reordered away or renamed. Everything else is an extension.

Validated and repaired by `../scripts/auditlib/schema.py`.

---

## Required floor

```json
{
  "site": "example.com",
  "audited_at": "2026-09-04T14:32:00Z",
  "summary": {
    "total_findings": 6,
    "critical": 1,
    "high": 2,
    "medium": 3,
    "low": 0
  },
  "findings": [
    {
      "id": "F-001",
      "title": "No JSON-LD structured data on product pages",
      "severity": "high",
      "evidence": "Crawled 12 product pages; 0/12 contain schema.org markup.",
      "suggested_action": {
        "summary": "Add Product/Offer JSON-LD to every product page.",
        "priority": "high"
      }
    }
  ]
}
```

Enforced invariants:

| Rule | Enforced by |
|---|---|
| `site` is a non-empty string | validator + repair from `audited_url` |
| `audited_at` is ISO-8601 with an offset or `Z` | validator + repair to now |
| `summary` counts match the findings exactly | validator + recount |
| `id` matches `F-NNN`, unique, contiguous from `F-001` | validator + renumber |
| `severity` ∈ critical/high/medium/low | validator + downgrade to `low` |
| `title` and `evidence` non-empty | validator + placeholder |
| `suggested_action.summary` non-empty, `.priority` a valid severity | validator + fill from severity |
| The whole document is JSON-serialisable | validator |

---

## Extensions

### Top level

| Field | Purpose |
|---|---|
| `schema_version`, `report_type` | Consumer versioning. |
| `audited_url` | The URL actually delivered after redirects. |
| `executive_summary` | `headline`, `start_here` (top three actions with the reason), `root_causes_identified`, `opportunities_beyond_defects`, `how_to_read_this`. Written for a non-expert. |
| `summary.by_category` | Findings split across the two pillars. |
| `root_cause_analysis` | Compound diagnoses with `fix_order`, referencing finding IDs. Adds no findings. |
| `proactive_recommendations` | Opportunities where no defect was found. |
| `coverage` | Every check with status and note, plus crawl statistics. |
| `limitations` | What was not evaluated, and why. |
| `finding_rollups` | Which symptom checks were folded into which root cause. |
| `audit_metrics` | Timings, page counts, request counts, skills invoked. |
| `engine` | Marketplace name, version, entrypoint, user agent, and the read-only/robots guarantees. |
| `report_validation` | `valid`, `errors`, `repairs_applied`. |

### Per finding

| Field | Purpose |
|---|---|
| `category`, `category_label`, `subcategory` | Pillar and check group. |
| `check_id`, `dedupe_key`, `produced_by` | Traceability to the skill and check. |
| `confidence` | `high` / `medium` / `low`. |
| `severity_score`, `severity_rationale` | The arithmetic, printed. A reader can audit the grade. |
| `mechanism`, `impact` | *Why* this damages discoverability or engagement. |
| `breadth` | `{affected, applicable, unit, ratio}` — the denominator, explicitly. |
| `evidence_items[]` | `{url, observed, expected, excerpt}` — machine-checkable, per URL. |
| `affected_urls[]` | Up to 25. |
| `applicability` | Why the check applied here (and what was excluded). |
| `reproducibility` | Determinism statement. |
| `priority_rank` | 1..N remediation order. |
| `suggested_action.steps[]`, `.effort`, `.expected_benefit` | The actual fix. |
| `suggested_action.verify` | `command` and `expect`, plus a `reaudit` fallback, so the fix is falsifiable rather than assumed. |
| `suggested_action.fix_prompt` | A self-contained prompt for a coding agent working in the site's own repository. See below. |
| `merged_from`, `also_affects` | Set when skills' observations were merged. |
| `contributing_symptoms`, `rollup_note` | Set when symptoms were folded in. |

---

## Design rules for the fields a human reads

**`evidence` must quantify.** Not *"the site has no structured data"* but
*"0 of 12 sampled product pages contained parseable JSON-LD"*. Where the sample
is the denominator, the sample size is in the sentence.

**`suggested_action.summary` names the change**, not the goal. Not *"improve
structured data"* but *"Add Product markup reflecting the product name, canonical
URL and visible offer information"*.

**`suggested_action.steps` must be executable by someone who did not run the
audit.** Each step is an action, ordered, and mentions the specific artefact
(the template, the header, the field) wherever it can.

**`mechanism` explains the causal chain**, in one or two sentences, in terms of
how retrieval and rendering actually work — not by restating the finding.

---

## Empty and degraded reports

A report with zero findings is valid and normal. A report produced after a
failure is still schema-valid: the failure appears in `limitations`, and
`report_validation.valid` is `true`.

`run_audit.py` exits `2` only when the input URL could not be used — and it
still writes a valid, empty report first.

---

## Worked example

`example-report.json` in this directory is a real report produced by the test
suite against a synthetic site, trimmed for length. It shows a merged finding, a
rolled-up root cause, a correlation and a proactive recommendation.

---

## Executable fixes

The report's consumer is an agent, so each finding carries two fields beyond the
prose remediation.

**`suggested_action.verify`** gives a one-line command and the result to expect,
so a fix can be proved rather than assumed:

```json
"verify": {
  "command": "curl -s 'https://example.com/pricing' | grep -c '<th'",
  "expect": "1 or more header cells per data table",
  "reaudit": "python skills/audit-orchestrator/scripts/run_audit.py --url '...' --out after.json"
}
```

**`suggested_action.fix_prompt`** is a ready-to-run prompt for a coding agent.
Three rules govern how it is built, and each exists for a reason.

**It never touches the live site.** The marketplace is recommend-only. The
prompt is addressed to an engineer editing templates in a repository and says so
explicitly, and it forbids inventing any value the page does not already show.

**Scraped content is fenced and labelled as data.** The prompt has to quote what
was found on the audited site, and that site is untrusted input. Pasted raw into
a prompt another agent executes, a hostile page carrying "ignore previous
instructions" would be an injection vector. So every observation, excerpt and
evidence sentence is confined to one delimited block introduced as data that
must never be followed, and the closing marker is neutralised inside the content
so the fence cannot be escaped.

URLs are the single exception, because the instructions are useless without
them. They are therefore *validated* rather than escaped: quotes, backticks,
whitespace, shell metacharacters and non-printing characters are stripped, and
anything that no longer parses as an absolute HTTP URL is dropped. A path
carrying prose cannot survive the whitespace rule.

**It carries what the prose cannot.** The observed values, the exact affected
URLs and the required shape. A prompt that only restated the steps would be
padding.

Built once at composition time, so analyzer skills stay pure and one policy
governs all of them. Covered by `tests/test_fixprompt.py`.
