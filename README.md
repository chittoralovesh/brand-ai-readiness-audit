# brand-ai-readiness-audit

An Agent Skill Marketplace that audits any public website for the defects that
stop AI assistants **finding, reading and citing** it, and that stop human
visitors **engaging** once they arrive — then emits one evidence-backed,
prioritised report of findings and suggested actions.

Adobe University Hackathon 2026, Round 3.

---

## 1. What it is for

A brand can be invisible to an assistant for three different reasons, and the
usual mistake is to fix the wrong one:

1. **The crawler was not let in** — robots rules, bot walls, error statuses.
2. **It got in but could not read the page** — the content only exists after
   JavaScript runs, or there is barely any text in the response.
3. **It read the page but found nothing it could quote or attribute** — no typed
   values, no dates, no stable identity, nothing checkable to repeat.

Most teams work on (1) because it is the easiest to measure. This marketplace
distinguishes all three, says which one the site is actually failing, and
refuses to raise a finding it cannot evidence. It then asks the second half of
the question the brief poses: when someone *does* arrive from a citation, can
they orient, find the next step, and keep moving?

## 2. Architecture

One crawl, four independent analyses, one composed report.

```
                       ┌──────────────────────────┐
   url ───────────────▶│    audit-orchestrator    │  ← the single entrypoint
                       │  (bounded, read-only,    │
                       │   robots-respecting)     │
                       └────────────┬─────────────┘
                                    │ one site snapshot (structured, no raw HTML)
              ┌──────────────┬──────┴───────┬──────────────────┐
              ▼              ▼              ▼                  ▼
     crawl-render-audit  ai-citability  freshness-        engagement-audit
      "let in? readable?"  "extractable?  corroboration     "will a visitor
                            attributable?"  "current?         stay?"
                                             corroborated?"
              └──────────────┴──────┬───────┴──────────────────┘
                                    │ check results (status, impact, breadth, confidence)
                       ┌────────────▼─────────────┐
                       │  compose: select · merge │
                       │  · roll up · grade ·     │
                       │  correlate · order       │
                       └────────────┬─────────────┘
                                    ▼
                            one JSON audit report
```

The site is fetched **once**. Analyzer skills never touch the network; they read
a structured snapshot. That is what makes the audit fast, polite, and immune to
two skills disagreeing because they parsed the same page differently.

## 3. The skills

| Skill | Entry | Responsibility |
|---|---|---|
| [`audit-orchestrator`](skills/audit-orchestrator/SKILL.md) | **yes** | Validates the URL, enforces the read-only and robots policy, runs the one bounded crawl, invokes the analyzers, then de-duplicates, rolls up, grades, prioritises, validates and emits the single report. |
| [`crawl-render-audit`](skills/crawl-render-audit/SKILL.md) | no | Machine **access** and **readability**: robots policy including AI-crawler blocks, declared-versus-enforced access, bot walls, status codes, redirects, `noindex`, canonicals, sitemaps, client-render gaps, titles, headings, language, viewport, duplicated head metadata, meta-refresh redirects, locale alternates, broken links, soft 404s, crawl traps, response weight. |
| [`ai-citability-audit`](skills/ai-citability-audit/SKILL.md) | no | Fact **extraction** and **entity identity**: structured-data presence and validity, entity-appropriate markup gated on page evidence, markup that contradicts the page, brand-name consistency, stable identifiers, value proposition, quotable fact density, facts locked in images/PDFs/frames, unlabelled data tables, preview metadata, attribution. |
| [`freshness-corroboration`](skills/freshness-corroboration/SKILL.md) | no | **Currency** and **trust**: machine-readable dates, stale or contradictory dates, placeholder content, sitemap `lastmod`, contact-detail consistency, entity-disambiguation attributes, and optional off-site corroboration when a search tool is supplied. |
| [`engagement-audit`](skills/engagement-audit/SKILL.md) | no | The **visitor**: primary action, link labelling, navigation consistency, dead ends, orphan pages, broken links, error-page recovery, essential-page discoverability, pricing discoverability, scanability, mobile viewport, form usability, alt text, breadcrumbs. |

### Why these boundaries, and not others

The split follows the **pipeline a page passes through before it can be cited**,
so each skill owns one failure mode and can be read, tested and reasoned about
alone:

