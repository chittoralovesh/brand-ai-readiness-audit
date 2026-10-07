---
name: audit-orchestrator
description: Audit any public website for the defects that stop AI assistants and search crawlers finding, reading and citing it, and that stop human visitors engaging once they arrive. Performs one bounded, read-only, robots-respecting crawl, invokes the crawl-render-audit, ai-citability-audit, freshness-corroboration and engagement-audit skills over the shared snapshot, then de-duplicates, grades, prioritises and emits a single JSON audit report of evidence-backed findings, each with a prioritised suggested action, a verification command and a ready-to-run fix prompt. Use when asked to audit, diagnose or improve a site's AI discoverability, AI search visibility, citability or on-site engagement - or to explain why a brand is missing, stale or misrepresented in AI assistants. This is the marketplace entrypoint; do not invoke the analyzer skills directly for a full audit.
license: Apache-2.0
compatibility: Requires Python 3.9+ (standard library only) and outbound HTTP access to the site being audited. No API keys, no browser binaries, no database. Optional web-search and page-fetch tools enable off-site corroboration; everything else degrades gracefully without them.
allowed-tools: Bash Read Write WebSearch WebFetch
metadata:
  author: adobe-university-hackathon-2026-round-3
  version: "1.0.0"
  role: entrypoint
  pillars: ai-discoverability, on-site-engagement
---

# Brand AI-readiness audit — orchestrator

The single entrypoint of the `brand-ai-readiness-audit` marketplace. It receives
the audit request, gathers evidence once, composes the analyzer skills' findings,
and emits **one** report.

## When to use

Use when someone asks to audit a website for AI discoverability, AI search
visibility, citability, brand representation in assistants, or on-site
engagement — or asks why a brand is invisible, stale, confused with something
else, or losing visitors who do arrive.

Do **not** invoke `crawl-render-audit`, `ai-citability-audit`,
`freshness-corroboration` or `engagement-audit` directly for a full audit. They
analyse a snapshot this skill produces; running them alone means crawling the
site several times and emitting several partial reports.

## Inputs

| Input | Required | Notes |
|---|---|---|
| `url` | yes | The site or landing page to audit. `example.com` is accepted and normalised to `https://example.com/`. |
| `max_pages` | no | Default 20. |
| `max_depth` | no | Default 2. |
| `budget` | no | Hard internal deadline in seconds. Default 240; design target is under 180. |
| `research` | no | Off-site research JSON for `freshness-corroboration` (see step 4). |

## Safety — this is non-negotiable

The audit is **recommend-only** and **read-only**. It never modifies a site.

- Only `GET` and `HEAD` are ever issued. The HTTP client raises on any other
  method, so no skill in the marketplace can mutate a live site.
- `robots.txt` is obeyed without exception. Disallowed URLs are not fetched;
  the resulting gap is reported as a coverage limitation, never worked around.
- URLs matching login, logout, account, cart, checkout, register, subscribe,
  delete or edit patterns are never requested at all.
- No form is submitted, no credential is sent, no account is created, nothing
  is downloaded beyond HTML and small metadata files.
- Politeness is structural: ≤2 requests/second/host, ≤2 concurrent same-host
  sockets, site-declared `Crawl-delay` honoured, 8 s request timeout, one retry,
  2 MB per response, ~20 MB per audit.

Full policy: `references/runtime-and-safety.md`.

## Procedure

Run these in order. Steps 1–3 and 5–7 are deterministic; step 4 is optional.

### 1. Validate and normalise the target

```bash
python scripts/run_audit.py --url "<URL>" --out report.json
```

`run_audit.py` performs steps 1–3 and 5–7 in one process. Prefer it: it is the
tested path and the one the runtime budget is tuned for. Add
`--agent-context-out ctx.json` when you intend to perform step 4.

If the URL cannot be turned into an absolute HTTP(S) URL, the script writes a
schema-valid empty report explaining why and exits 2. Report that; do not retry
with a guessed URL.

### 2. Crawl once

One bounded crawl produces one snapshot that every downstream check reads. The
site is never fetched twice for the same fact.

Discovery order: `robots.txt` → `llms.txt` → a deterministic 404 probe →
sitemaps (from `robots.txt` and the conventional locations) → the entry page.
Then breadth-first to `max_depth`, with an explicit priority score per candidate
(`references/page-selection.md`) — never "the first 20 links". Finally, a
bounded probe of internal links that were not crawled, most-referenced first.

### 3. Run the analyzer skills

The orchestrator resolves sibling skills through `marketplace.json` and runs
each analyzer in its own process against the snapshot. Each returns a check
document (`references/check-result-contract.md`).

An analyzer that is missing, crashes, times out or returns unparseable output
becomes a recorded limitation. **The report is still produced.** The same is
true of an individual check inside an analyzer: checks are isolated from each
other.

### 4. Optional semantic pass

Some judgements are genuinely not deterministic — whether an opening paragraph
actually explains the offering, whether a brand name is ambiguous in its
category. Checks that want a second opinion tag themselves `semantic-review`
and appear in `ctx.json`.

