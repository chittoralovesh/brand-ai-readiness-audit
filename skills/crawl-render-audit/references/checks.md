# crawl-render-audit — check reference

Every check answers the same seven questions, which is what makes it a check
rather than an opinion:

1. What mechanism makes this matter?
2. What exact observable evidence supports it?
3. Under what conditions is it applicable?
4. What could create a false positive?
5. How is that false positive prevented?
6. What is the impact rating, and why?
7. What concrete remediation fixes the mechanism?

`impact_base` is fixed per check; `breadth` and `confidence` are computed per
run. The orchestrator turns the three into a severity
(`../../audit-orchestrator/references/severity-model.md`).

---

## Group 1 — Was the crawler let in?

### CR-01 · Entry URL reachable · impact 4 · confidence high

**Mechanism.** A crawler that cannot retrieve the entry URL indexes nothing.
Every downstream signal is unreachable, so the brand cannot appear as a source
at all.
**Evidence.** The HTTP status of the audited URL, plus the transport error.
**Applicable.** Always.
**False positive.** A transient outage read as a permanent block.
**Control.** One retry on transient statuses and transport errors; the observed
status is quoted verbatim, so a reader can distinguish 403 from 503.
**Fix.** Return 200 to an unauthenticated, non-browser GET; allowlist declared
crawlers at the CDN/WAF; replace an error with a single 301 if the URL moved.

### CR-31 · Edge refuses crawlers that robots.txt invites · impact 4 · confidence high

**Mechanism.** `robots.txt` is a published policy. A CDN or WAF enforces a
separate rule set at the network layer, typically by pattern-matching the
User-Agent string, and ships an "AI bots" or "known bots" category that is often
on by default. When the two disagree the site invites crawlers on paper and
turns them away in practice. Because nothing in the policy file is wrong, the
gap is invisible to any audit that only reads `robots.txt` - yet it stops
citations as completely as a site-wide `Disallow` would.
**Evidence.** How many catalogued crawlers `robots.txt` allows at the root, the
named agents, and the HTTP status a self-identifying automated request received.
**Gate.** `robots.txt` parsed cleanly **and** it allows at least half the
catalogued crawlers at the root. Where the policy is itself restrictive, a block
*agrees* with it and this check reports `NOT_APPLICABLE` - the blocking is then
`CR-02`/`CR-04`/`CR-05`'s business, not a contradiction.
**Honesty.** Measured with the auditor's own user agent. The check never claims
to be GPTBot or any other named crawler; the finding rests on "a client that
openly identified itself as automated was refused", which is the same
contradiction established without impersonating anyone. A test asserts no
spoofed crawler identity appears anywhere in the shipped source.
**False positive.** A silent edge-level drop and a broken local network look
identical from one end.
**Control.** With no HTTP response at all the check reports `INCONCLUSIVE`, says
both explanations remain open, and points at the `transport` limitation. Only a
real refusal status (401/403/407/429/451) produces a finding. A 5xx is a broken
server, not a bot rule, and is left to `CR-01`.
**Fix.** Find the rule in the CDN/WAF bot-management dashboard, then make the two
layers agree - allow the agents through the edge, or disallow them in
`robots.txt` so the published policy is honest. Keep rate limiting; it is the
blanket identity-based block, not throttling, that removes a site from answers.

### CR-02 · Bot wall or anti-bot interstitial · impact 4 (3 if <50 % of pages) · confidence high

**Mechanism.** Answer engines fetch with declared, non-browser clients that do
not solve JavaScript challenges or CAPTCHAs. When the edge returns a challenge,
the assistant stores *the challenge page* — so the brand is omitted, or described
from third-party sources the brand does not control.
**Evidence.** Count of fetched URLs returning 401/403/429/451, plus pages whose
title or opening text matches a challenge-page marker; ratio over all fetched
URLs.
**Applicable.** Any page response was captured.
**False positive.** A legitimate 403 on one restricted path read as a site-wide
wall.
**Control.** Ratio reported and used for impact; status codes *and* text markers
are both accepted as evidence, and the affected URLs are listed.
**Fix.** Allow verified search and AI crawler agents through the bot ruleset
(verify by reverse DNS or published IP ranges, not the UA string); keep rate
limiting but return 200 with content.

