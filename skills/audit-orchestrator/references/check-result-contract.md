# Check-result contract

What every analyzer skill returns, and what the orchestrator is allowed to
assume. This is the interface that keeps the skills genuinely decoupled: an
analyzer knows nothing about severity, report shape or its sibling skills.

```jsonc
{
  "schema_version": "1.0",
  "producer": "crawl-render-audit",
  "produced_at": "2026-09-04T14:32:10Z",
  "checks": [ /* CheckResult */ ],
  "notes":  ["free-text, shown in coverage"],
  "errors": [{ "check": "check_titles", "error": "TypeError: ..." }]
}
```

Emit this on **stdout** (or `--out`). Exit `0` even when individual checks
failed internally — a non-zero exit is read as "the skill did not run", which
loses every check it *did* complete.

## CheckResult

```jsonc
{
  "check_id": "CR-11",
  "title": "Page content only exists after JavaScript runs",
  "pillar": "ai_discoverability | on_site_engagement",
  "category": "renderability",
  "status": "PASS | FAIL | NOT_APPLICABLE | NOT_EVALUATED | INCONCLUSIVE",

  "impact_base": 4,
  "confidence": "high | medium | low",
  "breadth": { "affected": 4, "applicable": 6, "unit": "pages" },

  "evidence": "4 of 6 sampled pages returned an essentially empty document ...",
  "evidence_items": [
    { "url": "https://example.com/",
      "observed": "31 words in the server HTML; empty mount point #root",
      "expected": "the page's primary content present in the server response",
      "excerpt": "optional short quote" }
  ],
  "mechanism": "Most AI fetchers read the raw HTTP response and do not execute ...",
  "affected_urls": ["https://example.com/"],

  "remediation": {
    "summary": "Serve the primary content in the initial HTML response.",
    "steps": ["...", "..."],
    "expected_benefit": "...",
    "effort": "low | medium | high"
  },

  "dedupe_key": "render-gap",
  "tags": ["render-gap-root-cause"],
  "applicability": "why this check applied, or why it did not",
  "supersedes": []
}
```

## Rules an analyzer must follow

1. **`status` first.** `FAIL` requires an observation. If the precondition is
   absent, emit `NOT_APPLICABLE` and put the gate in `applicability`. If you
   could not observe, emit `NOT_EVALUATED` or `INCONCLUSIVE`. Never convert
   uncertainty into a defect.
2. **Emit a result for every check, every run** — including passes. Coverage is
   part of the report's value: "we checked and it was fine" is different from
   "we could not check".
3. **Never assign severity.** Supply `impact_base`, `breadth` and `confidence`.
4. **Quantify the evidence**, with the denominator in the sentence.
5. **`evidence_items` are machine-checkable.** Each is one URL with what was
   observed and what was expected, so a reader can verify the finding by hand.
6. **`mechanism` explains the causal chain**, not the finding restated.
7. **`remediation` must be executable** by someone who did not run the audit.
8. **Share a `dedupe_key`** with any sibling check observing the same defect.
9. **Isolate checks from each other.** One crashing check must not stop the
   rest; catch per check and record it in `errors`.
10. **Never touch the network.** Analyzers analyse the snapshot.

## `check_id` prefixes

| Prefix | Skill |
|---|---|
| `CR-` | `crawl-render-audit` |
| `AC-` | `ai-citability-audit` |
| `FC-` | `freshness-corroboration` |
| `EN-` | `engagement-audit` |
| `AG-` | agent findings from the optional semantic pass |

IDs are stable: a given `check_id` always means the same check, so reports are
comparable across runs and across sites.

## Tags with meaning to the orchestrator

| Tag | Effect |
|---|---|
| `render-gap-root-cause` | May absorb symptoms when it fails on ≥50 % of the sample. |
| `render-gap-symptom` | Folded into the root cause when that fires. |
| `semantic-review` | Included in the agent-context packet for the optional pass. |
| `indexation-signal` | Considered by the `RC-02` correlation. |
| `blocks-everything` | Marks a defect that invalidates downstream evidence. |

Unknown tags are carried through to the finding and otherwise ignored.