Read `references/semantic-review.md` and follow it. It is deliberately strict:
you may only add or adjust a finding on evidence that is present in the packet,
severity is capped, and speculation is forbidden. Merge your findings with:

```bash
python scripts/finalize_report.py --report report.json --agent-findings mine.json --out report.json
```

If a web-search tool is available, you may also produce the off-site research
file described in `../freshness-corroboration/references/offsite-research.md`
and re-run with `--research research.json`. Keep it
to at most 4 searches. If no search tool is available, skip this entirely —
off-site corroboration is then reported as `NOT_EVALUATED`, which is correct.

### 5. Compose

`references/composition.md` has the detail. In short:

1. **Only `FAIL` becomes a finding.** `PASS`, `NOT_APPLICABLE`,
   `NOT_EVALUATED` and `INCONCLUSIVE` go to `coverage` and `limitations`.
   *No evidence, no finding.*
2. **De-duplicate.** Checks sharing a `dedupe_key` merge into one finding
   carrying both pillars' consequences and the union of the evidence.
3. **Roll up to root causes.** Symptoms that a broader defect mechanically
   causes fold into it rather than padding the report.
4. **Grade** with the one severity policy — `references/severity-model.md`.
5. **Correlate.** Combinations that mean more than their parts are described in
   `root_cause_analysis`. They reference existing findings and never add new
   ones, so the counts stay honest.
6. **Order deterministically, then assign `F-001…`,** so an unchanged site
   yields identical IDs on every run.

### 6. Add proactive recommendations

Opportunities where no defect was found, each gated on evidence from this site
and each carrying a rationale, a mechanism, a priority and concrete steps.
Never generic SEO filler.

### 7. Validate and emit

The report is validated against the required schema, deterministically repaired
if necessary, re-validated, and the outcome recorded in `report_validation`.
Return the JSON. Summarise the top three actions in prose for the human.

## Output

One JSON document. Required floor — always present:

```json
{
  "site": "example.com",
  "audited_at": "2026-09-04T14:32:00Z",
  "summary": { "total_findings": 6, "critical": 1, "high": 2, "medium": 3, "low": 0 },
  "findings": [{
    "id": "F-001",
    "title": "No JSON-LD structured data on product pages",
    "severity": "high",
    "evidence": "Crawled 12 product pages; 0/12 contain schema.org markup.",
    "suggested_action": { "summary": "Add Product/Offer JSON-LD to every product page.",
                          "priority": "high" }
  }]
}
```

Extensions (also always present): `executive_summary`, `root_cause_analysis`,
`proactive_recommendations`, `coverage`, `limitations`, `finding_rollups`,
`audit_metrics`, `report_validation`; and per finding `category`, `check_id`,
`confidence`, `severity_score`, `severity_rationale`, `mechanism`, `breadth`,
`evidence_items`, `affected_urls`, `applicability`, `priority_rank` and
`suggested_action.steps`/`effort`/`expected_benefit`.

Each finding also carries an **executable fix**: `suggested_action.verify` (a
one-line command plus the result to expect, so the fix is falsifiable) and
`suggested_action.fix_prompt` (a self-contained prompt for a coding agent working
in the site's *own repository*). The prompt forbids touching the live site or
inventing any value, and every scraped observation inside it is confined to a
delimited block introduced as untrusted data that must never be followed as
instructions - a page carrying "ignore previous instructions" cannot reach the
agent as an instruction.

Full schema: `references/report-schema.md`. Worked example:
`references/example-report.json`.

## Degradation

Return a valid partial report rather than failing. In budget order, the
orchestrator stops discovering new pages, then skips optional work, then
finishes analysis on evidence already gathered, then always writes the report.
Every gap appears in `limitations` with its reason. A blocked or unreachable
site is itself a finding — the most severe kind — not an error.

## Files

| Path | Purpose |
|---|---|
| `scripts/run_audit.py` | End-to-end audit (steps 1–3, 5–7). |
| `scripts/crawl_site.py` | Snapshot only, for inspection or re-analysis. |
| `scripts/compose_report.py` | Compose a report from an existing snapshot + check files. |
| `scripts/finalize_report.py` | Merge agent findings, re-grade, re-validate. |
| `scripts/auditlib/` | Crawl, parse, severity, composition, schema, manifest. |
| `references/severity-model.md` | How impact, breadth and confidence become one severity. |
| `references/composition.md` | Selection, de-duplication, rollup, correlation, proactive rules. |
| `references/report-schema.md` | The required floor, the extensions, and the validator. |
| `references/snapshot-schema.md` | The contract between the crawl and every analyzer. |
| `references/check-result-contract.md` | What an analyzer skill must return. |
| `references/page-selection.md` | Deterministic sampling and crawl-trap avoidance. |
| `references/runtime-and-safety.md` | Read-only guarantee, robots policy, budgets, limits. |
| `references/semantic-review.md` | Protocol for the optional agent pass. |
