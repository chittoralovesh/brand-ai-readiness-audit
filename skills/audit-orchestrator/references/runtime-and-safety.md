# Runtime budget, network etiquette and the read-only guarantee

## 1. Read-only — structurally, not by convention

The guarantee is enforced in the transport layer, so no skill can bypass it even
by mistake.

`auditlib/fetcher.py`:

```python
if method not in ("GET", "HEAD"):
    raise ValueError("Fetcher is read-only: only GET and HEAD are permitted")
```

There is no code path in the marketplace that can emit `POST`, `PUT`, `PATCH` or
`DELETE`, send a request body, or attach credentials. The test suite asserts it
from the other side too: the fixture server fails the test if it ever receives a
non-GET/HEAD request.

Never performed, at any budget, on any site:

- submitting a form, logging in, creating an account, or sending a message;
- adding to a cart, checking out, or purchasing;
- changing any setting, uploading anything, or writing anything;
- bypassing `robots.txt`, brute-forcing paths, or probing for vulnerabilities;
- downloading media, archives or binaries.

### URLs that are never requested at all

Filtered before the frontier, by path or query, in `auditlib/urls.py`:

`/login` `/logout` `/signin` `/signout` `/register` `/signup` `/account`
`/admin` `/wp-admin` `/wp-login` `/my-account` `/cart` `/basket` `/checkout`
`/password` `/reset` `/unsubscribe` `/subscribe` `/delete` `/remove` `/edit`
`/preferences` `/oauth` `/sso` `/xmlrpc.php` `/cgi-bin/`; and any URL carrying
`add-to-cart`, `remove_item`, `logout`, `action`, `do`, `delete` or `confirm`.

Skipped URLs are counted and reported in `limitations`, so the gap is visible
rather than silent.

## 2. robots.txt

Obeyed without exception, for the auditor's own user agent, using an RFC 9309
parser (longest-match wins, `Allow` beats `Disallow` on ties, `*` and `$`
supported, per-agent groups preserved).

| robots.txt outcome | Behaviour |
|---|---|
| 2xx, parseable | Rules applied. `Crawl-delay` honoured. `Sitemap:` lines used. |
| 404 / 410 / 401 / 403 | No restrictions — a missing robots.txt means "crawl everything". Reported as `PASS`. |
| 5xx from the server | **Nothing is crawled.** Major crawlers treat a persistently failing robots.txt as disallow-all, so the audit does the same and reports it as a `critical`-eligible finding. |
| No HTTP response (DNS, refused, unroutable, TLS, timeout) | Nothing is crawled, and nothing is reported. The failure describes this machine's connectivity, not the site; `CR-03` becomes `NOT_EVALUATED` and a `transport` limitation is recorded. |
| Policy allows crawlers but the edge refuses them | Reported as `CR-31`. Measured with the auditor's own honest user agent - the audit never impersonates a named crawler to test a bot rule. |
| Served as HTML | Treated as unreadable; reported. |

A disallowed URL is never fetched and never worked around. The resulting gap
appears in `limitations` as a coverage statement.

The auditor identifies itself honestly, following the convention used by
mainstream crawlers:

```
Mozilla/5.0 (compatible; BrandAIReadinessAudit/1.0; +https://agentskills.io/) read-only-site-auditor
```

It is never varied to evade a block. If a site refuses this agent, that refusal
*is the finding* (`CR-02`) — the most important one an AI-discoverability audit
can return.

## 3. Network etiquette

| Limit | Value | Enforced in |
|---|---|---|
| Max HTML pages | 20 | `crawler.MAX_PAGES` |
| Max crawl depth | 2 | `crawler.MAX_DEPTH` |
| Max extra link probes | 30 | `crawler.MAX_LINK_PROBES` |
| Concurrent same-host sockets | 2 | `fetcher._HostLimiter` semaphore |
| Request rate | ≤2 /s /host, plus site `Crawl-delay` | `fetcher._HostLimiter` token bucket |
| Request timeout | 8 s, clamped by remaining budget | `fetcher.DEFAULT_TIMEOUT` |
| Retries | 1, transient failures only | `fetcher.fetch` |
| Redirect hops | 5 | `fetcher.MAX_REDIRECTS` |
| Body per response | 2 MB | `fetcher.MAX_BODY_BYTES` |
| Bytes per audit | ~20 MB | `fetcher.MAX_TOTAL_BYTES` |
| Sitemap files | 3 | `crawler.MAX_SITEMAP_FILES` |
| Sitemap URLs read | 2000 | `crawler.MAX_SITEMAP_URLS` |

Only HTML and small metadata resources are fetched. Images, video, archives,
fonts, stylesheets and scripts are recognised by extension and never downloaded.
Documents (`.pdf`, `.docx`, …) are *recorded* — publishing facts only as a PDF
is itself a finding — but not retrieved.

A typical audit makes roughly 25–50 requests to one host: fewer than a single
human browsing session with a warm cache.

## 4. Time budget

`--budget` sets a hard internal deadline (default **240 s**). The design target
is **under 180 s**; 240 s exists so there is always room to serialise a report
before Adobe's 300 s ceiling.

```
start    = monotonic()
deadline = start + budget
```

Every expensive operation consults the remaining budget. Reserves, from
`auditlib/budget.py`:

| Reserve | Value | Effect once remaining time drops below it |
|---|---|---|
| `RESERVE_FOR_OPTIONAL` | 75 s | Optional work (render comparison, off-site research) is skipped. |
| `RESERVE_FOR_ANALYSIS` + `RESERVE_FOR_REPORT` | 60 s | No new pages are discovered or fetched. |
| `RESERVE_FOR_REPORT` | 15 s | Analysis stops; the report is composed from evidence already collected. |

Degradation order, in full:

1. stop discovering new pages;
2. skip link probes;
3. skip optional render analysis;
4. skip optional off-site research;
5. finish analysis on the evidence already gathered;
6. **always** compose, validate and write the report;
7. record every gap in `limitations`.

Analyzer subprocesses each receive an explicit share of the remaining budget and
are killed if they exceed it; the timeout becomes a limitation, not a crash.

Nothing sleeps except the inter-request pacing that politeness requires. There
are no unbounded loops, no unbounded retries, no recursive crawling and no
unbounded model calls.

`audit_metrics` reports what actually happened: `duration_ms`, `phase_ms`,
`pages_attempted`, `pages_fetched`, `pages_skipped_by_robots`,
`pages_skipped_unsafe`, `pages_skipped_for_time`, `links_probed`,
`rendered_pages`, `http_requests`, `bytes_downloaded`, `checks_completed`,
`checks_skipped_for_time`.

## 5. JavaScript rendering

Baseline behaviour uses the **raw server HTML only**. No browser binary,
Playwright, Puppeteer or Chromium is bundled or required — they would add
hundreds of megabytes and seconds per page for a signal that the render-gap
check already detects from the response itself.

`CR-11` identifies pages whose content appears only after hydration, using two
independent signals plus a low text count. If the host environment happens to
offer a rendering tool, at most **4** suspicious high-value pages may be
rendered for comparison; without one, render comparison is `NOT_EVALUATED` and
no render result is ever invented.

## 6. Portability

Python 3.9+ standard library only: `urllib`, `html.parser`, `json`, `re`,
`xml.etree.ElementTree`, `concurrent.futures`, `dataclasses`, `pathlib`,
`datetime`, `gzip`, `zlib`, `ssl`, `socket`, `threading`, `subprocess`.

No third-party packages, no virtual environment, no Docker, no database, no
cache server, no model weights, no embeddings, and **no API key of any kind**.
Nothing is written outside the paths the caller names.
