# engagement-audit — check reference

Mechanism, evidence, gate, false-positive control, impact and fix per check.
`impact_base` is fixed; `breadth` and `confidence` are computed per run; the
orchestrator grades
(`../../audit-orchestrator/references/severity-model.md`).

**Scope discipline.** Everything here is a structural or functional property
observable in markup. Colour, spacing, tone, brand voice and layout preference
are out of scope — turning taste into a finding is how an audit loses the reader's
trust, and the rubric penalises it directly.

**Safety.** Every signal is read from HTML the orchestrator already captured.
This skill performs no network access, so it cannot log in, submit a form, add to
a cart, send a message or change a setting. Forms are *inspected*, never
submitted.

---

## Why this pillar is scored the way it is

Traffic from a search result or an assistant's citation arrives **deep and
without context**: no home page, no prior browsing, often no idea which site this
is. That is a different visit from the one most sites are designed for, and it
fails in a predictable order — orientation, then next action, then continuity,
then friction. The checks follow that order.

---

## Orientation

### EN-01 · No obvious next action on the entry page · impact 3 · confidence medium

**Mechanism.** A visitor arriving from a citation has no prior context. If the
landing page does not name one obvious next step, the decision cost falls on them
and the common resolution is to leave. This is distinct from having navigation: a
menu asks the visitor to *choose*; a primary action tells them what to *do*.
**Evidence.** Count of main-content links that are button-styled or
action-labelled, plus `<button>` elements; the opening text is quoted.
**Gate.** The entry page has ≥30 words of main content. Below that the render and
thin-content findings already explain the problem, and this check reports
`NOT_EVALUATED` rather than piling on.
**False positive.** Demanding a commercial CTA from an informational, reference
or personal site.
**Control.** The CTA vocabulary is intentionally broad and non-commercial —
`browse`, `read the docs`, `documentation`, `watch`, `find`, `compare`, `search`
count alongside `buy`, `book` and `sign up`. A link styled as a button counts
regardless of its wording. Confidence is `medium`, and the check is tagged
`semantic-review` so the optional pass can overturn it.
**Fix.** One primary action on the first screen, labelled with the **outcome**
rather than the mechanism — "Get a quote for your fleet", not "Submit"; "Read the
setup guide", not "Learn more". At most one secondary action, so the primary one
stays unambiguous.

### EN-02 · Generic or empty link labels · impact 2 · confidence high

**Mechanism.** Link text is the only preview a visitor gets before committing a
click, and it is also the anchor text a crawler uses to understand the
destination. "Read more" predicts nothing for either: the visitor hesitates or
clicks blindly, and the destination page loses the topical signal its inbound
links should have carried. Screen-reader users navigating by link list get an
unusable list of identical entries.
**Evidence.** Count and ratio of links whose text is empty or matches a generic
label, with examples showing the label and its destination.
**Gate.** Reported above a **12 %** ratio, so one or two "read more" links do not
trigger it.
**Control.** `aria-label` and `title` are accepted as the label when the visible
text is empty, so icon links that are correctly annotated pass.
**Fix.** Name the destination in the link text; annotate icon-only links with
`aria-label`; in card layouts wrap the heading in the link rather than adding a
separate "more" link.

### EN-08 · Contact, about or privacy pages not linked · impact 3 (contact) / 2 · confidence medium

**Mechanism.** These are the checks a cautious visitor performs before spending
money or sharing data; their absence reads as evasiveness. The same pages are also
where an organisation states the entity facts — who it is, where it is — that
assistants need to describe it correctly, so this defect costs both pillars.
**Evidence.** Which of the three routes was not found, and how many links were
examined.
**Control.** Matches both URL patterns and link text, in several languages
(`kontakt`, `contacto`, `impressum`). Bounded by the crawl sample, stated in
`applicability`, reported at `medium` confidence.
**Fix.** Persistent footer links; publish contact details as selectable text as
well as a form — a form gives a machine nothing to extract and gives the visitor
no alternative if it fails.

### EN-09 · A commercial site publishes no price and no route to one · impact 2 · confidence medium

**Mechanism.** Cost is the question most commercial visitors want answered first,
and price questions are among the most common asked of assistants. A site with no
price and no stated route to one cannot answer them, so the assistant answers
from a competitor or a third-party listing — and the visitor who was ready to buy
leaves to find the number elsewhere.
**Gate.** **≥3 commercial-intent phrases in the site's own language.** An
informational, documentation, portfolio or non-profit site reports
`NOT_APPLICABLE` with the count shown.
**Control.** Passes if *either* a price appears in visible text *or* a pricing/
quote route is linked — asking for a quote is a legitimate model.
**Fix.** Publish prices as text (not as an image) with `Offer`/`priceCurrency`
markup; where pricing is bespoke, publish a starting-from figure or a range with
the variables that move it. A range is far more useful than silence.

---

## Continuity

### EN-03 · Navigation changes between pages · impact 2 · confidence medium

**Mechanism.** Persistent navigation is what lets a visitor keep their bearings
after landing deep — which is how most assistant and search traffic arrives. When
the menu changes between pages the visitor loses the map they had just learned
and must re-orient on every step.
**Evidence.** Jaccard overlap of each page's `<nav>` link set against the largest
menu on the site; the least consistent page is named.
**Gate.** ≥3 pages exposing a `<nav>` region. Reported below **50 %** overlap.
**Control.** The threshold is deliberately loose: section-specific sub-navigation
is normal and should not trigger this. Confidence `medium`, because a site using
`<div class="nav">` has no detectable nav region at all.
**Fix.** One primary navigation component in the shared layout; section navigation
as a clearly secondary level; mark the current section active.

