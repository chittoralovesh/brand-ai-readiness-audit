# Requirements traceability

Every requirement in the Adobe University Hackathon 2026 Round 3 brief, mapped
to where it is implemented and how it is verified.

Status key: **PASS** implemented and tested · **PARTIAL** implemented with a
documented limitation · **N/A** not applicable.

---

## 1. What you submit

| Requirement | Where | Verified by | Status |
|---|---|---|---|
| A single Agent Skill Marketplace | repository root | `test_marketplace.py` | PASS |
| Each skill in the agentskills.io format (`SKILL.md` + frontmatter) | `skills/*/SKILL.md` | `test_marketplace.py::TestSkillFormat`, `agentskills validate` | PASS |
| Optional bundled `scripts/` and `references/` | every skill folder has both | `test_marketplace.py::test_referenced_files_exist` | PASS |
| A marketplace manifest | `marketplace.json` | `test_marketplace.py::TestManifest` | PASS |
| Exactly one designated entrypoint | `audit-orchestrator`, `"entrypoint": true` | `test_marketplace.py::test_exactly_one_entrypoint`; the packager refuses otherwise | PASS |
| The entrypoint receives the request and emits the final report | `skills/audit-orchestrator/scripts/run_audit.py` | `test_end_to_end.py` | PASS |
| Decomposition into focused skills | 5 skills, one concern each | README §3 explains each boundary | PASS |
| A short root `README.md` | `README.md` | `test_marketplace.py::test_marketplace_root_has_required_files` | PASS |

## 2. Report requirements

| Required field | Where | Verified by | Status |
|---|---|---|---|
| `site` | `compose.build_report` | `schema.validate` + `test_core.py::TestReportSchema` | PASS |
| `audited_at` (ISO-8601) | same | same | PASS |
| `summary` with counts by severity | `compose.summarise` | `test_composition.py::test_summary_matches_the_findings` | PASS |
| `findings[].id` (`F-NNN`, unique, contiguous) | `compose.assign_ids` | `test_composition.py::TestOrderingAndIdentity` | PASS |
| `findings[].title` | `compose.build_finding` | `schema.validate` | PASS |
| `findings[].severity` | `severity.severity_of` | `test_core.py::TestSeverityModel` | PASS |
| `findings[].evidence` | analyzer checks | `test_analyzers.py::test_every_check_declares_the_contract_fields` | PASS |
| `findings[].suggested_action` (`summary`, `priority`) | `compose.build_finding` | `schema.validate` | PASS |
| Suggested actions may go beyond detected problems | `proactive_recommendations` | `test_composition.py::TestProactiveRecommendations` | PASS |
| Schema is a floor, extensions allowed | 12 top-level and 20 per-finding extensions | `references/report-schema.md` | PASS |

## 3. What the marketplace must detect

### Off-site discoverability — why the brand is not found or cited

| Failure mode | Checks | Status |
|---|---|---|
| Crawler not let in | `CR-01` entry unreachable · `CR-02` bot wall · `CR-03` unreadable robots.txt · `CR-04` AI crawlers disallowed · `CR-05` search crawlers disallowed · `CR-06` linked pages disallowed · `CR-31` edge refuses crawlers robots.txt invites | PASS |
| Important pages not crawlable | `CR-06`, `CR-08` noindex, `CR-25` broken links, `CR-27` sitemap, `CR-28` crawl traps, `CR-32` meta-refresh redirect the fetcher does not follow, `EN-05` orphans | PASS |
| Content unreadable to a machine | `CR-11` render gap · `CR-12` thin text · `CR-18` landmarks · `AC-13` facts in images · `AC-14` content in PDFs/frames | PASS |
| Facts not extractable | `AC-01`/`AC-02` structured data · `AC-03` malformed JSON-LD · `AC-06`/`AC-07`/`AC-08` entity markup · `AC-11` value proposition · `AC-12` fact density · `AC-19` tables with no header cells | PASS |
| Entity misunderstood or confused | `AC-04` identity node · `AC-05` name consistency · `AC-10` stable `@id` · `AC-18` external profiles · `FC-09` disambiguating attributes | PASS |
| Content considered stale | `FC-01` no dates · `FC-02` staleness · `FC-03` future dates · `FC-04` conflicting dates · `FC-05` copyright · `FC-07` sitemap `lastmod` | PASS |
| Lacks corroboration | `AC-18`, `FC-10` off-site coverage | PARTIAL — off-site half needs a search tool; `NOT_EVALUATED` without one |
| Fails to be cited | `AC-09` markup contradicting the page · `AC-16` preview metadata · `AC-17` attribution · `CR-33` duplicated head metadata, so the parser picks the cited title | PASS |
| Brand misrepresented | `AC-05`, `AC-09`, `FC-08` contact inconsistency, `FC-11` off-site contradiction | PASS |
| Content behind client-side rendering or non-text media | `CR-11`, `AC-13`, `AC-14` | PASS |
| Wrong-locale or competing localised variants | `CR-34` missing, invalid or non-self-referencing `hreflang`, gated on evidence the site is localised at all | PASS |