- `crawl-render-audit` answers questions about the **transport and the
  document**. Its evidence is status codes, headers and markup structure.
- `ai-citability-audit` answers questions about **meaning** — what the page
  asserts and whether a machine can attribute it. Its evidence is typed values
  and text content.
- `freshness-corroboration` answers questions about **time and agreement**,
  which is the only concern that may reach outside the site, and therefore the
  only one with an optional capability.
- `engagement-audit` answers questions about a **human**, which needs a
  different notion of severity entirely: a dead end costs a session, not an
  index entry.

They overlap deliberately at three points — broken links, missing viewport,
missing alt text — because those defects genuinely harm both audiences. Rather
than suppress one side, both skills report them with a shared `dedupe_key`, and
the orchestrator merges them into a single finding carrying **both**
consequences. That is a stronger argument for fixing it than either alone.

Nothing here is padding: removing any one skill removes a whole class of
detection, and no skill duplicates another's checks.

## 4. How the entrypoint composes them

Detail in
[`references/composition.md`](skills/audit-orchestrator/references/composition.md).

1. **Select** — only `FAIL` becomes a finding. `PASS`, `NOT_APPLICABLE`,
   `NOT_EVALUATED` and `INCONCLUSIVE` go to `coverage` and `limitations`.
   *No evidence, no finding.*
2. **De-duplicate** — checks sharing a `dedupe_key` merge into one finding with
   the union of the evidence and both pillars' mechanisms.
3. **Roll up** — where one defect mechanically causes others (a page that only
   exists after JavaScript has no title, headings or structured data *because of
   that*), the symptoms fold into the root cause instead of padding the report.
4. **Grade** — one severity policy, applied centrally:
   [`references/severity-model.md`](skills/audit-orchestrator/references/severity-model.md).
   Analyzer skills never grade their own concern.
5. **Correlate** — combinations that mean more than their parts are described in
   `root_cause_analysis` with an ordered fix sequence. They add no findings, so
   the counts stay honest.
6. **Order and identify** — deterministic sort, *then* `F-001…`, so an unchanged
   site produces identical IDs every run.
7. **Recommend proactively** — opportunities where no defect was found, each
   gated on evidence from this site.
8. **Validate** — validate, repair deterministically if needed, re-validate, and
   record the outcome in `report_validation`.

### Every finding ships an executable fix

The report is consumed by an agent, so a finding that stops at prose stops one
step short. Each carries two more fields:

- **`suggested_action.verify`** — a one-line command and the result to expect,
  so the fix is falsifiable rather than assumed.
- **`suggested_action.fix_prompt`** — a self-contained prompt for a coding agent
  working in the site's *own repository*.

The prompt states the defect, the affected URLs, the required change and the
constraint that it edits templates and never the live site or any value the page
does not already show. The marketplace stays recommend-only: it emits text, and
a human decides whether to run it.

The security property matters as much as the feature. A prompt of this kind must
quote content taken from the audited site, and that content is untrusted. Pasted
raw into a prompt another agent executes, a page carrying "ignore previous
instructions" would be an injection vector. So every observation, excerpt and
quoted evidence sentence sits inside one delimited block introduced as data that
must never be followed, with the closing marker neutralised inside the content
so the fence cannot be escaped. URLs are the one exception, because the
instructions need them, and they are *validated* rather than escaped: shell
metacharacters, quotes and whitespace are stripped and anything that no longer
parses as an absolute HTTP URL is dropped.

Built once at composition time, so the analyzer skills stay pure functions over
the snapshot and one policy governs all 77 checks.

## 5. Installation and environment

Nothing to install.

- Python 3.9+ — **standard library only**.
- Outbound HTTP access to the site being audited.
- No API key, no browser binary, no Playwright, no database, no model weights,
  no third-party package.

Optional: a web-search tool enables off-site corroboration. Without it that one
check reports `NOT_EVALUATED` and everything else runs unchanged.

An agent harness installs the marketplace by reading `marketplace.json`, which is
self-contained and needs no remote service to resolve.

## 6. Running an audit

Invoke the **entrypoint** skill and give it a URL. Directly:

```bash
python skills/audit-orchestrator/scripts/run_audit.py \
    --url https://example.com --out report.json
```

