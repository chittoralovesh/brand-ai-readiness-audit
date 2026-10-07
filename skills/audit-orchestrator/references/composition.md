# Composition rules

How four skills' check documents become one report. Implemented in
`../scripts/auditlib/compose.py`.

The point of composing rather than concatenating is that a report which lists
every observation separately is a worse product than one that says *this is the
cause, these are its symptoms, fix it in this order*. Adobe's brief calls that
out directly, and it is what these rules exist to produce.

---

## Step 1 — Selection

Only `FAIL` becomes a finding.

| Status | Where it goes | Why |
|---|---|---|
| `FAIL` | `findings` | An observation established the defect. |
| `PASS` | `coverage.checks` | Proves the check ran — useful evidence of what is *not* wrong. |
| `NOT_APPLICABLE` | `coverage.checks` | The precondition was absent. The stated gate shows the expectation was considered and correctly dropped. |
| `NOT_EVALUATED` | `coverage.checks` + `limitations` | Could not observe (no capability, no budget, page blocked). |
| `INCONCLUSIVE` | `coverage.checks` + `limitations` | Observed, but the evidence does not settle it. |

**Uncertainty is never converted into a defect.** A missing capability is a
coverage limitation, not a finding about the site.

---

## Step 2 — De-duplication

Skills overlap on purpose. A broken link is a crawl-path defect *and* a visitor
dead end; a missing viewport is a mobile-rendering defect *and* an engagement
defect. Reporting each twice would inflate the count and split the fix.

Checks that observe the same underlying defect declare the same `dedupe_key`.
The orchestrator then:

1. keeps the member with the highest severity score (ties broken by `check_id`);
2. unions `affected_urls`, `evidence_items` and `tags`;
3. records the others in `merged_from`;
4. sets `also_affects` when a merged member belongs to the other pillar, and
   appends that member's mechanism under *"Same defect, second consequence"*.

The merged finding therefore states both consequences, which is a **stronger**
argument for fixing it than either alone.

Shared keys currently in use:

| `dedupe_key` | Merged from |
|---|---|
| `broken-internal-links` | `CR-25` (crawl paths) + `EN-06` (visitor paths) |
| `viewport-missing` | `CR-19` (rendering) + `EN-11` (engagement) |
| `facts-in-images` | `AC-13` (extraction) + `EN-13` (accessibility) |

---

## Step 3 — Root-cause rollup

Some defects mechanically cause others. A page that only exists after JavaScript
runs has no title, no headings, no structured data and no text **because of
that**. Listing those as four more findings is a laundry list: it triples the
apparent problem count and points the team at four fixes that would not work.

A check that is a root cause tags itself `render-gap-root-cause`. Checks it
mechanically causes tag themselves `render-gap-symptom`. When the root cause
fails on **≥50 %** of the sample, the symptoms are folded into it:

- they leave `findings` entirely;
- their evidence is appended to the root finding under
  *"Downstream effects observed on the same pages"*;
- they are listed in `contributing_symptoms` and in the top-level
  `finding_rollups`, so nothing is hidden.

The threshold matters. Below 50 % the symptoms are probably independent defects
on other templates and stay as separate findings.

Rollup is deliberately a short, explicit table rather than an inference. Rollup
that cannot be audited is indistinguishable from dropping findings.

---

## Step 4 — Grading

`references/severity-model.md`. One policy, applied here and nowhere else.

---

## Step 5 — Correlation

Some combinations mean more than the sum of their parts. These go in
`root_cause_analysis` as a **diagnosis with an ordered fix sequence**, keyed on
`dedupe_key` so a check id carrying several verdicts is not mixed up.

| ID | Fires when | What it tells the reader |
|---|---|---|
| `RC-01` | Access checks all pass, AI crawlers not blocked, **and** ≥2 of {no structured data, low fact density, no value proposition, thin content} | The bottleneck is not crawling. Teams in this position spend months on indexing work that changes nothing, because the failure is one step later in the pipeline. |
| `RC-02` | `noindex` findings overlap URLs the sitemap lists | The site is simultaneously requesting and forbidding indexation of the same URLs — the classic signature of a staging configuration reaching production. Invisible in a browser. |
| `RC-03` | No sitemap **and** (dead-end pages or orphan pages) | Discovery has a single point of failure: the entry page's menu. New pages wait for a re-crawl of one URL. |
| `RC-04` | ≥2 of {inconsistent name, markup contradicting the page, inconsistent contact details, too few disambiguating attributes, no identity node, no external identity links} | Entity resolution will fail by *merging*, so the outcome is confident misattribution rather than absence — and nothing in the brand's own analytics will show it. |

Correlations **never create findings**, so severity counts stay honest. They
reference existing finding IDs.

---

## Step 6 — Ordering and identity

Sort deterministically (`severity.sort_key`), *then* assign `F-001…`. Assigning
IDs before sorting would make them unstable between runs. `priority_rank` mirrors
the position.

---

## Step 7 — Proactive recommendations

Opportunities where **no defect was found**. Every one is gated on evidence from
this site and carries a rationale, a mechanism, a priority, the evidence that
triggered it, and concrete steps.

| ID | Gate |
|---|---|
| `PR-01` publish `llms.txt` | `/llms.txt` absent and ≥3 content pages. Framed as a convention, not a standard — which is exactly why it is a recommendation and not a finding. |
| `PR-02` question-shaped headings | Zero question-shaped headings across the sample. |
| `PR-03` broaden corroboration | `sameAs` present but <4 distinct external hosts linked. |
| `PR-04` publish a feed | Editorial pages exist, no RSS/Atom/JSON feed link found. |
| `PR-05` structured `contactPoint` | Contact details in text but no `contactPoint`/`telephone` in JSON-LD. |
| `PR-06` date the evergreen pages | ≤20 % of sampled pages carry a machine-readable date, and `FC-01` did not already fail. |

Nothing is emitted "to fill space". If none of the gates match, the array is
empty and that is a correct outcome.

---

## Step 8 — Coverage, limitations, validation

`coverage` lists every check with its status and note, plus the crawl statistics
— so a reader can tell "we checked and it was fine" from "we could not check".

`limitations` collects every `NOT_EVALUATED`/`INCONCLUSIVE` with its reason,
plus crawl-level bounds: robots exclusions, deliberately-skipped state-changing
URLs, budget skips, and the fact that findings describe a bounded sample.

The report is then validated, deterministically repaired if needed, re-validated,
and the outcome recorded in `report_validation`. See `report-schema.md`.