### CR-03 · robots.txt readable · impact 4 (5xx) / 2 (served as HTML) · confidence high/medium

**Mechanism.** Major crawlers treat a persistently failing `robots.txt` (5xx or
unreachable) as **disallow everything**, not allow everything. A broken robots
endpoint can silently de-index an entire healthy site.
**Evidence.** URL, status, and the classified outcome.
**Applicable.** Always.
**False positive.** Treating a 404 as a defect.
**Control.** A missing `robots.txt` means "crawl everything" and is reported
`PASS` with an explicit note.
**Fix.** Serve `/robots.txt` as `text/plain` with 200 or 404 — never 5xx; handle
it at the web server rather than the application router.

### CR-04 · AI and answer-engine crawlers disallowed · impact 4/3/2 · confidence high

**Mechanism.** Assistants only quote pages their fetcher may retrieve. A
root-level `Disallow` for these agents means the brand's own wording never
reaches an answer; the assistant omits the brand or describes it from pages the
brand does not control. Blocking a *live fetcher* additionally breaks look-ups a
user explicitly asked for by pasting the URL.
**Evidence.** Which of 22 catalogued AI agents are disallowed at root, whether
by an explicit group or the wildcard group, and each agent's purpose.
**Applicable.** A parseable `robots.txt` exists.
**False positive.** Reporting an agent that simply has no group as "blocked".
**Control.** Only an explicit root-level `Disallow` counts; group provenance is
reported. Breadth is `sites 1/1` — "2 of 22 catalogued agents" would understate a
deliberate block of the two that matter. Extent is carried by impact: 4 for a
wildcard block or ≥6 agents, 3 for a live fetcher or ≥2 agents, else 2.
**Fix.** Separate training crawlers from live retrieval fetchers and decide each
deliberately; scope any genuine exclusion to paths rather than `/`.

### CR-05 · Search crawlers disallowed · impact 4 · confidence high

**Mechanism.** Assistants that cannot fetch a page directly fall back on a
search index. Excluding the search crawlers removes the brand from that fallback
too, leaving no path by which an answer could cite the site.
**Control.** Same provenance rules as CR-04.
**Fix.** Replace `Disallow: /` with specific paths; keep private areas behind
authentication rather than robots.txt.

### CR-06 · Linked pages disallowed · impact 3 · confidence high

**Mechanism.** A page linked in navigation but disallowed cannot be read by any
crawler: anchor context points at a document no machine can open.
**Evidence.** Disallowed URLs that appear as targets in the observed link graph.
**Applicable.** At least one URL was withheld by robots.txt.
**False positive.** Reporting normal housekeeping exclusions.
**Control.** Only exclusions covering URLs the site **itself links to** are
reported; unlinked exclusions are reported `PASS`.
**Fix.** Reconcile navigation with robots.txt — allow the path or stop linking
it publicly.

### CR-08 / CR-09 / CR-10 · `noindex`, page-level `nofollow`, `noai`

| Check | impact | Mechanism | Control |
|---|---|---|---|
| `CR-08` `noindex` | 4 site-wide, else 3 | Removes the page from search indexes, so assistants grounding in an index never see it. Frequently left over from staging and invisible in a browser. | Both `<meta name=robots>` and the `X-Robots-Tag` **header** are read — the header wins and is the one usually missed. |
| `CR-09` page `nofollow` | 2 | Tells crawlers to ignore every outgoing link, so pages reachable only through this one are never discovered. | Only reported when it affects at least half the sample; a single page is normal. |
| `CR-10` `noai` | 2 | An emerging voluntary opt-out. Legitimate as policy; a defect when inherited from a template and contradicting a goal of being cited. | Reported explicitly as a *policy inconsistency*, at medium confidence. |