Useful flags: `--max-pages` (20), `--max-depth` (2), `--budget` (240 s),
`--timeout` (8 s), `--rate` (2 req/s/host), `--snapshot-out`,
`--agent-context-out`, `--research`.

Other entry points, all optional:

```bash
# Snapshot only, for inspection or re-analysis without re-crawling
python skills/audit-orchestrator/scripts/crawl_site.py --url https://example.com --out snap.json

# Run one analyzer skill over an existing snapshot
python skills/ai-citability-audit/scripts/check_citability.py --snapshot snap.json --out ac.json

# Re-compose a report from saved check documents
python skills/audit-orchestrator/scripts/compose_report.py --snapshot snap.json \
    --checks ac.json en.json --out report.json

# Merge findings from the optional agent semantic pass
python skills/audit-orchestrator/scripts/finalize_report.py \
    --report report.json --agent-findings mine.json --out report.json
```

### Example input

```json
{ "url": "https://example.com" }
```

### Example output (abridged)

A complete generated report is at
[`example-report.json`](skills/audit-orchestrator/references/example-report.json).

```json
{
  "site": "example.com",
  "audited_at": "2026-09-04T14:32:00Z",
  "summary": { "total_findings": 6, "critical": 1, "high": 2, "medium": 3, "low": 0 },
  "findings": [
    {
      "id": "F-001",
      "title": "Product pages carry no Product/Offer structured data",
      "severity": "high",
      "evidence": "12 of 12 sampled pages that qualify as pages showing a price together with purchase controls contain no Product/Offer markup (/products/a, /products/b, ...).",
      "suggested_action": {
        "summary": "Add Product/Offer markup to the 12 qualifying templates.",
        "priority": "high",
        "effort": "medium",
        "steps": ["Add `Product` with `name`, `description`, `image`, `sku` where you have one, and a nested `Offer` with `price`, `priceCurrency` and `availability`.", "..."]
      },
      "category": "ai_discoverability",
      "check_id": "AC-06",
      "confidence": "high",
      "severity_score": 3,
      "severity_rationale": "impact 3; breadth +1 (12/12 pages); confidence high (+0); score 3",
      "mechanism": "Product markup carries the exact name, price, availability and identifiers. Without it a shopping or answer surface has to scrape a price out of layout, which it usually declines to do ...",
      "breadth": { "affected": 12, "applicable": 12, "unit": "pages", "ratio": 1.0 },
      "evidence_items": [{ "url": "...", "observed": "...", "expected": "..." }],
      "applicability": "Raised only for pages that demonstrably contain this entity ..."
    }
  ],
  "executive_summary": { "headline": "...", "start_here": ["..."] },
  "root_cause_analysis": [{ "id": "RC-01", "title": "...", "fix_order": ["..."] }],
  "proactive_recommendations": [{ "id": "PR-02", "priority": "high", "title": "..." }],
  "coverage": { "checks_by_status": { "FAIL": 6, "PASS": 41, "NOT_APPLICABLE": 12, "NOT_EVALUATED": 2 } },
  "limitations": [{ "scope": "FC-10", "status": "NOT_EVALUATED", "reason": "..." }],
  "audit_metrics": { "duration_ms": 41280, "pages_fetched": 18, "checks_completed": 61 },
  "report_validation": { "valid": true, "errors": [], "repairs_applied": [] }
}
```

The required floor — `site`, `audited_at`, `summary` counts, and per finding
`id` / `title` / `severity` / `evidence` / `suggested_action` — is always
present. Everything else is an extension. Full schema:
[`references/report-schema.md`](skills/audit-orchestrator/references/report-schema.md).

## 7. Runtime budget

Adobe's ceiling is 300 s. The design target is **under 180 s**, with a hard
internal deadline of **240 s** that reserves time to serialise a report.

Measured medians (see §12):

| Target | Wall clock |
|---|---|
| Small brochure site (fixture) | ~1.8 s |
| 60-page site, 20 crawled (fixture) | ~2.8 s |
| Large documents, 120 images/page (fixture) | ~3.1 s |
| Live site, 20 pages + 30 link probes at 2 req/s | ~30 s |

Most of a live audit is deliberate politeness, not computation: analysis and
composition together take well under a second.

Degradation order when the budget tightens: stop discovering pages → skip link
probes → skip optional work → finish analysis on what was gathered → **always**
write the report. Every gap appears in `limitations`.

