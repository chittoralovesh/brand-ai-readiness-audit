# freshness-corroboration — check reference

Mechanism, evidence, gate, false-positive control, impact and fix per check.
`impact_base` is fixed; `breadth` and `confidence` are computed per run; the
orchestrator grades
(`../../audit-orchestrator/references/severity-model.md`).

---

## Where dates come from

Every date signal on a page is collected and tagged with its source, so a
"missing date" verdict cannot be produced by looking in only one place:

| Source | Read from |
|---|---|
| `json-ld` | `datePublished`, `dateModified`, `dateCreated`, `uploadDate` in any node, including `@graph` |
| `meta` | `article:published_time`, `article:modified_time`, `og:updated_time` |
| `html` | `<time datetime="…">`, falling back to the element's text |
| `header` | `Last-Modified` |
| `text` | The first ~900 characters of main content |
| sitemap | `<lastmod>` per URL |

Only ISO-8601 and unambiguous month-name forms are parsed. `03/04/2026` is
**ignored, not guessed** — reading it as either 3 April or 4 March would
manufacture evidence.

---

## Freshness

### FC-01 · No machine-readable date on time-sensitive pages · impact 3 · confidence high

**Mechanism.** Recency is one of the strongest selection signals for anything
that can go out of date. A page with no parseable date is scored as being of
*unknown* age, which is treated as worse than old: it loses to a competitor's
dated page even when yours is newer and better. A date rendered only as prose
inside a paragraph is frequently missed.
**Evidence.** Editorial pages with no `json-ld`, `meta` or `html` date signal.
**Gate.** Editorial content only — ≥150 words of main content under a
`/blog`, `/news`, `/article`, `/post`, `/insights`, `/press`, `/newsroom`,
`/research`, `/updates`, `/events`, `/journal`, `/release`, `/changelog` or
`/announcements` path. Everything else is `NOT_APPLICABLE`.
**False positive.** Demanding a publication date from an evergreen product page.
**Control.** The gate above; a date in visible text alone does not satisfy the
check but *is* recorded, so `FC-04` can compare it.
**Fix.** `datePublished` and `dateModified` in ISO-8601 with an offset; render
the visible date inside `<time datetime>` so the human and machine values cannot
drift; update `dateModified` only for substantive edits — refreshing it on every
deploy destroys the signal's meaning; mirror it in the sitemap `<lastmod>`.

### FC-02 · Newest dated content is years old · impact 2 · confidence medium

**Mechanism.** An entire site whose newest signal is years old reads as
abandoned. That reduces both the chance of being selected as a source and the
confidence attached to facts taken from it — including facts that are still true.
**Evidence.** The newest date observed across all pages and sitemap `lastmod`
values, with the age in months and the sample size.
**Gate.** ≥24 months **and** the site has a time-sensitive editorial section.
**False positive.** Calling a well-maintained evergreen site "stale".
**Control.** Without an editorial section the check reports `NOT_APPLICABLE` and
still prints the newest observed date, so a reader has the fact without the
accusation. With no date signal at all it reports `INCONCLUSIVE`, not `FAIL`.
**Fix.** Review the highest-value evergreen pages for accuracy and re-date only
what genuinely changed; add a short dated "what changed" note so the update is
verifiable rather than cosmetic. Publishing filler to look active is worse than
doing nothing.

### FC-03 · Dates in the future · impact 2 · confidence high

Future dates are treated as unreliable and commonly ignored altogether, silently
removing the recency signal the site was trying to publish. Reported at >2 days
ahead, which absorbs timezone skew. Usually a day/month transposition or a
scheduled post rendered publicly.

### FC-04 · A page's dates contradict each other · impact 2 · confidence high

**Mechanism.** Contradictory dates make *every* date on the page untrustworthy.
The usual outcome is that all of them are discarded, leaving the page at unknown
age — the same position as publishing no date at all, but harder to notice.
**Evidence.** Either `dateModified` earlier than `datePublished`, or a
machine-readable date differing from the visible date by more than 45 days.
**Gate.** Pages carrying ≥2 independent date signals.
**Control.** The 45-day tolerance absorbs the legitimate case where a visible
"published" date and a structured "modified" date differ.
**Fix.** Render every date on a page from the same field.

### FC-05 · Stale copyright year · impact 1 · confidence high

