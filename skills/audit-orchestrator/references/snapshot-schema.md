# Site snapshot schema

The contract between the orchestrator and every analyzer skill. Produced once
per audit by `../scripts/auditlib/crawler.py`; consumed by all four analyzers.

Why it exists: parsing HTML once and shipping the *structured* result means
analyzer skills never see raw markup, cannot drift apart on parsing details, and
stay cheap enough to run in a subprocess each. It also keeps the payload small
enough to hand to a language model.

Analyzers must treat the snapshot as **read-only** and must not perform network
access of their own.

```jsonc
{
  "schema_version": "1.0",
  "site": "example.com",            // registrable host, www-stripped
  "input": "example.com",           // exactly what the caller passed
  "target_url": "https://example.com/",
  "final_url": "https://www.example.com/",   // after redirects
  "origin": "https://www.example.com",
  "captured_at": "2026-09-04T14:32:00Z",

  "engine":  { "name": "...", "version": "...", "user_agent": "..." },
  "config":  { "max_pages": 20, "max_depth": 2, "max_link_probes": 30,
               "obey_robots": true, "request_timeout_s": 8.0,
               "rate_per_sec": 2.0, "host_concurrency": 2,
               "max_body_bytes": 2097152, "max_total_bytes": 20971520 },

  "robots": {
    "url": "...", "status": 200,
    "outcome": "ok | missing | server_error | unreachable | non_text",
    "sitemaps": ["..."],
    "groups": [{ "agents": ["*"], "crawl_delay": null,
                 "blocks_everything": false,
                 "rules": [{ "allow": false, "pattern": "/private/", "line": 3 }] }],
    "raw_excerpt": "first 1200 chars"
  },

  // Per-crawler verdict at the site root, with the reason attached.
  "robots_agent_matrix": {
    "gptbot": { "kind": "ai", "purpose": "OpenAI - ChatGPT training and browsing corpus",
                "allowed_at_root": false, "has_explicit_group": true,
                "group_blocks_everything": true }
  },

  "sitemaps": [{ "url": "...", "status": 200, "ok": true,
                 "type": "urlset | sitemapindex | malformed | unknown",
                 "url_count": 42, "lastmod_count": 40,
                 "newest_lastmod": "2026-08-01", "oldest_lastmod": "2024-01-04",
                 "referenced_in_robots": true }],
  "sitemap_urls": [{ "loc": "...", "lastmod": "..." }],   // capped at 500
  "sitemap_url_total": 42,

  "well_known": { "llms_txt": { "url": "...", "evaluated": true, "status": 404,
                                "ok": false, "looks_like_html": true,
                                "excerpt": "..." } },

  // One deterministic request for a path that cannot exist.
  "not_found_probe": { "url": ".../audit-probe-does-not-exist-9c1f",
                       "evaluated": true, "status": 404, "soft_404": false,
                       "redirected_to": "", "word_count": 58,
                       "internal_link_count": 7, "title": "Page not found",
                       // `blocked`: the response was a bot challenge or refusal,
                       // not the site's own 404, so EN-07 must not judge it.
                       // `has_search`: a search box is a recovery route even
                       // when the page carries few links. Inspected, never sent.
                       "blocked": false, "has_search": true },

  "pages": [ /* see below */ ],
  "link_checks": [{ "url": "...", "status": 404, "ok": false, "method": "HEAD",
                    "redirect_hops": 0, "final_url": "...",
                    "reference_count": 6,
                    "referenced_from": [{ "from": "...", "text": "Pricing",
                                          "region": "nav" }] }],

  "link_graph": { "internal_edges": [["from", "to"]],
                  "external_hosts": { "linkedin.com": 3 },
                  "orphan_sitemap_urls": ["..."] },

  "coverage": { "pages_attempted": 8, "pages_fetched": 7, "pages_with_html": 7,
                "pages_failed": 1, "pages_skipped_by_robots": 1,
                "pages_skipped_unsafe": 2, "pages_skipped_for_time": 0,
                "links_probed": 12, "sitemap_urls_seen": 42,
                "max_depth_reached": 2,
                "robots_blocked_sample": ["..."], "unsafe_skipped_sample": ["..."] },

  "http_stats": { "requests": 21, "bytes": 384122, "transport_errors": 0, "hosts": 1 },
  "notes": ["honouring site-declared Crawl-delay of 1.0s"],
  "errors": [{ "stage": "input", "message": "..." }]
}
```

## `pages[]`