### Connections are raced, not queued

`socket.create_connection` tries a host's resolved addresses strictly in order,
waiting for each to time out. On a machine whose IPv6 path is black-holed — the
norm in containers, CI runners and offices with partial dual-stack — that costs
a full connect timeout on **every request** to a dual-stack host.

Measured on exactly such a host, that was 16 seconds per request. Two effects
followed, and neither was the audited site's fault:

- A 20-page audit spent close to four minutes waiting on the local network.
- `robots.txt` has a shorter timeout than a page fetch, so it failed first. An
  unreadable `robots.txt` means the crawler stays out, so the whole site was
  treated as disallowed and **nothing was crawled at all**.

The fetcher therefore follows RFC 8305 ("Happy Eyeballs"): attempts start
`CONNECT_STAGGER` apart, run concurrently, and the first socket to complete
wins while the rest are closed. The family that answered is remembered per host
and tried first afterwards — the family, deliberately, not the individual
address, since pinning a whole crawl to one edge IP of a large site reads as a
traffic spike to that node.

On the same host the cold request fell from 16 s to 0.5 s and warm requests to
under 0.1 s. The Python documentation audit went from 222 s to 33 s while
sampling *more* pages, and GOV.UK from 223 s to 52 s.

## 8. Safety — read-only, and structurally so

The guarantee is enforced in the transport layer, not by convention:

```python
if method not in ("GET", "HEAD"):
    raise ValueError("Fetcher is read-only: only GET and HEAD are permitted")
```

No code path in the marketplace can emit `POST`/`PUT`/`PATCH`/`DELETE`, send a
request body, or attach credentials. The test suite asserts it from both sides —
the fixture server fails the test if it ever receives another verb.

Never done, at any budget, on any site: submitting a form, logging in, creating
an account, sending a message, adding to a cart, checking out, changing a
setting, uploading anything, bypassing `robots.txt`, brute-forcing paths, or
probing for vulnerabilities.

URLs matching login, logout, account, cart, checkout, register, subscribe,
delete or edit patterns are filtered out **before** the crawl frontier and are
never requested at all. Skipped URLs are counted and reported.

## 9. robots.txt policy

Obeyed without exception, with an RFC 9309 parser (longest match wins, `Allow`
beats `Disallow` on ties, `*` and `$` supported, per-agent groups preserved).

| robots.txt | Behaviour |
|---|---|
| 2xx, parseable | Rules applied; `Crawl-delay` honoured; `Sitemap:` used. |
| 404 / 410 / 401 / 403 | No restrictions — reported as `PASS`, not a defect. |
| 5xx from the server | **Nothing is crawled**, matching how major crawlers behave, and reported as a severe finding. |
| No response at all (DNS, refused, unroutable, TLS, timeout) | Nothing is crawled, and **nothing is reported as a site defect** — see §10. |

A disallowed URL is never fetched and never worked around; the gap becomes a
coverage limitation. The auditor identifies itself honestly and never varies its
user agent to evade a block — if a site refuses it, that refusal *is* the
finding.

Politeness limits: ≤20 pages (≤35 only when the deadline has generous slack), depth ≤2, ≤30 extra link probes, ≤2 concurrent
same-host sockets, ≤2 requests/second/host, 8 s timeout, 1 retry, 5 redirect
hops, 2 MB per response, ~20 MB per audit. A typical audit makes 25–50 requests
to one host. Full policy:
[`references/runtime-and-safety.md`](skills/audit-orchestrator/references/runtime-and-safety.md).

## 10. Failure behaviour, and the checks that are easiest to get wrong

A valid report is always produced. The only thing that stops one being written
is the process being killed.

| Failure | Behaviour |
|---|---|
| Invalid URL | Schema-valid empty report explaining why; exit code 2. |
| Site answers 4xx/5xx, or shows a bot wall | Reported as a **finding** — the origin told us something about itself. |
| No HTTP response from the origin at all | Reported as a **coverage limitation**, never a finding. See below. |
| An analyzer skill missing, crashing, hanging or returning garbage | Recorded as a coverage limitation; the other three still report. |
| One check inside a skill raising | Caught per check, recorded in `errors`; the rest run. |
| Malformed snapshot | Fields degrade individually; skills still emit results. |
| Budget exhausted | Optional work skipped in a documented order; partial report emitted with limitations. |
| Report fails its own schema check | Deterministically repaired, re-validated, and the repairs listed in `report_validation`. |