The copyright line is the cheapest signal a visitor uses to judge whether a site
is still operating; a year several behind reads as abandoned and undermines
trust in prices and contact details that are actually correct. Reported only at
**≥2 years** behind, and at impact 1 — it is a real but small signal, and
over-grading it would be exactly the kind of noise the rubric penalises. Fix by
rendering the year at build or request time.

### FC-06 · Placeholder or unfinished content · impact 2 · confidence high

Placeholder copy is indexed and quoted like any other text, so it can surface in
an answer attributed to the brand; for a visitor it is an unambiguous signal the
site is unfinished. Detects `lorem ipsum`, `coming soon`, `under construction`,
`placeholder text`, `your text here`, `sample text`, `todo:`, `tbd`, `test page`.
Evidence quotes the surrounding text so a reader can confirm it is not a false
match on ordinary prose.

### FC-07 · Sitemap `lastmod` · impact 1 · confidence high

`lastmod` is how a crawler decides which URLs are worth re-fetching. Without it,
updates are found on the crawler's schedule rather than yours, so a corrected
price can take weeks to reach answers. Two verdicts: **absent** (no entry carries
one) and **uniform** (>3 entries all carrying the identical value — the signature
of a build timestamp, which crawlers learn to ignore).

---

## Consistency and corroboration

### FC-08 · Site-wide contact number differs from the contact page · impact 2 · confidence medium

**Mechanism.** When a site states the same fact two different ways, an extractor
has no way to choose and will surface whichever it saw first — or neither. For
contact details specifically, an assistant repeating the wrong number sends real
customers to a dead line, and the inconsistency weakens the local-business
signals that depend on a single consistent name/address/phone.
**Evidence.** Distinct numbers found in the header/footer region across pages
versus in the contact page's main content.
**Gate.** Both a site-wide number **and** a contact/about page must exist.
**False positive.** Legitimate multiple numbers — sales versus support, regional
offices.
**Control.** Compares the *site-wide* set against the *contact-page* set and
reports only when the two are **disjoint**. Several numbers on one page never
trigger it. Numbers are normalised to their last nine digits so formatting and
country-prefix differences do not register as conflicts. Confidence `medium`.
**Fix.** Render every occurrence from one source; where several numbers are
intentional, label each explicitly and model them as `contactPoint` with
`contactType`.

### FC-09 · Too little distinguishes this brand from a same-named one · impact 3 (none) / 2 · confidence medium

**Mechanism.** Entity resolution fails by **merging**, not by returning nothing.
When a brand name is shared with another company, a product, a place or a common
word, a system with no distinguishing attributes picks the entity it already
knows best — and then answers questions about your brand using someone else's
facts. That failure never appears in the brand's own analytics, which is why it
persists.
**Evidence.** Which of five disambiguating attributes are present across the
sample, and which are absent:

1. a physical or postal location
2. a founding or established date
3. a registration or tax identifier
4. an explicit category sentence (`X is a Y that does Z`)
5. links to external profiles (`sameAs`)

Fewer than three present is reported.
**Why this framing.** "The brand has a common name" is neither measurable nor
actionable. What the site *supplies* is both.
**Control.** Confidence `medium` — the attributes are detected by pattern, and
the sample is bounded. Tagged `semantic-review`.
**Fix.** One sentence — *"<Brand> is a <category> based in <place>, founded in
<year>"* — on the home and about pages supplies three of the five at once, in the
place both a reader and an extractor will find it. Then add the registration
number, the `sameAs` array, and a shared `@id` so the attributes accumulate onto
one entity.

### FC-10 / FC-11 · Off-site corroboration · impact 2 / 3 · confidence low / medium

**Only evaluated when the caller supplies a research file** — see
`offsite-research.md`. Without one, `FC-10` is `NOT_EVALUATED` with an explicit
statement that no conclusion was drawn.

| Check | Fires when | Confidence |
|---|---|---|
| `FC-11` contradiction | A third-party source states a fact that disagrees with the site | `medium` |
| `FC-10` thin coverage | Fewer than 2 independent hosts observed describing the brand | `low` |

**The rule that matters.** *Absence from a small search sample is not proof of
absence.* `FC-10` is reported at `low` confidence, which the severity model caps
at `medium`, and its `applicability` says in as many words that it is evidence of
what was **not observed** rather than proof that nothing exists. No check in this
marketplace ever asserts that a brand is unindexed.