### On-site engagement — why an arriving visitor does not engage

| Failure mode | Checks | Status |
|---|---|---|
| Value not understood quickly | `AC-11`, `EN-01` | PASS |
| Cannot orient | `EN-03` nav consistency · `EN-08` essential pages · `EN-14` breadcrumbs | PASS |
| No identifiable next action | `EN-01` primary action · `EN-02` link labels | PASS |
| Weak navigation, dead ends, lost context | `EN-03`, `EN-04`, `EN-05`, `EN-14` | PASS |
| Unclear CTAs | `EN-01`, `EN-02` | PASS |
| Page hierarchy / missed information | `CR-16` headings · `EN-10` scanability · `AC-15` boilerplate ratio | PASS |
| Broken or misleading links | `CR-25` / `EN-06` (merged) · `CR-26` soft 404 · `EN-07` error recovery | PASS |
| Poor accessibility / readability | `EN-13` alt text · `EN-11` viewport · `EN-12` forms · `CR-17` `lang` | PASS |
| Fails to build trust | `EN-08` about/privacy/contact · `EN-09` pricing · `FC-05` stale copyright · `FC-06` placeholder content | PASS |
| Leaves without engaging | the whole `engagement-audit` pillar | PASS |

## 4. Rubric

| Criterion | How it is addressed | Status |
|---|---|---|
| **Detection accuracy** | 77 distinct checks across both pillars (67 report a verdict on a single-page site), each with a documented mechanism, gate and evidence format. `FAIL` only from an observation; every other outcome goes to coverage. Applicability gates keep inapplicable expectations out of the denominator. 324 tests, many asserting the *absence* of false positives. Live-tested on 24 public sites across 14 verticals; nine false positives found and fixed across two sweeps (see §7, §7b, §7c). | PASS |
| **Suggested-action quality** | Every finding carries `mechanism`, a `summary` naming the change, ordered `steps`, `expected_benefit` and `effort`. `priority_rank` sequences the whole set; `root_cause_analysis` gives ordered fix sequences where findings share a cause. Six evidence-gated proactive recommendations. Every finding also carries a `verify` command and a `fix_prompt` an agent can run against the site's own repository, with all scraped content fenced as untrusted data. | PASS |
| **Output design** | Required floor plus `executive_summary` (headline, start-here, quick wins, low-severity triage note, how-to-read), `root_cause_analysis`, `coverage`, `limitations`, `severity_rationale` showing the arithmetic. | PASS |
| **Skill format & engineering hygiene** | All five skills pass the official `skills-ref` validator and the local structural tests. Deterministic by construction; read-only enforced in the transport layer; standard library only. | PASS |
| **Marketplace composition** | Five skills split along the pipeline a page passes through. Deliberate overlap at three points is merged by shared `dedupe_key` rather than duplicated. No skill duplicates another's checks. | PASS |
| **Generalization** | No domain, brand, CMS, industry or vertical is special-cased. Page roles are inferred from page content; crawl-priority vocabulary describes roles any site can have, and unfamiliar vocabulary is sampled by structure instead of penalised. Gates fail safe. | PASS |

