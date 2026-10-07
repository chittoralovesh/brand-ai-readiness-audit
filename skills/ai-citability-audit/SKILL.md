---
name: ai-citability-audit
description: Check whether a machine can extract a specific fact from a website and attribute it to the right brand - structured data presence and validity, JSON-LD that parses, Organization/Person identity with sameAs, entity-appropriate Product, Article and LocalBusiness markup gated on evidence the page actually contains that entity, structured data that contradicts the visible page, brand-name consistency across signals, stable entity identifiers, value-proposition clarity, quotable fact density, facts locked in images or PDFs or iframes with no text equivalent, data tables with no header cells to bind a value to its column, boilerplate drowning unique content, Open Graph preview metadata and editorial attribution. Reads a snapshot produced by audit-orchestrator and emits check results with quantified evidence; it does not fetch anything itself. Use as part of a brand AI-readiness audit when diagnosing why assistants omit, misquote or misattribute a brand even though its pages are crawlable.
license: Apache-2.0
compatibility: Requires Python 3.9+ (standard library only). No network access needed - it analyses an existing snapshot.
allowed-tools: Bash Read
metadata:
  author: adobe-university-hackathon-2026-round-3
  version: "1.0.0"
  role: analyzer
  pillar: ai-discoverability
---

# AI citability audit

`crawl-render-audit` establishes that a crawler got in and the text arrived.
This skill asks the next question: **given that the text arrived, can a machine
pull out the entity, the offering, the price, the location, the author — and
attach them to the right brand with enough confidence to repeat them?**

That is where most otherwise-healthy sites fail. They are perfectly crawlable
and still never cited, because there is nothing on the page a system is willing
to quote and nothing that resolves the page to a specific real-world entity.

## When to use

Called by `audit-orchestrator` as part of a full audit. Run it directly only to
re-analyse an existing snapshot.

## Input and output

```bash
python scripts/check_citability.py --snapshot snapshot.json --out citability.json
```

Input: a site snapshot (`../audit-orchestrator/references/snapshot-schema.md`).
Output: a check document (`../audit-orchestrator/references/check-result-contract.md`).
No network access.

## The controlling rule: applicability before assertion

**Never assume every site needs every schema type.** A portfolio site does not
need `Product`. A documentation site does not need `LocalBusiness`. A SaaS site
does not need physical-store information. Demanding them is the single easiest
way to fill a report with confident nonsense, and it is what separates a real
audit from a checklist.

So every schema expectation here is **gated on evidence from the page itself**
that the entity exists, and the gate is reported alongside the finding so a
reader can see why the expectation applied.

| Expectation | Gate — all of it observed on the page |
|---|---|
| `Product`/`Offer` | A price *and* purchase controls ("add to cart", "in stock", SKU), or a product-shaped URL with a price. |
| `Article`/`BlogPosting` | ≥250 words *and* an editorial path, or a date plus a byline. |
| `LocalBusiness`/`PostalAddress` | A street-address pattern *and* a postcode, plus a phone number or opening hours. |
| `FAQPage` | ≥3 question-shaped headings (offered as a recommendation, not a defect). |

Pages that do not match a gate are excluded from that check's denominator
entirely, and the check reports `NOT_APPLICABLE` with the gate quoted. Details
and the reasoning behind each threshold: `references/applicability.md`.

## Reporting rules

- **No evidence, no finding.** Only `FAIL` becomes a finding.
- **Quantify.** "0 of 12 sampled product pages contained parseable JSON-LD",
  never "no structured data".
- **No severity here.** Report `impact_base`, `breadth` and `confidence`; the
  orchestrator grades.
- **Microdata and RDFa count.** A site using `itemtype` or `typeof` has
  structured data. Reporting "no structured data" because it is not JSON-LD
  would be a false positive.

## What it checks

Full mechanism, gate and false-positive control per check: `references/checks.md`.

**Structured data** — `AC-01` presence across the sample · `AC-02` inconsistent
coverage between templates · `AC-03` JSON-LD that fails to parse (a syntax error
discards the *whole* block, so the page behaves as if it had none while every
"is there a script tag" dashboard reports success) · `AC-06`/`AC-07`/`AC-08`
entity-appropriate markup, gated as above · `AC-09` markup asserting values the
page does not show.

**Entity identity** — `AC-04` no machine-readable Organization/Person/WebSite ·
`AC-05` the site stating its own name inconsistently across structured data,
`og:site_name`, title boilerplate and the copyright line · `AC-10` entities with
no stable `@id`, so signals never accumulate onto one node · `AC-18` no outbound
links or `sameAs` to any profile that could corroborate the entity.

**Extractable facts** — `AC-11` an entry page that never says what it offers ·
`AC-12` pages with nothing concrete to quote · `AC-13` facts carried by images
with no text equivalent · `AC-14` primary content delivered as PDFs or embedded
frames · `AC-19` tables that carry data but no `<th>`, so the numbers survive extraction and
their meaning does not · `AC-15` navigation and footer outweighing unique content ·
`AC-16` missing preview metadata · `AC-17` unattributed editorial content.

## The checks that most often change an outcome

**`AC-09`, structured data that contradicts the page.** Markup is trusted only
while it agrees with the rendered page. Asserting an unseen price or an
`aggregateRating` with no visible reviews is read as manipulation: the values are
discarded and trust in the rest of the site's markup drops. This is a defect that
looks like a success in most validators, because the JSON is *valid* — it is just
untrue.

**`AC-05` + `AC-04`, entity resolution.** Entity resolution fails by **merging**,
not by returning nothing. A brand with no declared identity and an inconsistent
name gets fused with whatever similarly-named entity the system already knows —
after which every "fact" an assistant repeats about the brand belongs to someone
else. That failure is invisible in the brand's own analytics, which is why it
persists.

**`AC-13`, facts in images.** A price list, menu, specification table or
opening-hours graphic published only as an image is invisible to text extraction
*and* to screen readers. The same fix serves both audiences.

## False-positive controls

| Risk | Control |
|---|---|
| Demanding schema a site does not need | Every expectation gated on page evidence; gate quoted in the finding. |
| "No structured data" on a microdata site | `itemtype`/`typeof` counted as structured data. |
| Brand-name mismatch from title word order | The brand element of a title is the segment *repeated across pages*, never the last one. |
| "Acme" vs "Acme Ltd" read as a conflict | Names where one is a prefix of the other are treated as consistent. |
| Decorative images reported as missing alt | `alt=""` is a correct declaration and is never counted; only a *missing* attribute is. |
| Price mismatch from formatting | Comparison is digits-only and accepts substring matches. |
| Judging clarity subjectively | `AC-11` fails only on objective conditions (no H1, placeholder H1, brand-only H1, <15 words); anything softer is tagged `semantic-review` for the orchestrator's optional pass. |

## Files

| Path | Purpose |
|---|---|
| `scripts/check_citability.py` | All checks; standard library only. |
| `references/checks.md` | Per-check mechanism, gate, evidence, impact and FP control. |
| `references/applicability.md` | The page-role inference rules and why each threshold is set where it is. |
