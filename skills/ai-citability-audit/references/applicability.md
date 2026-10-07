# Applicability gates — page-role inference

The rule this skill is built around:

> **Never assume every site needs every schema type.**
> A portfolio site does not need `Product`. A documentation site does not need
> `LocalBusiness`. A SaaS site does not need physical-store information.

Demanding markup a site has no reason to publish is the easiest way to fill a
report with confident nonsense, and it is precisely the "laundry list" failure
mode. So every schema expectation is gated on evidence that **the entity exists
on the page**, drawn from the page's own content — never from the industry the
domain looks like it belongs to.

Implemented in `infer_roles()` in `../scripts/check_citability.py`.

---

## How a gate is used

1. Infer the roles each sampled page plays, from its own content.
2. For a given expectation, the denominator is **only** the pages that matched
   its gate.
3. If no page matched, report `NOT_APPLICABLE` and quote the gate. This is a
   deliberate, visible outcome: it shows a reader the expectation was considered
   and correctly dropped, rather than silently omitted.
4. If pages matched, report the ratio with the gate restated in `applicability`.

The consequence is that the *same* skill produces different, correct
expectations for an e-commerce site, a hospital, a SaaS product, a newspaper and
a personal portfolio, without any per-vertical configuration.

---

## The gates

### `product` → `Product` / `Offer` expected

Matched when **either**:

- a price pattern **and** a purchase-intent phrase (`add to cart`, `add to bag`,
  `buy now`, `in stock`, `out of stock`, `sku`, `order now`, `checkout`,
  `pre-order`) appear on the page; **or**
- the URL path contains a product segment (`/product`, `/products`, `/p/`,
  `/item`, `/shop`, `/store`, `/collections`, `/dp/`) **and** a price pattern
  appears.

*Why both halves.* A price alone appears on pricing pages, case studies and
blog posts. A product-shaped URL alone appears on category listings. Requiring a
price plus either purchase controls or a product URL keeps category pages and
editorial mentions out of the denominator.

Currency detection covers symbols (`$ € £ ¥ ₹ ₽ ₩ ¢`) and ISO codes, in both
prefix and suffix positions, so it is not English- or dollar-specific.

### `offer` → pricing page

`/pricing`, `/plans`, `/packages`, `/tariff` in the path **and** a price pattern.
Currently used for role reporting rather than a separate expectation.

### `article` → `Article` / `BlogPosting` / `NewsArticle` expected

Matched when the page has **≥250 words of main content** and **either**:

- an editorial path segment (`/blog`, `/news`, `/article`, `/post`, `/insights`,
  `/stories`, `/press`, `/newsroom`, `/research`, `/resources`, `/updates`,
  `/journal`, `/magazine`); **or**
- a date in the first ~1200 characters (or a `<time>` element) **and** a byline
  pattern (`by …`, `written by …`, `author …`, `posted by …`, `reviewed by …`).

*Why the word floor.* Category and tag listings sit under the same paths and are
not articles. 250 words separates a post from an index in practice, without
needing to recognise a CMS.

The same gate drives `AC-17` (authorship), so the two checks always agree about
what counts as editorial.

### `faq` → `FAQPage` **recommended, never required**

Matched when the page has ≥3 headings ending in `?`, or ≥2 under a
`/faq`, `/help`, `/support` or `/questions` path.

This role deliberately produces a **proactive recommendation** (`PR-02`), not a
finding. Rich-result support for `FAQPage` has been narrowed by search engines,
so its absence is not a defect — but question-shaped content remains the highest
-yield format for passage retrieval, which makes it a genuine opportunity.

### `local_business` → `LocalBusiness` / `PostalAddress` expected

Matched when a street-address pattern **and** a postcode pattern both appear,
**and** either a phone number or an opening-hours phrase (`opening hours`,
`hours`, `visit us`, `directions`, `find us`, `our store/shop/office/clinic/
showroom`) is present.

*Why so strict.* A postcode alone appears in a footer for a registered office
that customers never visit; a phone number alone appears everywhere. Requiring a
street pattern, a postcode and a visit-intent signal keeps remote-first companies
and pure-online services out of the denominator.

Postcode matching covers US ZIP/ZIP+4, UK, four-digit-plus-letters (NL) and
six-digit (IN) forms; street matching covers English, German (`strasse`,
`straße`), French (`rue`), Italian (`via`) and Spanish (`calle`) thoroughfare
words. It is not exhaustive, and a miss produces `NOT_APPLICABLE` — the safe
direction.

### `contact` → contact facts expected in structured form

A phone number, an email address, or a `/contact` path segment (including
`/kontakt`, `/contacto`). Drives `PR-05` (structured `contactPoint`) rather than
a finding.

### `howto` → informational only

≥3 step markers plus a `/how-to`, `/guide` or `/tutorial` path. Recorded but not
currently the subject of an expectation, because `HowTo` rich results have been
deprecated and the markup no longer carries the benefit it once did.

---

## What is deliberately *not* inferred

| Not inferred | Why |
|---|---|
| Industry or vertical | Nothing in this skill branches on "is this healthcare / e-commerce / SaaS". A vertical classifier would be exactly the overfitting the brief warns against, and it would be wrong on the sites that matter most — the unusual ones. |
| CMS or framework | Framework markers are used *only* as one of two required render-gap signals, never to decide what a site should publish. |
| Company size, seniority or budget | Remediation effort is rated from the change itself, not from assumptions about the team. |
| Language of the content | Gates use symbol, numeric and structural patterns wherever possible. Where an English keyword list is unavoidable, a miss yields `NOT_APPLICABLE` rather than a false finding. |

## The safe direction

Every gate is designed so that **failing to match produces `NOT_APPLICABLE`, not
a finding**. A missed role costs one true positive; a wrongly-matched role costs
a false positive, which is far more expensive — it damages trust in the whole
report and is what the rubric penalises most directly.