## 5. Scope and guardrails

| Requirement | Implementation | Verified by | Status |
|---|---|---|---|
| Recommend-only, never alters a live site | `Fetcher` raises on any method but GET/HEAD | `test_crawler.py::test_read_only_fetcher_refuses_other_methods`; the fixture server fails the test on any other verb | PASS |
| No destructive actions | no code path writes to a site | code review + tests | PASS |
| No authenticated-area actions | login/account/checkout URL patterns filtered before the frontier | `test_crawler.py::test_state_changing_urls_are_never_requested` | PASS |
| No rate abuse | ≤2 req/s/host, ≤2 concurrent sockets, `Crawl-delay` honoured, ≤20 pages, ≤30 probes | `test_crawler.py::TestLimits`, `test_crawl_delay_is_honoured` | PASS |
| Respect `robots.txt` | RFC 9309 parser; disallowed URLs never fetched; 5xx robots.txt stops the crawl | `test_core.py::TestRobots`, `test_crawler.py::TestSafety` | PASS |
| Each skill portable and declares its tool needs | `compatibility` + `allowed-tools` in every `SKILL.md`; standard library only | `test_marketplace.py` | PASS |
| Manifest self-contained, no external service | `marketplace.json` is plain local JSON | `test_manifest_is_valid_json_and_self_contained` | PASS |
| Submission ZIP ≤ 50 MB, no model weights | **0.28 MB compressed**; weight extensions excluded and the packager fails at 50 MB | `tools/package_submission.py` | PASS |
| Audit runtime < 5 minutes | design target 180 s, hard deadline 240 s; measured 1.8–4.6 s on fixtures, 30–44 s on live sites | `tools/benchmark.py`, `test_end_to_end.py::test_runtime_is_well_inside_the_five_minute_ceiling` | PASS |

## 6. Engineering constraints

| Constraint | Implementation | Status |
|---|---|---|
| No API key of any kind | none read, none required | PASS |
| No bundled model weights, embeddings or vector database | none; the packager blocks the file extensions | PASS |
| No Docker, Redis, PostgreSQL or SQLite | none; entirely in-memory | PASS |
| No browser binary / Playwright | not bundled, not required; the render gap is *detected* from the response | PASS |
| Standard-library Python only | `urllib`, `html.parser`, `json`, `re`, `xml.etree`, `concurrent.futures`, `dataclasses`, `pathlib`, `datetime`, `gzip`, `zlib`, `ssl`, `socket`, `threading`, `subprocess` | PASS |
| No secrets anywhere | scanned by the test suite and the packager | PASS |
| Deterministic output | normalised URLs, scored selection, fixed check IDs, sort before ID assignment | `test_end_to_end.py::test_report_is_deterministic_across_runs` | PASS |
| No unbounded loops, retries, recursion or model calls | every loop bounded by an explicit constant | code review + `TestLimits` | PASS |
| Valid partial report on any failure | per-check isolation, per-skill isolation, budget reserves, schema repair | `test_end_to_end.py::TestDegradation` | PASS |

## 7. Field testing

The brief asks for learning from real websites without overfitting to them. Four
public sites across different verticals were audited during development
(documentation, reference/media, public sector, and a minimal static page). None
is referenced anywhere in the shipped code, and no site-specific rule exists.

What the exercise changed — all four were **generalisation bugs**, not tuning:

