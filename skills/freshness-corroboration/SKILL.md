---
name: freshness-corroboration
description: Check whether a website's facts are current, self-consistent and corroborated - machine-readable publication and modification dates on time-sensitive pages, content whose newest date is years old, contradictory or future dates, stale copyright years, placeholder and unfinished content, sitemap lastmod signals, contact details that differ between the footer and the contact page, and whether the site supplies enough disambiguating attributes (location, founding date, registration identifier, explicit category, sameAs profiles) to stop a machine merging the brand with a similarly-named entity. Optionally corroborates brand facts against independent public sources when a web-search tool is supplied; reports NOT_EVALUATED rather than guessing when it is not. Use as part of a brand AI-readiness audit when diagnosing why a brand reads as stale, unverified, or is confused with something else.
license: Apache-2.0
compatibility: Requires Python 3.9+ (standard library only). Runs fully offline against an existing snapshot; the optional off-site corroboration step consumes a research file the caller may produce with a web-search tool.
allowed-tools: Bash Read
metadata:
  author: adobe-university-hackathon-2026-round-3
  version: "1.0.0"
  role: analyzer
  pillar: ai-discoverability
---

# Freshness and corroboration audit

Two failure modes, and they are related.

**Staleness.** A page with no parseable date is scored as being of *unknown*
age, which is treated as worse than old: it loses to a dated competitor even
when it is newer and better. Dates that contradict each other are usually
discarded altogether, which quietly produces the same result.

**Uncorroborated identity.** A fact stated in exactly one place is fragile.
Machines weight a claim by how many independent sources repeat it, and they
resolve identity by joining a site to records that already exist. A brand that
supplies nothing to join on stays an isolated, low-confidence entity — and any
similarly-named organisation with real profiles wins the tie.

## When to use

Called by `audit-orchestrator` as part of a full audit. Run it directly only to
re-analyse an existing snapshot.

## Input and output

```bash
python scripts/check_freshness.py --snapshot snapshot.json \
    [--research research.json] --out freshness.json
```

Input: a site snapshot, plus an optional research file. Output: a check
document. The on-site half needs no network access at all.

## The rule about off-site research

**Off-site research is optional and must never be invented.**

- With a research file: use at most 4 bounded searches, compare *facts* rather
  than ranking positions, and record the exact source URLs.
- Without one: emit `NOT_EVALUATED` for corroboration and say so in coverage.
  Do not fail the audit, and do not substitute a guess.
- **Absence from a small sample is not proof of absence.** Where thin
  third-party coverage is reported it is reported at `low` confidence, which the
  severity model caps at `medium` — the finding can never be presented as
  certain. It is evidence of what was *not observed*, never proof that nothing
  exists.

Schema and query guidance: `references/offsite-research.md`.

## Reporting rules

- **No evidence, no finding.** Only `FAIL` becomes a finding.
- **Quantify**, and state the observation window.
- **No severity here.** Report `impact_base`, `breadth` and `confidence`.

## What it checks

Full mechanism, gate and false-positive control per check: `references/checks.md`.

**Freshness** — `FC-01` no machine-readable date on time-sensitive pages ·
`FC-02` newest observed date years old · `FC-03` dates in the future ·
`FC-04` a page's own dates contradicting each other · `FC-05` a copyright year
years behind · `FC-06` placeholder or unfinished content · `FC-07` sitemap
`lastmod` missing, or identical across every URL (the signature of a build
timestamp, which crawlers learn to ignore).

**Consistency and corroboration** — `FC-08` the site-wide contact number
differing from the contact page · `FC-09` too few disambiguating attributes ·
`FC-10`/`FC-11` off-site corroboration and contradiction, when research is
supplied.

## `FC-09`, the check worth understanding

Entity ambiguity is usually described as "the brand has a common name". That is
not measurable and not actionable. What *is* measurable is whether the site
supplies the attributes a system needs to break a tie:

1. a physical or postal location,
2. a founding or established date,
3. a registration or tax identifier,
4. an explicit category sentence ("X is a Y that does Z"),
5. links to external profiles (`sameAs`).

Fewer than three present is reported, with the missing ones named. The fix is
correspondingly concrete: one sentence of the form *"<Brand> is a <category>
based in <place>, founded in <year>"* supplies three of the five at once, in the
place both a reader and an extractor will find it.

## False-positive controls

| Risk | Control |
|---|---|
| Calling an evergreen site stale | `FC-02` only reports staleness where the site has a time-sensitive editorial section; otherwise `NOT_APPLICABLE` with the newest date stated. |
| Nagging about a copyright year | Only reported at ≥2 years behind, at `impact_base` 1. |
| Misreading a date format | Only ISO-8601 and unambiguous month-name forms are parsed; ambiguous numeric forms are ignored rather than guessed. |
| "No dates" on a site that has them in prose | Every source is checked: JSON-LD, `article:*` meta, `<time datetime>`, `Last-Modified`, and visible text. |
| Multiple legitimate phone numbers | `FC-08` compares the *site-wide* number against the *contact page* number and only reports when the two sets are disjoint; regional or departmental numbers on one page do not trigger it. |
| Concluding a brand is unindexed | Never asserted. Thin coverage is `low` confidence and explicitly framed as "not observed", not "does not exist". |

## Files

| Path | Purpose |
|---|---|
| `scripts/check_freshness.py` | All checks; standard library only. |
| `references/checks.md` | Per-check mechanism, gate, evidence, impact and FP control. |
| `references/offsite-research.md` | Research file schema, query budget and the rules for using it. |
