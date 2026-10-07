---
name: engagement-audit
description: Check whether a visitor who lands on a website can orient, understand the offer, find the next action and keep moving - a primary call to action on the entry page, descriptive versus generic link labels, navigation that stays consistent between pages, content pages that dead-end with no onward links, pages present in the sitemap but linked from nowhere, broken links in the visitor path, an error page that offers a route back, discoverability of contact, about and privacy pages, pricing discoverability on commercially-intended sites, scanability of long pages, mobile viewport, form labelling (inspected, never submitted), image text alternatives and breadcrumbs on deep pages. Reads a snapshot produced by audit-orchestrator and emits check results with quantified evidence; it does not fetch anything itself. Use as part of a brand AI-readiness audit when diagnosing why visitors who arrive from search or an AI citation leave without engaging.
license: Apache-2.0
compatibility: Requires Python 3.9+ (standard library only). No network access needed - it analyses an existing snapshot.
allowed-tools: Bash Read
metadata:
  author: adobe-university-hackathon-2026-round-3
  version: "1.0.0"
  role: analyzer
  pillar: on-site-engagement
---

# On-site engagement audit

Being cited is half the problem. The other half starts the moment someone
follows the citation.

Traffic from a search result or an assistant's answer arrives **deep and
without context**: no home page, no prior browsing, often no idea which site
this is. The checks here follow the order a visit actually fails in —
orientation, next action, continuity, friction.

## When to use

Called by `audit-orchestrator` as part of a full audit. Run it directly only to
re-analyse an existing snapshot.

## Input and output

```bash
python scripts/check_engagement.py --snapshot snapshot.json --out engagement.json
```

Input: a site snapshot (`../audit-orchestrator/references/snapshot-schema.md`).
Output: a check document (`../audit-orchestrator/references/check-result-contract.md`).

## Safety

Every signal is read from HTML the orchestrator already captured. This skill
performs no network access at all, and therefore cannot log in, submit a form,
add to a cart, send a message or change any setting. Forms are **inspected**
— labels, field counts, submit controls — never submitted.

## Reporting rules

- **No evidence, no finding.** Only `FAIL` becomes a finding.
- **Quantify**, with the sample as the denominator.
- **No severity here.** Report `impact_base`, `breadth` and `confidence`.
- **Share a `dedupe_key`** with the discoverability skills where the same defect
  hurts both audiences — broken links, missing viewport, missing alt text. The
  orchestrator then merges them into one finding that states both consequences,
  instead of the report saying the same thing twice.
- **Do not turn taste into a finding.** Colour, spacing, tone and layout
  preference are out of scope. Everything here is a structural or functional
  property that can be observed in markup.

## What it checks

Full mechanism, gate and false-positive control per check: `references/checks.md`.

**Orientation** — `EN-01` no obvious next action on the entry page ·
`EN-02` links labelled "read more"/"click here" or not at all ·
`EN-08` contact, about or privacy pages not linked anywhere in the sample ·
`EN-09` a commercially-intended site publishing neither prices nor a route to a
quote.

**Continuity** — `EN-03` navigation changing substantially between pages ·
`EN-04` content pages that dead-end · `EN-05` sitemap pages linked from nowhere ·
`EN-14` deep pages with no breadcrumb.

**Friction** — `EN-06` broken links in the visitor path ·
`EN-07` an error page with no route back · `EN-10` long pages with no scanning
structure · `EN-11` no mobile viewport · `EN-12` unlabelled or oversized forms ·
`EN-13` images with no text alternative.

## The checks that matter most for cited traffic

**`EN-01`, primary action.** A menu asks the visitor to choose; a primary action
tells them what to do. A visitor arriving from a citation has no prior context,
so an entry page with no labelled next step pushes the whole decision cost onto
them — and the usual resolution is the back button.

**`EN-07`, error-page recovery.** Every stale external link, mistyped URL and
outdated citation lands here. A bare error page turns each of those arrivals
into a lost visitor; the same page inside the normal layout recovers a large
share of them, at almost no cost.

**`EN-14`, breadcrumbs.** Most search and assistant traffic lands deep, not on
the home page. A breadcrumb is the cheapest possible answer to "where am I, and
what else is near this?" — and its structured-data form also tells retrieval
systems where the page sits in the hierarchy.

**`EN-02`, link labels.** "Read more" predicts nothing for a visitor, carries no
topical signal to the destination page, and produces an unusable list for anyone
navigating by links with a screen reader. One rewrite fixes all three.

## False-positive controls

| Risk | Control |
|---|---|
| Demanding a CTA on an informational site | `EN-01` only runs when the entry page has ≥30 words, and accepts any action-labelled link *or* button-styled link, including "Read the docs" and "Browse the catalogue". |
| Demanding prices from a non-commercial site | `EN-09` requires ≥3 commercial-intent phrases in the site's *own* language; otherwise `NOT_APPLICABLE`. |
| Reporting a shallow site for missing breadcrumbs | Requires ≥2 pages that are two or more levels deep. |
| Reporting a short page as unscannable | Requires ≥300 words. |
| Reporting a labelled form as unlabelled | `<label>` elements are counted as well as `aria-label`, `aria-labelledby` and `title`; a form with enough labels for its inputs passes. |
| Reporting decorative images | `alt=""` is a correct declaration; only a missing attribute counts. |
| "Page X isn't linked" when X wasn't crawled | `EN-05` and `EN-08` state the sample bound explicitly and are reported at `medium` confidence. |
| Nav-consistency noise on a small site | Requires ≥3 pages that expose a `<nav>` region. |

## Files

| Path | Purpose |
|---|---|
| `scripts/check_engagement.py` | All checks; standard library only. |
| `references/checks.md` | Per-check mechanism, gate, evidence, impact and FP control. |