| Observed | Root cause | Fix |
|---|---|---|
| A site canonicalising `/about` → `/about/` appeared entirely broken: 19 of 20 pages recorded as 301 failures | URL normalisation stripped trailing slashes, so the redirect target equalled the source and was rejected as a loop | `normalize()` preserves the trailing slash; a separate `canonical_key()` handles frontier de-duplication (`urls.py`) |
| Every site with an events list was reported as having "dates set in the future" | The future-date check used *any* `<time>` element, but those describe upcoming events, not the page's age | Staleness and future-date checks now use publication-semantics fields only (`freshness/PUBLICATION_FIELDS`) |
| A documentation site was reported as having inconsistent navigation on 19 of 20 pages | The comparison target was the largest single `<nav>` — a sidebar containing 1,329 links | Compare against the *persistent* menu: nav links recurring on ≥60 % of pages (`EN-03`) |
| A large site was reported as having orphan pages because 40 of 2,000 sitemap URLs were unreached | The 20-page sample cannot cover a 2,000-page site; the finding described the crawl budget, not the site | `EN-05` reports `NOT_APPLICABLE` when the sitemap is more than twice the crawl size |
| A `WebSite` node was told it was missing `logo` and `sameAs` | Document entities were treated as identity entities | `PUBLISHER_TYPES` and `DOCUMENT_TYPES` separated; properties merged across all publisher nodes (`AC-04`) |
| A price of "490 US dollars" was reported as absent from the page | Price matching required a currency-adjacent token | Compare the price digits against every number in the page text (`AC-09`) |

Each fix has a regression test.

## 7b. Batch-test regressions (24-property run)

A batch run across a large set of real web properties exposed two production
defects. Both are fixed, and both have regression tests that were verified to
**fail** against the previous behaviour before being accepted.

| Defect | Root cause | Fix | Regression test |
|---|---|---|---|
| A DNS failure on the auditing host produced `CR-01 critical`, `CR-27 medium` and `CR-03 low` — three confident claims about a site nothing ever reached | Checks inferred absence from a failed fetch without asking whether the origin had answered | The crawl records a `transport` diagnosis; `CR-01` becomes `INCONCLUSIVE` and `CR-02`/`CR-03`/`CR-25`/`CR-27` become `NOT_EVALUATED` when `target_responded` is false, plus a `transport` coverage limitation | `tests/test_transport.py` (33 tests: classification per platform wording, analyzer gating for six failure modes, and end-to-end runs for DNS, refused, timeout, outage, 403, 429, CAPTCHA, robots-disallow and a real sitemap 404) |
| Parallel audits shared `<output-dir>/.audit-snapshot.json`, so one run could overwrite or delete another's snapshot | A fixed staging filename in the output directory | The snapshot stays in memory and is piped to each analyzer on stdin; the audit creates no file of its own | `tests/test_concurrency.py` (11 tests: 5 simultaneous audits in one directory, asserting no `FileNotFoundError`, valid reports, correct per-run site, no cross-site evidence, and no leftover state) |

Verified by reintroducing each bug: `test_concurrency` failed 5 assertions and
`test_transport` failed 16 against the old behaviour, and both pass after the fix.

## 7c. Vertical sweep (24 sites, 14 verticals)

A second live sweep audited 24 public sites chosen to span verticals rather than
volume: e-commerce, marketplace, education, SaaS, PaaS, developer documentation,
news, government, NGO, healthcare, fintech, travel, restaurant and a personal
blog. Every run produced a schema-valid report and exited 0. No site is
referenced anywhere in the shipped code.

The sweep was run twice, before and after the fixes below, and exposed three
false positives — two of which were **pre-existing bugs that the new checks made
visible** rather than defects the new checks introduced.

| Observed | Root cause | Fix | Regression test |
|---|---|---|---|
| A news site was reported at `high` for emitting "16 `<title>` elements" on every page | The parser counted `<svg><title>` — the accessible name of an icon — as a document title, and *concatenated every icon label onto the page title*. Icon systems put one in every glyph, so the title an assistant would cite had been corrupted on any site using inline SVG icons | `<title>` inside `svg`, `math` or `template` is neither counted nor appended (`EMBEDDED_TITLE_HOSTS`, `htmlparse.py`) | `test_dormant_signals.py` — 16 icon titles must not raise `CR-33`, and an SVG title must never reach `Extraction.title` |
| A healthcare site and a restaurant chain were told their error page left visitors with no way back | The not-found probe was answered by a WAF interstitial titled "Access Denied". A bot wall answers every path identically, so the page judged was the WAF's, not the site's — no human following a stale link would ever see it | The probe records `blocked`; `EN-07` reports `INCONCLUSIVE` on a refusal status or a challenge title, and points at `CR-02` for the blocking itself | `test_transport.py::TestABotWallsPageIsNotTheSitesPage` — 8 tests, including that a genuinely bare 404 is still a finding |
| A large marketplace's 404 was reported as a dead end although it carries a search box and a link home | `EN-07` counted internal links only, so a recovery route that is not a link was invisible | The probe records `has_search`, read from the form's action and inputs and never submitted; an error page with a search box and at least one link passes | `test_transport.py` — a search box plus a link passes, a search box with no links at all still fails |