---

## Group 2 — Could it read the page?

### CR-11 · Content only exists after JavaScript · impact 4 (≥60 % of pages) / 3 · confidence high

**Mechanism.** Most AI fetchers and many crawlers read the raw HTTP response and
do not execute JavaScript. If the text appears only after hydration, those
clients store an empty page: the brand looks contentless to machines while
looking complete to people. This is the single most common reason a visually
rich site is never quoted.
**Evidence.** Per page: server-HTML word count, plus the specific signals — an
empty framework mount point, a hydration payload, a script-to-text imbalance, a
`<noscript>` fallback carrying the message.
**Applicable.** At least one HTML page parsed.
**False positive.** Server-rendered React/Next/Nuxt sites, which are extremely
common, being called broken because a framework marker was present.
**Control.** **Two independent signals are required, plus fewer than 150 words.**
A framework marker alone is never sufficient. Median word count is reported so a
reader can sanity-check the verdict.
**Impact rationale.** Tagged `render-gap-root-cause`: it mechanically causes
missing titles, headings, structured data and text, so at ≥50 % breadth the
orchestrator folds those symptoms into it instead of listing them separately.
**Fix.** Server-side render or statically generate the highest-value templates
so the H1, body copy, links and structured data are in the raw response; verify
with `curl -s <url> | wc -w`; keep client rendering for genuinely interactive
widgets.

### CR-12 · Very little extractable text · impact 3 (≥50 %) / 2 · confidence medium

**Mechanism.** Retrieval systems rank and quote *passages*. A page with almost
no prose offers nothing to quote, so a perfectly crawlable page is passed over
for a competitor that states the same fact in sentences.
**Evidence.** Per-page main-content word count against a 120-word threshold.
**Control.** Confidence is `medium`: a legitimately short page (a contact card,
a landing page) exists. Tagged `render-gap-symptom`.
**Fix.** 100–200 words per thin template stating what the page is, who it is for
and one or two concrete facts, in the server response, not behind a tab.

### CR-13 / CR-14 · Titles · impact 3 (missing) / 2 (duplicated) · confidence high

Missing: the title is the primary label a retrieval system uses and the string
displayed when citing. Duplicated: identical titles make separate pages look
like duplicates, and near-duplicates are collapsed to one representative — the
others stop being citable at all. Evidence names the reused string and its
count. Missing titles are tagged `render-gap-symptom`.

### CR-15 · Meta descriptions · impact 1 · confidence high

Search and assistant surfaces fall back to an arbitrary text fragment without
one, which often lands on navigation or legal boilerplate and misrepresents the
page. Low impact because the page is still indexable and quotable.

### CR-33 · Head metadata declared more than once · impact 3 (`robots`) / 2 · confidence high

**Mechanism.** HTML allows one `<title>` and one meaningful value per metadata
name. When a theme, an SEO plugin and a CMS field each emit their own, the
winner is chosen by whichever parser reads the page - and parsers differ. The
title an assistant cites, or the description it displays, is then not the one
the author wrote. A repeated `robots` directive is the dangerous case:
conflicting values are typically resolved to the most restrictive, so a stray
`noindex` from one layer silently removes the page from every index that reads
it, while the page looks perfectly normal in a browser.
**Evidence.** Per page, the count of `<title>` elements and which decisive
`<meta name>` values appear more than once.
**Applicable.** At least one HTML page parsed, and the origin responded.
**False positive.** Flagging harmless repetition - a second `viewport` or a
duplicated `keywords` changes nothing about what is indexed.
**Control.** Only `robots` and `description` count as decisive; every other
repeated name is ignored. Impact rises to 3 only when `robots` is among them.
**Fix.** Leave exactly one component responsible for each head element, disable
the duplicate in whichever layer is not authoritative, and assert the count in
CI by grepping the raw HTML of each template.