### EN-04 · Content pages dead-end · impact 2 · confidence high

**Mechanism.** A visitor who has just finished reading is at the point of highest
intent. A page that ends with nothing to do next converts that intent into a
back-button press. The same dead end caps crawl depth: pages reachable only from
a page with no in-content links are discovered late or not at all.
**Gate.** Pages with ≥100 words of main content and **zero** links in that main
content. Site-wide navigation does not count — that is exactly the difference
being measured.
**Fix.** Two to four contextual links inside the copy, with descriptive anchor
text, to what a reader of *this* page would want next.

### EN-05 · Sitemap pages linked from nowhere · impact 2 · confidence medium

**Mechanism.** A page nobody links to is a page nobody finds. Visitors cannot
navigate to it; a sitemap entry lets a crawler reach it, but it receives no
internal link signal, so it is treated as peripheral and rarely selected as a
source.
**Gate.** A sitemap inventory exists.
**False positive.** A page linked only from a page that was not crawled.
**Control.** Stated explicitly in `applicability`, and reported at `medium`
confidence for exactly this reason.
**Fix.** Link each orphan from the most relevant existing page or a genuine hub;
remove from the sitemap anything nobody should reach.

### EN-14 · No breadcrumb on deep pages · impact 1 · confidence medium

**Mechanism.** Most search and assistant traffic lands deep, not on the home
page. A breadcrumb is the cheapest possible answer to "where am I, and what else
is near this?" — without it the only options are the top-level menu or the back
button. `BreadcrumbList` markup additionally tells retrieval systems where the
page sits in the hierarchy.
**Gate.** ≥2 pages that are two or more path segments deep.
**Control.** Four detection routes — `BreadcrumbList` JSON-LD, `rel` attributes,
microdata types, or a `Home ›`-style trail in nav text — so a visually correct
breadcrumb without markup still passes.

---

## Friction

### EN-06 · Broken links in the visitor path · impact 3 (in navigation) / 2 · confidence high

From the visitor's side a broken link is a promise the site made and broke. A
dead link in the main navigation is worse than one in a footnote: it is on every
page and usually on the path to a high-intent destination. Shares
`dedupe_key: broken-internal-links` with `CR-25`, so the orchestrator merges the
crawl consequence and the visitor consequence into one finding.

### EN-07 · Error page with no route back · impact 2 · confidence high

**Mechanism.** Every stale external link, mistyped URL and outdated citation
lands here. A bare error page turns each arrival into a lost visitor; the same
page inside the normal layout recovers a large share of them.
**Evidence.** Internal link count, word count and search-box presence in the
response to a deterministic probe of a path that cannot exist.
**Gate.** The probe completed, and what came back is the *site's* error page.
Passes at ≥3 internal links, or at ≥1 link plus a search box.
**False positive.** Judging a bot wall's interstitial as the site's 404. A
challenge page answers every path identically, so the probe can come back
holding one — and no visitor following a stale link would ever see it.
**Control.** The probe records `blocked`. A refusal status (401/403/407/429/451)
or a challenge title makes this check `INCONCLUSIVE`, with the blocking itself
reported by `CR-02`. A search box is read from the form's action and inputs and
counts as a route back; the form is inspected, never submitted.
**Fix.** Render the 404 inside the normal layout, add a search box and links to
the main sections — **while keeping the HTTP status at 404** (see `CR-26`).

### EN-10 · Long pages with no scanning structure · impact 2 · confidence medium

Visitors scan before they read: they look for a subheading matching their
question and stop if they cannot find one. Subheadings are also the boundaries a
retrieval system uses to cut a page into passages, so an unstructured page is
both harder to skim and harder to quote from. Gated at **≥300 words**; reported
when there are no H2+ headings, when the words-per-subheading ratio exceeds 400,
or when ≥3 paragraphs exceed 120 words.

### EN-11 · No mobile viewport · impact 3 · confidence high

Without a viewport declaration a phone renders at ~980 px and scales down: body
text arrives a few pixels tall and every tap target is too small. On mobile
traffic — usually the majority — this is an immediate bounce whatever the content
is worth. Shares `dedupe_key: viewport-missing` with `CR-19`.

### EN-12 · Forms hard to complete · impact 2 · confidence medium

A form is where intent turns into a lead, and every unlabelled or unnecessary
field is a place to abandon. Unlabelled inputs are unusable with a screen reader
and lose their prompt as soon as typing begins, when the placeholder disappears.
Reported for unlabelled inputs, more than 10 fields, or no submit control.
**Control.** `<label>` elements are counted as well as `aria-label`,
`aria-labelledby` and `title`; a form with at least as many labels as inputs
passes. Hidden and submit inputs are excluded. **Forms are never submitted.**

### EN-13 · Images with no text alternative · impact 2 · confidence high

A missing `alt` leaves screen-reader users with a filename or nothing, so any
information the image carries is lost to them — and to every text-only consumer,
including crawlers. Where the image is a link, the missing alt removes the link's
only label.
**Control.** `alt=""` is a **correct** declaration that the image is decorative
and is never counted; only a missing attribute is. `aria-hidden` and
`role="presentation"` images are excluded. Shares
`dedupe_key: facts-in-images` with `AC-13`, so the merged finding carries both
the accessibility and the extraction consequence.