### No target response, no target-specific finding

A DNS failure, refused connection, unroutable network, TLS environment fault
or bare timeout describes the machine running the audit — not the website. An
earlier revision let those surface as `CR-01 critical: entry URL could not be
retrieved`, `CR-27 medium: no XML sitemap was found` and `CR-03 low: robots.txt
is unreadable`, which is three confident claims about a site nothing ever
reached.

The crawl now records a transport diagnosis (`snapshot.transport`), and every
check that could otherwise assert absence is gated on it:

| Observation | Verdict |
|---|---|
| No HTTP response from the origin | `CR-01` INCONCLUSIVE; `CR-02`/`CR-03`/`CR-27`/`CR-25` NOT_EVALUATED; a `transport` limitation |
| HTTP 401/403/429/451 from the origin | Blocking findings — the server answered |
| Challenge or CAPTCHA interstitial | `CR-02` bot-wall finding |
| robots.txt retrieved with restrictive directives | `CR-04`/`CR-05` findings |
| robots.txt 5xx from the server | `CR-03` finding (crawlers treat it as disallow-all) |
| Sitemap candidates answered 404/410 | `CR-27` missing-sitemap finding, quoting the statuses |
| Sitemap candidates never answered | `CR-27` NOT_EVALUATED |

Absence is only ever asserted from an answer.

### Declared policy versus enforced access

`robots.txt` is a published *policy*; a CDN or WAF enforces a separate rule set
at the network layer, usually by matching the User-Agent string. When the two
disagree, a site invites crawlers on paper and refuses them in practice — and
because nothing in the policy file is wrong, an audit that only reads
`robots.txt` never sees it. **`CR-31`** compares the two and reports the gap.

It is measured with the auditor's **own honest user agent**. The check never
claims to be GPTBot or any other named crawler: the finding is that a request
which openly identified itself as an automated client was refused while the
published policy invited it. Where nothing answered at all, the result is
`INCONCLUSIVE` — a silent edge-level drop and a broken local network are
indistinguishable from one end, so neither is asserted.

### Adaptive page budget

The sample is 20 pages by default. When discovery finishes with genuinely
generous headroom — the site has already proved it responds quickly — the cap
widens toward a hard ceiling of 35, so the ratios every finding quotes rest on
more pages. The decision is made once, before the crawl loop, so page selection
stays deterministic; an explicit `--max-pages` always wins; and the expansion is
recorded in `notes` so the report says the sample was widened and why.

### Redirects the fetcher never follows

A `<meta http-equiv="refresh">` is a redirect written in the document instead of
the HTTP response. Browsers honour it; many crawlers and most AI fetchers do
not, so the document they store is the near-empty waypoint rather than the
destination. **`CR-32`** reports it, and requires a `url=` target first, so a
self-refreshing dashboard is not mistaken for a redirect.

### Metadata the author does not control

HTML allows one `<title>` and one meaningful value per metadata name. When a
theme, an SEO plugin and a CMS field each emit their own, the survivor is chosen
by whichever parser reads the page. **`CR-33`** reports the duplication, and
counts only `robots` and `description` as decisive — a repeated `viewport` is
untidy but changes nothing about what is indexed. A repeated `robots` raises the
impact, because conflicting directives usually resolve to the most restrictive
and a stray `noindex` then removes the page silently.

### Localised pages competing with each other

Locale variants of one page are near-duplicates in different languages, so
without `hreflang` they compete: one is kept, the rest are suppressed, and a
reader can be handed the wrong language. **`CR-34`** reports a missing set, codes
that are not valid BCP 47, and sets that omit their own self-reference. It is
gated on evidence the site *is* localised — two distinct language roots in the
URL space, or two distinct `<html lang>` values — so a monolingual site is never
asked for it, and a two-letter path segment only counts when it is a real
ISO 639-1 code.

### Tables whose columns are not labelled

A value in a table means nothing without its column, and `<th>` is what binds
the two. **`AC-19`** reports tables carrying data with no header cells, at
`medium` confidence because a count alone cannot separate a layout table from an
unlabelled data table. The denominator is pages that contain a table, not all
pages.

