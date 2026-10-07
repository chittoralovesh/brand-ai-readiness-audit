# ai-citability-audit — check reference

Each check states its mechanism, evidence, applicability gate, false-positive
control, impact rating and fix. `impact_base` is fixed per check; `breadth` and
`confidence` are computed per run; the orchestrator grades
(`../../audit-orchestrator/references/severity-model.md`).

Page-role inference — the gates that decide whether a schema expectation applies
at all — is documented separately in `applicability.md`.

---

## Structured data

### AC-01 · No structured data anywhere · impact 3 · confidence high

**Mechanism.** Structured data is the only channel that states a fact as a
*typed value* rather than as prose a machine must guess at. Without it, a system
that wants the organisation's name, a price, an address or an author has to
infer them from layout — which fails silently and produces confident but wrong
answers about the brand.
**Evidence.** "0 of N sampled pages contain parseable JSON-LD, microdata or
RDFa", with the pages listed.
**Applicable.** At least one HTML page parsed.
**False positive.** Reporting "no structured data" on a site that uses microdata
or RDFa instead of JSON-LD.
**Control.** `itemtype` and `typeof` attributes are counted as structured data.
Tagged `render-gap-symptom`, because a client-rendered site injects its JSON-LD
after load and the render gap is the real cause.
**Fix.** Start with site identity on the home page: one `Organization` (or
`Person`) with `name`, `url`, `logo`, `description`, `sameAs`; add `WebSite`;
then mark up whichever entity each template is actually about — and only that
one. Emit it server-side.

### AC-02 · Inconsistent coverage across templates · impact 1 · confidence high

**Mechanism.** Templates without markup are read as plain prose while their
siblings are read as typed entities, so facts on them are quoted less reliably.
**Applicable.** Some but not all pages carry structured data.
**Fix.** Move the markup into a shared layout partial parameterised by page type.

### AC-03 · JSON-LD that fails to parse · impact 3 · confidence high

**Mechanism.** A JSON syntax error causes the **entire block** to be discarded,
not partially read. The page behaves exactly as if it had no structured data —
while every dashboard that only checks for the presence of a `<script
type="application/ld+json">` tag reports success. That combination is why this
defect survives for years.
**Evidence.** The parser error message and the offending excerpt, per page.
**Applicable.** At least one page emitted a JSON-LD block. If none did, this is
`NOT_APPLICABLE`: absence is AC-01's business, not this check's.
**Fix.** The usual causes are a trailing comma, an unescaped quote or apostrophe
in a value, a raw newline in a string, or an unescaped `</script>` in a
description. Build the object as a data structure and serialise it with a JSON
encoder instead of concatenating strings in a template; add a CI check that
parses every emitted block.

### AC-06 / AC-07 / AC-08 · Entity-appropriate markup · impact 3 / 3 / 2 · confidence high

Gated on evidence from the page itself — see `applicability.md`. Pages that do
not match the gate are **excluded from the denominator**, and the check reports
`NOT_APPLICABLE` quoting the gate, so a reader can see the expectation was
considered and correctly dropped.

| Check | Applies to | Mechanism |
|---|---|---|
| `AC-06` `Product`/`Offer` | Pages showing a price *and* purchase controls | Carries exact name, price, availability and identifiers. Without it a shopping or answer surface must scrape a price out of layout — which it usually declines to do, so the product is not quoted, or is quoted with a stale price from a third-party listing. |
| `AC-07` `Article`/`BlogPosting` | Long-form pages under an editorial path with a date or byline | Supplies headline, author, publisher and the publication/modification dates. Recency is weighted heavily for anything that can go out of date; an undated article is treated as unknown age and loses to a dated competitor. |
| `AC-08` `LocalBusiness`/`PostalAddress` | Pages showing a street address with a phone number or opening hours | A visible address is prose to a machine. Structured `PostalAddress`, `geo`, `telephone` and `openingHours` are what let an assistant answer "where are they and are they open" — and keep *your* details, not an out-of-date directory listing, as the source. |

**Fix, in all three cases.** Emit the markup from the same view model that
renders the page, with values byte-identical to the visible ones. Do not add
`aggregateRating` unless real, visible reviews back it.

### AC-09 · Markup asserting values the page does not show · impact 3 · confidence medium

