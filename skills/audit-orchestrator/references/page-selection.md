# Deterministic page selection

The crawler must not simply take the first 20 links it finds. Link order is an
artefact of markup, so a first-N sample is effectively arbitrary: it varies with
an unrelated template change, and it systematically over-samples whatever the
header happens to list first.

Instead the frontier is a priority queue with an explicit, documented score.
Implemented in `crawler.score_candidate`.

## The score

```
score = 40
      + role_weight            # what job this page does on the site   (0..28)
      + 8   if the site's own navigation links to it
      - 9 * depth              # breadth before depth
      - 11 * trap_score        # unbounded spaces are cheap to enter, expensive to leave
      - 6   if the path is more than 4 segments deep
      - 25  if it is a document rather than a page
      - 3   if the link had no anchor text
clamped to 0..100; the audit's entry URL is always 100.
```

Ordering is `(-score, depth, url)`. The URL tiebreak makes the selection
**totally ordered**, so an unchanged site is sampled identically on every run.

## Role weights

Roles describe jobs a page can do on *any* website — not an industry, a CMS or a
vertical. They are matched against the URL path and, at a small discount,
against the anchor text that pointed at the page.

| Weight | Role | Why it is sampled first |
|---|---|---|
| 28 | about, company, who-we-are, team, mission | Carries the entity facts that decide identity resolution. |
| 26 | product, service, solution, platform, features, what-we-do | The offering itself — what the site would be cited *for*. |
| 24 | pricing, plans, rates, quote | The question users most often ask an assistant. |
| 22 | contact, support, help, FAQ | Trust, plus the extractable contact facts. |
| 20 | docs, guide, api, reference, tutorial | Highest fact density on technical sites. |
| 16 | blog, news, article, insights, press, case study | Where freshness and attribution live. |
| 15 | shop, catalogue, category, menu, courses, listings | Entry points to inventory. |
| 12 | locations, hours, booking, appointment | Local-entity facts. |
| 10 | careers, partners, testimonials, portfolio, projects | Secondary corroboration. |
| 6 | privacy, terms, legal, accessibility, imprint | Trust signals; low information density. |

A site whose vocabulary is not in this table is **not penalised**: every
candidate starts at 40 and is then ranked by structure alone — depth,
navigation membership and trap risk. The list biases *which* pages are sampled
first; it never affects whether a finding is raised, and no check consults it.

## Trap avoidance

`urls.trap_score` returns 0–3. Higher means "this URL leads into an unbounded
space", and each point costs 11 score:

- known infinite-space query keys (`replytocom`, `share`, `print`, `calendar`,
  `ical`, `orderby`, `filter`, `facet`, `sort_by`);
- three or more query parameters;
- a path six or more segments deep;
- pagination beyond page 2 (`/page/9`, `?page=9`);
- dated archive paths (`/2024/03/17/`).

Trap score only *de-prioritises*. It never raises a finding on its own —
`CR-28` reports parameter explosions from the observed link graph instead.

## Normalisation

Every candidate passes through `urls.normalize` first, so two URLs that differ
only cosmetically are one crawl target:

- scheme and host lowercased, default ports dropped, fragment removed;
- `index.html` / `default.aspx` / `home.php` collapsed to the directory;
- duplicate slashes collapsed, one trailing-slash convention applied;
- tracking parameters removed (`utm_*`, `gclid`, `fbclid`, `mc_*`, `_hs*`,
  `msclkid`, `srsltid`, and ~40 more);
- remaining query pairs sorted.

This is what makes finding IDs, de-duplication and page counts reproducible.

## Seeding and expansion

1. **Seed** — the entry URL at depth 0, plus every same-site HTML URL from the
   sitemap, scored and depth-assigned from its path.
2. **Wave per depth** — candidates at the current depth are sorted, de-duplicated
   and admitted up to the remaining page budget.
3. **Admission gate**, before anything is fetched: same registrable host;
   not a state-changing URL; probably HTML; allowed by `robots.txt`.
4. **Expand** — links found at this depth become candidates for the next, with a
   navigation bonus if the URL appears in any page's `<nav>`.

Subdomains are treated as different sites. Auditing `example.com` should not
silently pull in `blog.example.com` and change the scope the operator asked for.

## Link probing

Internal links that were *not* crawled are probed for liveness, up to 30,
ordered by **reference count descending**, then shortest path, then URL. A link
repeated across site-wide navigation costs far more when broken than a link on
one deep page, so it is verified first.

Probes use `HEAD`, then re-check with a ranged `GET` when the server answers
`405`/`501`/`400`/`403`/`429` or nothing — many servers are HEAD-hostile, and
this second request removes the most common source of false "broken link"
reports.