### A declared Crawl-delay is honoured in full

`robots.txt` may declare a `Crawl-delay`, and the audit's rule is that
robots.txt is respected *without exception*. Hacker News declares 30 seconds.

The limiter used to clamp the declared delay to 10 seconds and then cap each
individual sleep at 5, so a site asking for one request every 30 seconds
received roughly one every 5. That is a politeness violation, and fixing it
naively pushed a real audit to 332 s — past Adobe's 300 s ceiling.

Both are now resolved without trading one against the other:

- The declared interval is waited **in full**, up to `MAX_HONOURED_CRAWL_DELAY`.
- The **page target shrinks** to what the delay affords inside the budget, and
  the reduction is recorded, so every ratio still names its denominator.
- A request that cannot be made at the declared pace in the time remaining is
  **skipped, never hurried**, and counted.
- Optional discovery yields to content: where requests are expensive, `llms.txt`
  and guessed sitemap locations are skipped so the budget buys sampled pages.

The pace is never shortened. When politeness and the deadline conflict, the
audit gathers less rather than asking for more. Hacker News now completes in
152 s with 5 pages and a valid report.

### Concurrency

Audits are safe to run in parallel, including into the same output directory.
The crawl snapshot is held **in memory** and handed to each analyzer process on
stdin; the audit creates no temporary file of its own, so there is no shared
resource for concurrent runs to race on. `--snapshot-out` writes the snapshot
only where you ask it to.

## 11. Testing

```bash
python -m unittest discover -s tests -v        # everything (~200 s)
python -m unittest discover -s tests -p "test_marketplace.py"   # spec compliance
python -m unittest discover -s tests -p "test_core.py"          # URL, robots, HTML, severity, schema
python -m unittest discover -s tests -p "test_crawler.py"       # safety, limits, determinism
python -m unittest discover -s tests -p "test_analyzers.py"     # detection and false positives
python -m unittest discover -s tests -p "test_composition.py"   # merge, rollup, ordering
python -m unittest discover -s tests -p "test_transport.py"     # network failure is not a site defect
python -m unittest discover -s tests -p "test_enforcement_and_budget.py"  # CR-31 + adaptive sampling
python -m unittest discover -s tests -p "test_dormant_signals.py"  # CR-32/33/34 + AC-19, and the dead-field guard
python -m unittest discover -s tests -p "test_connection.py"       # Happy Eyeballs connection racing
python -m unittest discover -s tests -p "test_politeness.py"       # Crawl-delay honoured in full
python -m unittest discover -s tests -p "test_fixprompt.py"        # executable fixes + injection fencing
python -m unittest discover -s tests -p "test_concurrency.py"   # 5 parallel audits, one directory
python -m unittest discover -s tests -p "test_end_to_end.py"    # full pipeline
```

Standard library only — no pytest. Every test runs against an in-process fixture
server, never a live third-party site: results must not drift, and testing our
own politeness by hammering someone else's server would be self-defeating.

The suite deliberately tests **absence of false positives** as hard as
detection — including that a broken network never becomes a website defect: a server-rendered framework site must not be reported as a render
gap, `alt=""` must not be reported as missing alt text, a documentation site
must not be asked for `Product` markup, and a healthy site must produce zero
critical or high findings.

## 12. Validation and benchmarking

```bash
# Agent Skills spec compliance, with the official reference validator if present
python -m unittest discover -s tests -p "test_marketplace.py"
pip install skills-ref && agentskills validate ./skills/audit-orchestrator

# Manifest, frontmatter, secrets and size checks without packaging
python tools/package_submission.py --check-only

# Runtime, against synthetic sites (no network) or live ones you name
python tools/benchmark.py --runs 3
python tools/benchmark.py --url https://example.com --json bench.json

# Regenerate the worked example in the documentation
python tools/make_example_report.py
```

## 13. Packaging

```bash
python tools/package_submission.py
```

Validates first and refuses to package an invalid submission — malformed
manifest, missing skill folder or `SKILL.md`, anything other than exactly one
entrypoint, a name/directory mismatch, non-spec frontmatter, anything resembling
a credential, or a ZIP at or above 50 MB. Development artefacts (`__pycache__`,
`.git`, `dist`, virtual environments, model-weight extensions, local audit
output) are excluded.