**Mechanism.** Structured data is trusted only while it agrees with the rendered
page. Markup asserting unseen values is treated as manipulation: the specific
values are discarded, and repeated mismatches reduce trust in the rest of the
site's markup. An unbacked `aggregateRating` is a well-known spam signal and
risks a manual action. This defect passes every syntax validator — the JSON is
valid, it is just untrue.
**Evidence.** Three comparisons: a `Product`/`Article` `name` absent from the
visible text; an `aggregateRating` with no review or rating text on the page; an
`offers.price` whose digits appear nowhere in the page text.
**False positive.** Currency and thousands-separator formatting differences;
names rendered inside an image or a heading the extractor normalised.
**Control.** Price comparison is digits-only and accepts substring matches in
either direction; name comparison normalises to alphanumerics and searches the
title, headings and body together; confidence is `medium`.
**Fix.** Generate the markup from the view model, not from a separate source;
add a test asserting the JSON-LD price equals the displayed price.

---

## Entity identity

### AC-04 · No machine-readable identity · impact 3 (missing) / 2 (no `sameAs`) / 1 · confidence high

**Mechanism.** Entity resolution is the step where an assistant decides *which
real-world thing* a page is about. With no declared identity node the system
guesses from the domain and page text, and a brand whose name is a common word
or shared with another company is routinely merged with the wrong entity — after
which every fact it repeats about you is really about someone else.
**Evidence.** The types present on the entry page and which identity properties
are absent.
**Applicable.** The entry page parsed.
**Fix.** One `Organization` (or `Person`) node with a stable `@id`, the exact
name you want repeated, `url`, `logo`, `description` and `sameAs`; reference the
same `@id` from `publisher`/`provider` elsewhere rather than repeating the object.

### AC-05 · The site states its own name inconsistently · impact 2 · confidence medium

**Mechanism.** Entity matching uses the name string. When structured data, the
title and the copyright line disagree, the matcher either splits the brand into
several weak entities or picks the wrong one — which is how a company ends up
described with a subsidiary's or a competitor's facts.
**Evidence.** Every name signal found, quoted with its source.
**Applicable.** At least two independent name signals; otherwise `INCONCLUSIVE`.
**False positives.** (a) Title word order — "Brand | Page" and "Page | Brand" are
both common, so taking the last segment guesses wrong about half the time.
(b) "Acme" vs "Acme Ltd".
**Controls.** (a) The brand element of a title is the segment **repeated across
pages**, requiring at least two pages. (b) Names where one normalises to a prefix
of the other are treated as consistent.
**Fix.** Pick one canonical string — including or excluding the legal suffix —
and use it in `Organization.name`, `og:site_name`, the title template and the
footer. Model genuine trading names as `alternateName` on one entity.

### AC-10 · Entities with no stable `@id` · impact 1 · confidence high

`@id` is what says the `Organization` on the pricing page and the one on the
article are the same thing. Without it each page creates a fresh anonymous node,
so signals never accumulate and the brand stays weakly defined no matter how many
pages describe it. Fix with fragment URIs on canonical URLs
(`https://host/#organization`).

### AC-18 · No external identity links · impact 2 · confidence medium

**Mechanism.** A claim that exists in one place is fragile. Machines weight a
fact by how many independent sources agree, and resolve identity by following
links between a site and profiles that already exist in a knowledge graph. A site
with no outbound identity links gives the graph nothing to join on — and any
similarly-named organisation *with* profiles will win as the referent.
**Evidence.** External hosts linked, checked against a list of ~30 registry,
profile and review hosts, plus any declared `sameAs`.
**Control.** Both outbound links **and** `sameAs` count; either passes.
Confidence `medium` because the sample is bounded.
**Fix.** Claim the profiles relevant to the category, publish identical name,
description and URL on each, link them from the site *and* list them in `sameAs`.
Prefer a few authoritative profiles over many directory listings.

---

## Extractable facts

### AC-11 · The entry page never says what the site offers · impact 3 · confidence high

**Mechanism.** The opening heading and first paragraph are what both a retrieval
system and a first-time visitor use to decide what this site is. A brand-only or
placeholder opening gives an assistant nothing to match a question against, so
the site is never surfaced for the category it competes in.
**Evidence.** The H1 and the opening text, quoted, with the word count.
**Applicable.** The entry page parsed.
**False positive.** Judging copy quality subjectively.
**Control.** Fails only on **objective** conditions: no H1; an H1 matching a
placeholder pattern (`Home`, `Welcome`, `Untitled`, `Coming soon`); an H1 equal
to the brand name alone; or fewer than 15 words of opening body text. Anything
softer is tagged `semantic-review` and left to the orchestrator's optional pass.
**Fix.** `<what it is> for <who it is for>` as the H1 — concrete nouns, no slogan
— followed by two or three sentences with one verifiable specific, server-rendered
above any interactive component.