```jsonc
{
  "url": "https://example.com/pricing",       // after redirects
  "requested_url": "https://example.com/prices",
  "depth": 1,
  "discovery": "seed | link | sitemap",
  "discovered_from": "https://example.com/",
  "anchor_text": "Pricing",
  "priority_score": 64,                        // see page-selection.md
  "status": 200, "ok": true,
  "content_type": "text/html", "bytes": 48211, "truncated": false,
  "elapsed_ms": 312,
  "redirects": [{ "url": "...", "status": 301, "location": "..." }],
  "headers": { "content-type": "...", "x-robots-tag": "...", "last-modified": "...",
               "content-encoding": "gzip", "cache-control": "...", "link": "..." },
  "error": "", "error_kind": "",               // timeout|dns|tls|connection|http|protocol
  "internal_links": [{ "url": "...", "text": "...", "region": "main|nav|other",
                       "rel": "" }],
  "external_links": [ /* same shape */ ],
  "extraction": { /* see below */ }             // absent when the page is not HTML
}
```

## `pages[].extraction`

Produced by `htmlparse.py` in one tolerant pass. Malformed markup degrades a
single field, never the audit; a parse failure is reported in `parse_error`.

| Field | Notes |
|---|---|
| `title`, `title_count` | `title` is `null` when the element is absent — distinct from `""`. |
| `html_lang` | `<html lang>`. |
| `meta_name`, `meta_property`, `meta_http_equiv`, `meta_duplicates` | First value wins; duplicates recorded separately. |
| `canonical`, `canonical_count`, `hreflang[]`, `links[]` | `<link rel=…>` descriptors. |
| `headings[]` | `{level, text, order, region}`. |
| `anchors[]` | `{href, text, text_source, rel, region, button_like, target}`. `text_source` is `text`/`aria`/`none`. |
| `images[]` | `{src, alt, title, width, height, loading, region, in_link, aria_hidden, role}`. **`alt` is `null` when the attribute is absent and `""` when it is explicitly empty** — the difference between a defect and a correct decorative declaration. |
| `scripts[]`, `script_count`, `inline_script_bytes`, `inline_style_bytes` | |
| `jsonld_blocks[]` | Raw strings, unparsed, so each analyzer can report its own parse errors. |
| `microdata_types[]`, `rdfa_types[]` | Guards against a false "no structured data" verdict. |
| `iframes[]`, `media_counts`, `tables`, `table_headers`, `table_captions`, `list_items` | `table_headers` is the `<th>` count: what binds a cell to its column, and the basis of `AC-19`. |
| `forms[]` | `{action, method, inputs[], labels, has_submit, region}`. Inspected only — never submitted. |
| `buttons[]` | `{text, region, kind}`. |
| `landmark_counts` | `main`, `nav`, `header`, `footer`, `article`, `aside`, `section`, `figure`, `address`, `time`, `table`, `form`. |
| `time_elements[]` | `{datetime, text, region}`. |
| `csr_markers[]`, `empty_root_containers[]` | Framework hydration markers and mount points with `inner_text_chars`. Never used alone — see `CR-11`. |
| `noscript_text_len` | |
| `text_excerpt`, `main_text_excerpt`, `nav_text_excerpt`, `first_screen_text` | Capped excerpts. |
| `word_count`, `main_word_count`, `paragraph_count`, `long_paragraphs` | `long_paragraphs` counts paragraphs over 120 words. |
| `has_skip_link`, `parse_error` | |

### Region semantics

`region` is `nav` inside `<nav>`, `<header>`, `<footer>` or `<aside>`; `main`
inside `<main>`/`<article>` (or everywhere, when the document declares no such
landmark); `other` otherwise. The landmark presence is decided by a pre-scan
*before* parsing, so text appearing before `<main>` is classified the same way
as text after it.

**Known limitation:** a site using `<div class="header">` instead of `<header>`
has no machine-detectable chrome, so its navigation text is counted as main
content. Checks that depend on the distinction (`AC-15`) report at `medium`
confidence for this reason.

## `transport` — did the origin answer at all?

Recorded once per crawl. Every check that could assert *absence* of something
gates on it, because absence can only be inferred from an answer.

```json
"transport": {
  "target_responded": false,
  "responses_received": 0,
  "transport_failures": { "dns": 6 },
  "failed_requests": 6,
  "dominant_failure": "dns",
  "state": "dns",
  "detail": "No HTTP response was received from the target origin: the hostname could not be resolved (DNS failure). ..."
}
```

| Field | Meaning |
|---|---|
| `target_responded` | At least one HTTP response of **any** status came back from the origin. When false, no finding about the site may be raised. |
| `responses_received` | How many requests produced an HTTP status. |
| `transport_failures` | Count per failure kind: `dns`, `connection_refused`, `network_unreachable`, `tls`, `timeout`, `protocol`, `connection`, `budget`. |
| `state` | `ok`, `partial`, `not_attempted`, or the dominant failure kind. |
| `detail` | Sentence used verbatim in the report's coverage limitation. |

Per-resource records carry the same distinction: `sitemaps[].responded`,
`not_found_probe.responded`, `well_known.*.responded` and `robots.error_kind`
each say whether that specific request reached the server. A `sitemaps[]` entry
with `"responded": false` cannot support a "no sitemap" finding; one with
`"status": 404` can.