Output: `dist/brand-ai-readiness-audit.zip`, containing a single top-level
`brand-ai-readiness-audit/` directory with `marketplace.json` at its root.

**Current size: 0.28 MB compressed** (0.87 MB uncompressed, 55 files) — against a
50 MB limit and our own 5 MB target. There are no model weights, no bundled
binaries and no vendored dependencies.

## 14. Repository layout

```
marketplace.json                 manifest: every skill, exactly one entrypoint
README.md                        this file
REQUIREMENTS.md                  every brief requirement mapped to its implementation
LICENSE
skills/
  audit-orchestrator/            ENTRYPOINT
    SKILL.md
    scripts/  run_audit.py · crawl_site.py · compose_report.py · finalize_report.py
              auditlib/  urls · robots · fetcher · htmlparse · crawler ·
                         budget · severity · compose · schema · registry
    references/ severity-model · composition · report-schema · snapshot-schema ·
                check-result-contract · page-selection · runtime-and-safety ·
                semantic-review · example-report.json
  crawl-render-audit/            SKILL.md · scripts/ · references/checks.md
  ai-citability-audit/           SKILL.md · scripts/ · references/{checks,applicability}.md
  freshness-corroboration/       SKILL.md · scripts/ · references/{checks,offsite-research}.md
  engagement-audit/              SKILL.md · scripts/ · references/checks.md
tests/                           fixture server + 324 tests, standard library only
tools/                           package_submission.py · benchmark.py · make_example_report.py
```

## 15. Design decisions worth knowing

**No evidence, no finding.** Uncertainty is never converted into a defect. A
missing capability is a coverage limitation; an unmet precondition is
`NOT_APPLICABLE` with the gate quoted, so a reader can see the expectation was
considered and correctly dropped.

**Applicability before assertion.** Every schema expectation is gated on
evidence that the entity exists on the page. A portfolio site is never asked for
`Product`; a SaaS site is never asked for a postal address. Gates fail *safe* —
a missed role costs one true positive, a wrong one costs trust in the report.

**Confidence caps severity.** Low-confidence findings can never exceed `medium`,
whatever their theoretical impact. That bounds the cost of being wrong, and the
cap is shown in `severity_rationale`.

**Root causes over symptom lists.** A client-rendered site produces one finding
about the render gap with its symptoms folded in, not five findings about
missing titles, headings, text and structured data.

**Deterministic by construction.** Normalised URLs, scored page selection, fixed
check IDs, a fixed sort applied before IDs are assigned. Two runs against an
unchanged site produce identical reports.

**No signal is collected and discarded.** Every field the parser writes into a
snapshot costs bytes in the artefact and implies to a reader that something acts
on it. A field nothing reads is either a missing check or dead weight. A test
enumerates what the parser emits, subtracts what the analyzers read, and fails
unless the remainder matches a short allowlist of fields carried deliberately as
context. Adding an extraction field without a check that reads it, or a reason
it has none, breaks the build.

**No site-specific rules.** No domain, brand, CMS, industry or vertical is
special-cased anywhere. Page-role inference reads the page's own content; the
crawl-priority vocabulary describes roles a page can play on any site, and a
site whose vocabulary is unfamiliar is sampled by structure instead of being
penalised.

## 16. Known limitations

- **Bounded sample.** 20 pages at depth 2. Every finding states its own
  denominator, and `limitations` says so explicitly.
- **No JavaScript execution.** By design — the render gap is *detected* rather
  than worked around, which is the finding that matters. Render comparison is
  reported `NOT_EVALUATED` unless the host provides a rendering tool.
- **Off-site corroboration is optional.** Without a search tool it is
  `NOT_EVALUATED`; a small search sample can never prove absence, so thin
  coverage is reported at `low` confidence and phrased as "not observed".
- **Chrome detection needs semantic HTML.** A site using `<div class="header">`
  rather than `<header>` has no machine-detectable navigation, so checks that
  depend on the distinction report at `medium` confidence.
- **Language.** Symbol, numeric and structural patterns are used wherever
  possible; where an English keyword list is unavoidable, a miss yields
  `NOT_APPLICABLE` rather than a false finding.
- **Subdomains are separate sites.** Auditing `example.com` does not pull in
  `blog.example.com`.

## 17. Licence

Apache-2.0. See [`LICENSE`](LICENSE).