### AC-12 · Nothing concrete to quote · impact 2 · confidence medium

**Mechanism.** Assistants prefer to quote sentences that assert something
checkable. Pages built entirely from abstract positioning language offer no such
sentence, so even when retrieved they are summarised away rather than cited —
the brand is read but never named.
**Evidence.** A count of figures with units, prices, dates and definitional
verbs, per page; ≤1 is reported.
**Control.** Only pages with ≥60 words are in the denominator; confidence
`medium`; tagged `semantic-review`.

### AC-13 · Facts carried by images · impact 3 (image-heavy) / 2 · confidence high/medium

**Mechanism.** Text inside an image is invisible to a text crawler and to a
screen reader. A price list, menu, specification table or opening-hours graphic
that exists only as an image cannot be extracted, so the assistant omits the fact
or takes it from a third party that may be out of date.
**Evidence.** Content-area images with **no `alt` attribute**, plus pages with
≥5 content images and <150 words.
**False positive.** Flagging decorative images.
**Control.** `alt=""` is an explicit, correct declaration that the image is
decorative and is **never** counted; only a missing attribute is. Images marked
`aria-hidden` or `role="presentation"` are excluded, as are images outside the
main content region. Shares `dedupe_key: facts-in-images` with `EN-13`.
**Fix.** Alt text describing what the image conveys; publish factual content as
HTML text (a real table, not a screenshot of one) beside or instead of the image.

### AC-19 · Tables without header cells · impact 2 · confidence medium

**Mechanism.** A value in a table means nothing without its column. `<th>` is
what binds the two: it tells an extractor that the 490 in this cell is the
monthly price of the tier named in that row. With only `<td>`, the grid is a bag
of strings whose relationships exist purely in visual position - so a comparison
or pricing table, which is exactly the content an assistant is most often asked
to summarise, is the content it is least able to quote correctly. The same
markup is what lets a screen reader announce the column while reading a cell.
**Evidence.** Per page, the number of tables, the count of `<th>` cells and the
count of `<caption>` elements.
**Applicable.** Only pages that contain at least one `<table>`. A site with no
tables reports `NOT_APPLICABLE`; facts held in prose belong to `AC-12` and facts
held in images to `AC-13`.
**False positive.** A layout table, which legitimately has no headers because it
carries no data.
**Control.** Confidence is `medium` precisely because a count cannot separate a
layout table from an unlabelled data table. The remediation covers both cases,
and the denominator is pages that have a table rather than all pages.
**Fix.** `<th scope="col">` on the header row and `<th scope="row">` on row
labels, wrapped in `<thead>`, plus a `<caption>` naming what the table shows;
rebuild layout-only tables with CSS grid; and emit the same values as
`Product`/`Offer` JSON-LD so the facts are typed as well as labelled.

### AC-14 · Content delivered as documents or frames · impact 2 · confidence medium

Documents and third-party frames are separate resources: many fetchers do not
download them, frame content is attributed to the other origin, and neither
carries your page's headings or links. Reported at ≥3 document links in main
content, or a page relying on an iframe with <150 words of its own text.

### AC-15 · Boilerplate outweighs unique content · impact 2 · confidence medium

Near-duplicate detection compares whole documents. When most of every page is the
same menu and footer, pages look alike and get collapsed, and the small unique
portion is diluted in relevance scoring. Reported below a 35 % main-content
ratio, on pages with ≥80 words, across ≥2 pages. Confidence is `medium` because a
site using `<div class="header">` rather than `<header>` has no
machine-detectable chrome.

### AC-16 · No preview metadata · impact 1 · confidence high

Open Graph is the summary layer chat clients, messaging apps, social platforms
and several assistant surfaces read when rendering a link. Without it they fall
back to scraping and produce an unbranded or truncated card, so a link shared on
your behalf lands looking untrustworthy.

### AC-17 · Unattributed editorial content · impact 2 · confidence medium

Attribution is one of the few signals a retrieval system has for whether advice
is accountable. Anonymous guidance competes badly against attributed guidance on
the same topic, and gives an assistant nothing to name when explaining where an
answer came from. Gated on the same editorial inference as `AC-07`; a visible
byline **or** a schema `author` satisfies it.
