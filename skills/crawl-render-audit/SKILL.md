---
name: crawl-render-audit
description: Check whether a machine can reach a website's pages and read them from the server response alone - robots.txt policy including AI and answer-engine crawler blocks, gaps between the policy robots.txt declares and what a CDN or WAF actually enforces, bot walls and anti-bot interstitials, HTTP errors, redirect chains, noindex and X-Robots-Tag directives, canonical conflicts, meta-refresh redirects, duplicated head metadata, missing or invalid hreflang on localised sites, sitemap availability, client-side-render gaps where content only exists after JavaScript, thin server HTML, titles, meta descriptions, heading structure, language and viewport declarations, broken internal links, soft 404s, crawl traps and response weight. Reads a snapshot produced by audit-orchestrator and emits check results with quantified evidence; it does not fetch anything itself. Use as part of a brand AI-readiness audit when diagnosing why crawlers or AI fetchers cannot access or parse a site.
license: Apache-2.0
compatibility: Requires Python 3.9+ (standard library only). No network access needed - it analyses an existing snapshot.
allowed-tools: Bash Read
metadata:
  author: adobe-university-hackathon-2026-round-3
  version: "1.0.0"
  role: analyzer
  pillar: ai-discoverability
---

# Crawl and render audit

The first two of the three things that must succeed before a page can be cited:
**the crawler has to be let in**, and **it has to be able to read the page**.
(The third — extracting a specific fact — belongs to `ai-citability-audit`.)

## When to use

Called by `audit-orchestrator` as part of a full audit. Run it directly only to
re-analyse an existing snapshot without re-crawling — for example when checking
whether a fix landed.

## Input and output

```bash
python scripts/check_crawl_render.py --snapshot snapshot.json --out crawl.json
```

Input: a site snapshot (`../audit-orchestrator/references/snapshot-schema.md`).
Output: a check document (`../audit-orchestrator/references/check-result-contract.md`).

This skill performs **no network access**. Everything it reports was captured by
the orchestrator's single bounded crawl, which is what keeps the audit polite
and fast.

## Reporting rules

- **No evidence, no finding.** A check emits `FAIL` only from an observation.
  Where a precondition is absent it emits `NOT_APPLICABLE`; where it could not
  observe, `NOT_EVALUATED` or `INCONCLUSIVE`. Only `FAIL` becomes a finding.
- **Quantify.** Evidence carries its own denominator — "5 of 6 sampled pages",
  never "the site has thin content".
- **No severity here.** Each check reports `impact_base` (0–4), `breadth`
  (affected / applicable / unit) and `confidence`. The orchestrator applies the
  one severity policy, so grades stay comparable across skills.
- **Share a `dedupe_key`** with any other skill that observes the same defect
  (broken links, viewport) so the orchestrator merges rather than double-counts.

## What it checks

Grouped by the question each group answers. Every check's mechanism, gate,
false-positive control and impact rating is in `references/checks.md`.

**Was the crawler let in?**
`CR-01` entry URL reachable · `CR-02` bot wall or anti-bot interstitial ·
`CR-03` robots.txt readable (a 5xx robots.txt is treated by major crawlers as
*disallow all*, so it is graded as severely as an explicit block) ·
`CR-04` AI and answer-engine crawlers disallowed · `CR-05` search crawlers
disallowed · `CR-06` pages the site links to are disallowed ·
`CR-08`/`CR-09`/`CR-10` `noindex`, page-level `nofollow`, `noai`.

**Could it read the page?**
`CR-11` content exists only after JavaScript runs · `CR-12` very little
extractable text · `CR-13`/`CR-14` titles missing or duplicated ·
`CR-15` meta descriptions · `CR-33` head metadata declared more than once (two `<title>` elements, or a repeated `robots`, lets the parser rather than the author choose what is indexed) · `CR-16` heading hierarchy ·
`CR-17` `<html lang>` · `CR-18` semantic landmarks · `CR-19` mobile viewport.

**Will signals survive?**
`CR-20`–`CR-23` canonical missing, collapsed onto another page, cross-domain, or
duplicated · `CR-24` redirect chains · `CR-32` meta-refresh used as a redirect, which browsers follow and fetchers usually do not · `CR-34` localised pages with no hreflang alternates, or alternates whose codes are invalid or omit a self-reference · `CR-25` broken internal links ·
`CR-26` soft 404s · `CR-27` sitemap availability and declaration ·
`CR-28` parameter-driven crawl traps · `CR-29` response weight and compression ·
`CR-30` plain HTTP.

## The two checks that carry the most weight

**`CR-11`, render dependency.** Most AI fetchers read the raw HTTP response and
do not execute JavaScript. If the text only appears after hydration, they store
an empty page — the site looks contentless to machines while looking complete to
people. This is the most common reason a visually rich site is never quoted, and
it is the one defect that mechanically causes many others, so it is tagged
`render-gap-root-cause` and the orchestrator folds its symptoms into it.

To avoid false positives it requires **two independent signals** plus low text.
A framework marker alone proves nothing: most frameworks render on the server.

**`CR-04`, AI crawler policy.** Invisible to classic SEO tooling and decisive
for citation. The check distinguishes *training* crawlers from *live retrieval
fetchers* — blocking the former is a business decision, blocking the latter
removes the brand from answers a user explicitly asked for — and reports which
was blocked and by which rule.

## False-positive controls

| Risk | Control |
|---|---|
| Calling a JS-rendered site broken | Two independent signals plus <150 words required. |
| Calling a healthy site bot-walled | Status codes *and* interstitial text markers; ratio reported. |
| Flagging an absent robots.txt | A missing robots.txt means "crawl everything" — reported `PASS`. |
| Flagging plain HTTP on a test host | Localhost and private/reserved hosts are excluded. |
| Reporting a HEAD-hostile server as broken | Link probes re-check with a ranged GET before calling a link broken. |
| Flagging robots exclusions that are correct | Only exclusions covering URLs the site itself links to are reported. |
| Assuming missing landmarks matter on a stub | Only pages with ≥120 words are in the denominator. |

## Files

| Path | Purpose |
|---|---|
| `scripts/check_crawl_render.py` | All checks; standard library only. |
| `references/checks.md` | Per-check mechanism, gate, evidence, impact and FP control. |