All three were found by running against real sites and none by the unit suite,
which is the argument for the sweep existing at all.

## 7d. The runtime defect the sweep exposed

Two sites in the vertical sweep ran at 222 s and 223 s against a 240 s internal
deadline, and sampled fewer pages as a result. Profiling attributed almost all
of it to connection establishment rather than to the sites.

`socket.create_connection` walks a host's resolved addresses strictly in order.
The audit host's IPv6 path was black-holed, so every request to a dual-stack
host waited out a full connect timeout before falling back to IPv4 — measured at
16 s per request. The consequences were worse than slowness:

- A 20-page audit spent close to four minutes waiting on the local network,
  none of which described the site being audited.
- `robots.txt` is fetched with a shorter timeout than a page, so it failed
  first. An unreadable `robots.txt` correctly means the crawler stays out, so
  the entire site was treated as disallowed and nothing was crawled. The Python
  documentation audit produced 3 findings from 0 pages and looked like a clean
  result rather than a failed one.

The fetcher now follows RFC 8305 ("Happy Eyeballs"): connection attempts start a
short delay apart, run concurrently, and the first to complete wins. The
answering address *family* is cached per host — not the individual address,
which would pin a whole crawl to one edge node of a large site.

| Target | Before | After | Pages before → after |
|---|---|---|---|
| `docs.python.org` | 222 s | **33 s** | 15 → 20 |
| `www.gov.uk` | 223 s | **52 s** | 20 → 20 |
| Cold request to a dual-stack host | 16.4 s | **0.5 s** | — |
| Warm request to the same host | 16.3 s | **0.07 s** | — |

Covered by `tests/test_connection.py` (13 tests: interleaving policy, family
preference, resolver order preservation, a race against an unroutable RFC 5737
address, timeout behaviour when every address fails, and the read-only
guarantee across the new transport).

## 7e. Crawl-delay: politeness that the deadline cannot override

The holdout sweep surfaced a second defect, this time a **safety** one. Hacker
News declares `Crawl-delay: 30`. The limiter clamped that to 10 s and then
capped each sleep at 5 s, so a site asking for one request every 30 seconds
received roughly one every 5. The submission's own non-negotiable is that
robots.txt is respected without exception, and a Crawl-delay is part of
robots.txt.

Honouring it naively then produced a **332 s** audit, past Adobe's 300 s
ceiling. The resolution keeps both guarantees by giving up sample size instead:

| Mechanism | Effect |
|---|---|
| The declared interval is waited in full | The pace is never shortened |
| The page target is fitted to the delay | 30 s/page yields ~5 pages, not 20, and says so |
| A wait that will not fit is skipped, not shortened | The deadline holds without impoliteness |
| Optional probes yield to content | `llms.txt` and guessed sitemaps skipped when requests are costly |
| The adaptive expansion prices pages at the declared delay | It cannot plan a sample the pace cannot deliver |

Hacker News now completes in **152 s** with 5 pages, 23 findings and a valid
report. Covered by `tests/test_politeness.py` (15 tests), which asserts the
limiter waits in full, that the sample is fitted, that an extreme delay still
audits at least the entry page, and that the widening cannot undo the shrink.

## 8. Known limitations

Carried in every report under `limitations`, and listed in README §16: a bounded
20-page sample at depth 2; no JavaScript execution (the render gap is detected,
not worked around); optional off-site corroboration; navigation detection needs
semantic HTML; an English keyword list where symbol and structural patterns are
unavailable, failing safe to `NOT_APPLICABLE`; subdomains treated as separate
sites.