### CR-16 · Heading hierarchy · impact 2 · confidence high

Passage-level retrieval segments a document by headings and labels each passage
with the nearest one. Without a clear H1 and an ordered hierarchy, passages are
extracted under the wrong label — a correct answer on the page is attributed to
the wrong topic, or missed. Reported for no H1 or more than two H1s; a skipped
level is counted and mentioned but does not on its own raise the finding.
Tagged `render-gap-symptom`.

### CR-17 · `<html lang>` · impact 1 · CR-18 · Semantic landmarks · impact 1

`lang` drives locale eligibility and screen-reader pronunciation; short or
mixed-language pages are guessed wrong without it. Landmarks let extractors
separate the article from navigation and promos — without them boilerplate is
mixed into the extracted passage, producing citations that quote a menu.
CR-18 only counts pages with ≥120 words, so a stub is not reported.

### CR-19 · Mobile viewport · impact 3 (all pages) / 2 · confidence high

Without a viewport declaration a phone renders at ~980 px and scales down: text
arrives unreadable, tap targets too close. On mobile traffic this is an immediate
bounce whatever the content is worth. Shares `dedupe_key: viewport-missing` with
`EN-11`, so the merged finding states both the rendering and the engagement
consequence.

---

## Group 3 — Will the signals survive?

### CR-20 – CR-23 · Canonical URLs

| Check | impact | Mechanism | Control |
|---|---|---|---|
| `CR-20` missing | 2 (all) / 1 (some) | Every URL variant — parameters, trailing slash, http/https, www — becomes a separate candidate document; signals split and an arbitrary variant is cited. | Reported separately for "none at all" and "some templates". |
| `CR-21` collapsed onto another page | 3 | A cross-page canonical is an instruction to drop this URL. Applied to distinct content it **deletes those pages from the index**. | Only reported when ≥2 distinct pages point at one target, so a legitimate duplicate pair is not flagged. |
| `CR-22` cross-domain | 3 | Hands indexing credit to another site; assistants then cite that domain instead. | Host comparison is www-insensitive. |
| `CR-23` multiple | 2 | Conflicting declarations are treated as unreliable and usually ignored, so duplicate handling silently stops working. | Counted from the parsed `<link>` elements. |

### CR-24 · Redirect chains · impact 2 · confidence high

Each hop costs a request; some fetchers cap redirect depth or drop the request.
Chains also dilute the signal attached to the original URL. Only chains of **two
or more** hops are reported — one redirect is normal and correct.

### CR-32 · Meta-refresh used as a redirect · impact 3 · confidence high

**Mechanism.** `<meta http-equiv="refresh" content="0; url=...">` is a redirect
written in the document instead of the HTTP response. Browsers honour it, but
many crawlers and most AI fetchers do not - they keep the document they were
served. That document is the waypoint, which is usually near-empty, so the
destination is never stored and the URL that gets cited resolves to nothing
worth quoting. A 301 or 302 carries the same intent in a form every client
already honours.
**Evidence.** The redirecting URL, its target, the delay, and how many words the
waypoint carries of its own.
**Applicable.** At least one HTML page parsed, and the origin responded.
**False positive.** Treating a self-refresh as a redirect. `content="300"` with
no URL reloads the same page - a live-scores or dashboard widget - and hides
nothing.
**Control.** A `url=` target is required before anything is reported. The word
count of the waypoint is quoted so a reader can see how much content is at risk.
**Fix.** Return a real 301/302 with the destination in the `Location` header and
delete the meta tag; where the waypoint exists for consent or language
selection, serve the content at the URL and layer the interstitial over it.

### CR-34 · Localised pages without hreflang · impact 2 (absent) / 2 (invalid) / 1 (no self-reference) · confidence medium/high

