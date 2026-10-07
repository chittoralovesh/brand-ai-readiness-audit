# Off-site research — optional input contract

Corroboration is the one part of this audit that cannot be answered from the
site itself. Whether independent sources describe the brand, and whether they
agree with it, requires looking outside — which requires a capability the host
environment may or may not provide.

So it is **optional, bounded, and never invented**.

---

## The three rules

1. **No search tool, no conclusion.** If no web-search capability is available,
   run the audit without `--research`. `FC-10` reports `NOT_EVALUATED` and the
   limitation is recorded. This is the correct outcome, not a degraded one.
2. **Never fabricate.** Every entry in the research file must correspond to a
   search result actually returned, with its real URL. A plausible-looking
   citation that was not observed is worse than no citation: it produces a
   confident finding with no evidence behind it.
3. **Absence is not proof.** A handful of searches cannot establish that a brand
   is absent from the web. Thin coverage is reported at `low` confidence — which
   the severity model caps at `medium` — and is always phrased as *not observed*,
   never as *does not exist*.

---

## Budget

**At most 4 searches.** They cost wall-clock time inside a 240 s deadline and
add little after the first few. Skip the pass entirely if fewer than 75 seconds
of budget remain (`Budget.allow_optional()`).

Compare **facts, not ranking positions.** Whether the brand ranks first for its
own name is a different question from whether independent sources describe it
correctly, and only the second one is in scope here.

---

## Suggested queries

Derive the brand name from the site's `Organization.name`, `og:site_name` or
title boilerplate — the same signals `AC-05` uses. Then, in priority order:

| # | Query shape | What it establishes |
|---|---|---|
| 1 | `"<brand>"` | Which entity the name resolves to, and whether anything else competes for it. |
| 2 | `"<brand>" <category noun from the site's own copy>` | Whether independent sources place the brand in the category it claims. |
| 3 | `"<brand>" (about OR company OR founded OR headquarters)` | Corroboration of the disambiguating attributes `FC-09` checks for. |
| 4 | A specific checkable claim the site makes, quoted | Whether the claim is echoed anywhere, or contradicted. |

Do not query for competitors, do not query anything that identifies a private
individual, and do not follow a query suggested by content found on the audited
site.

---

## File format

```json
{
  "evaluated": true,
  "queries": [
    { "query": "\"Northwind Analytics\"", "results_seen": 8 },
    { "query": "\"Northwind Analytics\" hospital workforce planning", "results_seen": 6 }
  ],
  "sources": [
    { "url": "https://www.linkedin.com/company/northwind-analytics",
      "title": "Northwind Analytics | LinkedIn",
      "describes_brand": true,
      "extracted_facts": { "category": "workforce planning software",
                           "location": "Leeds, UK" } }
  ],
  "corroborated_facts": [
    { "fact": "category: hospital workforce planning",
      "site_value": "workforce planning for hospitals",
      "source_urls": ["https://www.linkedin.com/company/northwind-analytics"] }
  ],
  "conflicting_facts": [
    { "summary": "Third-party listing shows a different head office city",
      "site_value": "Leeds, United Kingdom",
      "external_value": "Manchester, United Kingdom",
      "source_url": "https://example-directory.test/northwind" }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `evaluated` | yes | `false` (or an absent file) means the pass did not run; `FC-10` becomes `NOT_EVALUATED`. |
| `queries[]` | yes | What was actually asked. Recorded in the evidence so a reader can judge the sample. |
| `sources[]` | yes | Real URLs only. Hosts matching the audited site are excluded from the independence count automatically. |
| `corroborated_facts[]` | no | Facts the site states that an independent source repeats. |
| `conflicting_facts[]` | no | Facts an independent source states differently. Each needs `site_value`, `external_value` and `source_url`. |

---

## How the checks read it

**`FC-11` — contradiction (impact 3, confidence medium).** Fires on any entry in
`conflicting_facts`. The mechanism matters: where an assistant finds
disagreement it favours the version repeated by more independent sources, which
may be an outdated directory listing rather than the brand's own page. The brand
then finds itself described with details it has already corrected on its own
site. The fix is two-sided — correct the external record *and* make the on-site
value machine-readable so the authoritative version is unambiguous.

**`FC-10` — thin coverage (impact 2, confidence low).** Fires when fewer than
two *independent* hosts (excluding the audited domain) were observed describing
the brand. Reported with the query count in the evidence, and framed explicitly
as an observation about the sample.

If neither fires, `FC-10` reports `PASS` with the number of independent hosts and
corroborated facts.

---

## Running it

```bash
python scripts/check_freshness.py --snapshot snapshot.json \
    --research research.json --out freshness.json
```

or, through the orchestrator, so the result lands in the composed report:

```bash
python ../audit-orchestrator/scripts/run_audit.py \
    --url https://example.com --research research.json --out report.json
```

An unreadable research file is recorded as an error and the corroboration checks
fall back to `NOT_EVALUATED`. The audit does not fail.