**Mechanism.** Localised variants say much the same thing in different
languages, so to a machine they are near-duplicates competing for one meaning.
`hreflang` is what says "these are the same document for different audiences,
pick by locale". Without it the system keeps one variant and suppresses the
rest, so a reader can be handed the wrong language and the authority earned by
the set is split across its members rather than pooled. This is also the signal
that lets a locale-aware or location-personalised answer resolve to the right
page rather than an arbitrary one.
**Evidence.** The localisation signals observed (distinct locale path roots,
distinct `<html lang>` values), and the declared alternates with their codes.
**Gate.** Reported only where the sample shows the site *is* localised: at least
two distinct language roots in the URL space, or two distinct `<html lang>`
values, or existing `hreflang` markup. A monolingual site reports
`NOT_APPLICABLE` - it does not need alternates and is never asked for them.
**False positive.** Reading a two-letter path segment as a language. `/us/`,
`/go/` and `/pr/` are not languages.
**Control.** A segment counts only if it is a real ISO 639-1 code, and two
distinct roots are required before localisation is inferred. Confidence is
`medium` for the absent case because the evidence is a bounded sample.
**Validity.** Declared codes are checked as BCP 47: an ISO 639-1 language, an
optional four-letter script, and an optional region that is either ISO 3166-1
alpha-2 or a three-digit UN M.49 area. `en-uk` is the classic failure - `uk` is
reserved but never assigned, and the United Kingdom is `gb`. One invalid member
commonly invalidates the whole cluster, so the site pays for the markup and gets
none of the consolidation.
**Self-reference.** A page's set is read as the complete membership list for the
cluster. A page that omits itself is claiming a group it does not belong to, and
consumers commonly reject the set rather than guess. Reported at impact 1 when
it affects more than half the declaring pages.
**Fix.** Render one reciprocal alternate list per cluster on every member,
including a self-reference and an `x-default`, with absolute URLs that match the
canonical exactly.

### CR-25 · Broken internal links · impact 3 (if a target is referenced ≥3 times) / 2 · confidence high

A broken link is a dead end for both audiences: a crawler discovers nothing
beyond it, a visitor lands on an error. Links repeated in site-wide navigation
multiply the cost across every page.
**Control.** Probes use HEAD then re-check with a ranged GET on 405/501/400/403/
429 or no response, because many servers are HEAD-hostile — this removes the most
common false "broken link". Shares `dedupe_key: broken-internal-links` with
`EN-06`.

### CR-26 · Soft 404 · impact 2 (200 for a missing page) / 1 (redirect) · confidence high/medium

When every URL returns 200, crawlers cannot tell real pages from mistakes:
typos, stale links and generated variants all become indexable near-duplicates,
diluting real pages and wasting crawl budget. Evidence comes from one
deterministic probe of a path that cannot exist. A 404 or 410 passes; anything
else unexpected is `INCONCLUSIVE` rather than a finding.

### CR-27 · Sitemap · impact 2 (absent or broken) / 1 (undeclared) · confidence high

Without a sitemap, discovery depends entirely on following links from pages
already known, so new, deep and weakly-linked pages are found late or never, and
there is no per-page freshness signal. Three distinct verdicts, each with its own
`dedupe_key`: absent, declared-but-broken, present-but-undeclared.

### CR-28 · Parameter crawl traps · impact 2 · confidence medium

Every parameter combination is a distinct URL. A handful of filters multiplies
into thousands of near-identical pages, consuming crawl budget that should reach
real content. Reported only at **≥8 permutations of the same path with the same
parameter set**, observed in the actual link graph — not inferred from the
presence of filters.

### CR-29 · Response weight and compression · impact 2 · CR-30 · Plain HTTP · impact 3

Document size is on the critical path to first render. Reported at >100 KB
uncompressed or >900 KB of HTML. Plain HTTP suppresses conversion via the
browser warning and creates a duplicate host; **localhost and private/reserved
addresses are excluded**, so a staging or test host is never flagged.
