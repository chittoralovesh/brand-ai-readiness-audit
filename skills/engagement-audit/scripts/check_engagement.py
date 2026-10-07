#!/usr/bin/env python3
"""Will a visitor who lands here orient, understand and act - or leave?

Reads a site snapshot produced by `audit-orchestrator` and writes a check-result
document to stdout (or `--out`). Standard library only; no network access, and
nothing here submits a form, authenticates or changes site state: every signal
is read from the already-captured HTML.

    python check_engagement.py --snapshot snapshot.json --out engagement.json

The checks follow the order a real visit fails in:

  1. orientation - can they tell what this is and where they are?
  2. next action - is there an obvious, labelled thing to do?
  3. continuity  - does the site keep them moving without dead ends?
  4. friction    - scanability, mobile, forms, error recovery.

Discoverability defects that also cost visitors (broken links, missing viewport,
images with no alt text) are reported here with the *same* `dedupe_key` the
discoverability skills use, so the orchestrator merges them into one finding
carrying both consequences rather than reporting them twice.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

PRODUCER = "engagement-audit"
SCHEMA_VERSION = "1.0"

PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_EVALUATED = "NOT_EVALUATED"
INCONCLUSIVE = "INCONCLUSIVE"

# Text that identifies a bot challenge or access denial rather than a real page.
# Duplicated from crawl-render-audit so this skill folder runs standalone.
CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "attention required! | cloudflare", "enable javascript and cookies to continue",
    "access denied", "request blocked", "are you a robot", "captcha",
    "px-captcha", "incapsula incident", "bot detection", "unusual traffic",
    "verify you are human", "ddos protection by",
)

AI_DISCOVERABILITY = "ai_discoverability"
ENGAGEMENT = "on_site_engagement"

GENERIC_LINK_TEXT = frozenset({
    "click here", "here", "read more", "learn more", "more", "more info",
    "more information", "this", "this page", "link", "continue", "go", "details",
    "see more", "view more", "find out more", "discover more", "explore",
    "submit", "click", "start", "next", "previous", "info", "open", "view", "...",
})
CTA_TOKENS = (
    "start", "get started", "sign up", "book", "buy", "order", "request",
    "contact", "demo", "quote", "apply", "join", "subscribe", "try", "schedule",
    "call", "shop", "donate", "register", "enroll", "enrol", "reserve",
    "download", "talk to", "get a", "free trial", "add to", "checkout",
    "consult", "enquire", "inquire", "appointment", "estimate", "see pricing",
    "browse", "read the docs", "documentation", "watch", "find", "search",
    "compare", "hire", "install", "get in touch", "learn how",
)
_PRICE_RE = re.compile(
    r"(?:[$€£¥₹₽₩¢]|\b(?:usd|eur|gbp|inr|aud|cad|chf|jpy)\b)\s?\d[\d,.\s]*"
    r"|\b\d[\d,.]*\s?(?:usd|eur|gbp|inr|dollars?|euros?|pounds?)\b", re.I)
_COMMERCE_RE = re.compile(
    r"\b(pricing|price|plans|subscription|buy|purchase|order|shop|store|cart|"
    r"quote|book (?:a|now)|hire|rates|fees|packages|tariff|free trial|"
    r"get started|sales)\b", re.I)


class Results:
    def __init__(self, producer: str):
        self.producer = producer
        self.checks: list = []
        self.notes: list = []
        self.errors: list = []

    def add(self, check_id, title, *, pillar, category, status,
            evidence="", mechanism="", impact_base=0, confidence="high",
            affected=0, applicable=0, unit="pages", evidence_items=None,
            affected_urls=None, remediation=None, dedupe_key="", tags=None,
            applicability="", supersedes=None):
        self.checks.append({
            "check_id": check_id, "title": title, "pillar": pillar,
            "category": category, "status": status, "impact_base": int(impact_base),
            "confidence": confidence,
            "breadth": {"affected": int(affected), "applicable": int(applicable),
                        "unit": unit},
            "evidence": evidence, "evidence_items": (evidence_items or [])[:12],
            "mechanism": mechanism,
            "affected_urls": sorted(set(affected_urls or []))[:25],
            "remediation": remediation or {}, "dedupe_key": dedupe_key or check_id,
            "tags": sorted(set(tags or [])), "applicability": applicability,
            "supersedes": sorted(set(supersedes or [])),
        })

    def note(self, message):
        if message not in self.notes:
            self.notes.append(message)

    def to_dict(self):
        return {"schema_version": SCHEMA_VERSION, "producer": self.producer,
                "produced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "checks": sorted(self.checks, key=lambda c: c["check_id"]),
                "notes": self.notes, "errors": self.errors}


def run_checks(results, snapshot, ctx, checks):
    for func in checks:
        try:
            func(snapshot, ctx, results)
        except Exception as exc:                       # pragma: no cover
            results.errors.append({"check": getattr(func, "__name__", "?"),
                                   "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})


def load_snapshot(path):
    if path in ("-", "", None):
        return json.loads(sys.stdin.read())
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------

def ex(page):
    return page.get("extraction") or {}


def short(url, origin):
    if origin and url.startswith(origin):
        return url[len(origin):] or "/"
    return url


def is_generic(text):
    t = (text or "").strip().lower().strip(" .,:;!?-–—>»→")
    return (not t) or t in GENERIC_LINK_TEXT or len(t) <= 2


def looks_like_cta(text):
    t = (text or "").strip().lower()
    return bool(t) and len(t) <= 60 and any(tok in t for tok in CTA_TOKENS)


def as_dict(value):
    """Coerce a snapshot field to a dict. A malformed snapshot must degrade a
    field, never stop the skill producing output."""
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def html_pages_of(snapshot):
    """Successfully-parsed HTML pages, tolerant of a malformed snapshot."""
    pages = [p for p in as_list(snapshot.get("pages")) if isinstance(p, dict)
             and isinstance(p.get("url"), str)]
    html = [p for p in pages if p.get("ok") and isinstance(p.get("extraction"), dict)
            and not p["extraction"].get("non_html")]
    return pages, html


def build_context(snapshot):
    pages, html_pages = html_pages_of(snapshot)
    for p in html_pages:
        p["_nav_targets"] = {l["url"] for l in p.get("internal_links", [])
                             if l.get("region") == "nav"}
        p["_main_links"] = [l for l in p.get("internal_links", [])
                            if l.get("region") == "main"]
    return {"pages": pages, "html_pages": html_pages,
            "entry": next((p for p in html_pages if p.get("depth") == 0), None),
            "origin": snapshot.get("origin") or "", "site": snapshot.get("site") or ""}


def guard(ctx, r, check_id, title, category="engagement"):
    if ctx["html_pages"]:
        return True
    r.add(check_id, title, pillar=ENGAGEMENT, category=category, status=NOT_EVALUATED,
          applicability="No HTML page was successfully retrieved, so the visitor experience "
                        "could not be assessed. The crawl-access findings explain why.")
    return False


# ---------------------------------------------------------------------------
# 1. Orientation
# ---------------------------------------------------------------------------

def check_primary_action(snap, ctx, r):
    if not guard(ctx, r, "EN-01", "Primary next action", "orientation"):
        return
    entry = ctx["entry"]
    if entry is None or ex(entry).get("main_word_count", 0) < 30:
        r.add("EN-01", "Primary next action", pillar=ENGAGEMENT, category="orientation",
              status=NOT_EVALUATED,
              applicability="The entry page returned too little content to assess whether a "
                            "next action is offered; see the render and thin-content findings.")
        return
    e = ex(entry)
    candidates = []
    for anchor in e.get("anchors") or []:
        if anchor.get("region") != "main":
            continue
        if anchor.get("button_like") or looks_like_cta(anchor.get("text")):
            candidates.append(anchor.get("text") or "(unlabelled)")
    for button in e.get("buttons") or []:
        if button.get("region") == "main" and button.get("text"):
            candidates.append(button["text"])
    if candidates:
        r.add("EN-01", "The entry page offers a clear next action", pillar=ENGAGEMENT,
              category="orientation", status=PASS,
              evidence="%d action control(s) in the entry page's main content (e.g. %s)."
                       % (len(candidates), ", ".join(repr(c[:30]) for c in candidates[:3])),
              applicable=1, unit="pages")
        return
    main_buttons = [b for b in (e.get("buttons") or []) if b.get("region") == "main"]
    r.add("EN-01", "The entry page offers no obvious next action", pillar=ENGAGEMENT,
          category="orientation", status=FAIL, impact_base=3, confidence="medium",
          affected=1, applicable=1, unit="pages",
          evidence="The main content of %s contains no button and no link whose label reads "
                   "as an action: %d main-content link(s) and %d main-content button(s) were "
                   "examined (%d further button(s) sit in navigation or chrome, where they "
                   "serve a different purpose)."
                   % (short(entry["url"], ctx["origin"]),
                      len([a for a in (e.get("anchors") or []) if a.get("region") == "main"]),
                      len(main_buttons),
                      len(e.get("buttons") or []) - len(main_buttons)),
          mechanism="A visitor arriving from a search result or an assistant's citation has no "
                    "prior context. If the landing page does not name one obvious next step, "
                    "the decision cost falls on them and the common resolution is to leave. "
                    "This is distinct from having no navigation: a menu asks the visitor to "
                    "choose, a primary action tells them what to do.",
          evidence_items=[{"url": entry["url"],
                           "observed": "no action-labelled link or button in main content",
                           "expected": "one prominent, specifically-labelled primary action",
                           "excerpt": (e.get("first_screen_text") or "")[:140]}],
          affected_urls=[entry["url"]],
          remediation={"summary": "Add one primary action to the first screen, labelled with the "
                                  "outcome rather than the mechanism.",
                       "steps": ["Choose the single action worth the most to a first-time visitor (start a trial, request a quote, browse the catalogue, read the getting-started guide).",
                                 "Label it with the outcome: 'Get a quote for your fleet' beats 'Submit'; 'Read the setup guide' beats 'Learn more'.",
                                 "Place it in the server-rendered HTML within the first screen, and repeat it once at the end of the page.",
                                 "Offer at most one secondary action so the primary one stays unambiguous."],
                       "expected_benefit": "Converts arriving traffic instead of returning it to "
                                           "the results page.",
                       "effort": "low"},
          dedupe_key="no-primary-cta", tags=["semantic-review"])


def check_link_labels(snap, ctx, r):
    if not guard(ctx, r, "EN-02", "Link and action labelling", "orientation"):
        return
    pages = ctx["html_pages"]
    total, generic, unlabelled = 0, [], []
    for p in pages:
        for anchor in ex(p).get("anchors") or []:
            if anchor.get("region") == "nav" and not anchor.get("text"):
                pass
            total += 1
            text = anchor.get("text") or ""
            if not text.strip():
                unlabelled.append((p, anchor))
            elif is_generic(text):
                generic.append((p, anchor))
    if total == 0:
        r.add("EN-02", "Link and action labelling", pillar=ENGAGEMENT,
              category="orientation", status=NOT_APPLICABLE,
              applicability="No links were found in the sampled pages.")
        return
    bad = len(generic) + len(unlabelled)
    ratio = bad / total
    if ratio < 0.12:
        r.add("EN-02", "Links are labelled descriptively", pillar=ENGAGEMENT,
              category="orientation", status=PASS,
              evidence="%d of %d links across %d pages use a generic or empty label (%.0f%%)."
                       % (bad, total, len(pages), 100 * ratio),
              applicable=total, unit="links")
        return
    samples = [(p, a) for p, a in (generic + unlabelled)][:8]
    r.add("EN-02", "Many links are labelled generically or not at all", pillar=ENGAGEMENT,
          category="orientation", status=FAIL,
          impact_base=2, confidence="high", affected=bad, applicable=total, unit="links",
          evidence="%d of %d links across %d sampled pages carry a non-descriptive label "
                   "(%d generic such as 'read more'/'click here', %d with no text at all) - "
                   "%.0f%% of all links."
                   % (bad, total, len(pages), len(generic), len(unlabelled), 100 * ratio),
          mechanism="Link text is the only preview a visitor gets before committing a click, "
                    "and it is also the anchor text a crawler uses to understand the "
                    "destination. 'Read more' predicts nothing for either audience: the "
                    "visitor hesitates or clicks blindly, and the destination page loses the "
                    "topical signal its inbound links should have carried. Screen-reader users "
                    "navigating by link list get an unusable list of identical entries.",
          evidence_items=[{"url": p["url"],
                           "observed": "link to %s labelled %r"
                                       % (a.get("href", "")[:60], (a.get("text") or "")[:40]),
                           "expected": "a label naming the destination"} for p, a in samples],
          affected_urls=[p["url"] for p, _ in samples],
          remediation={"summary": "Rewrite link text to name the destination.",
                       "steps": ["Replace 'read more'/'learn more' with the destination's subject: 'Read the 2026 pricing guide'.",
                                 "For icon-only links, add an `aria-label` describing the action.",
                                 "For card layouts, wrap the heading in the link rather than adding a separate 'more' link below it."],
                       "expected_benefit": "Higher click confidence for visitors and stronger "
                                           "topical signals for the destination pages.",
                       "effort": "low"},
          dedupe_key="generic-link-text")


def check_navigation_consistency(snap, ctx, r):
    if not guard(ctx, r, "EN-03", "Navigation consistency", "continuity"):
        return
    pages = [p for p in ctx["html_pages"] if p["_nav_targets"]]
    if len(pages) < 3:
        r.add("EN-03", "Navigation consistency", pillar=ENGAGEMENT, category="continuity",
              status=NOT_APPLICABLE,
              applicability="Fewer than three pages exposed a `<nav>` region, so navigation "
                            "cannot be compared across the site. %d of %d sampled pages had "
                            "a nav region." % (len(pages), len(ctx["html_pages"])))
        return
    # The comparison target is the *persistent* menu - the nav links that appear
    # on most pages - not the largest single nav region. Documentation sites put
    # an entire page tree inside <nav>; comparing against that made every page
    # look inconsistent with a sidebar it was never supposed to repeat.
    frequency = {}
    for p in pages:
        for url in p["_nav_targets"]:
            frequency[url] = frequency.get(url, 0) + 1
    threshold = max(2, int(0.6 * len(pages)))
    base = {url for url, count in frequency.items() if count >= threshold}
    if len(base) < 2:
        r.add("EN-03", "Navigation consistency", pillar=ENGAGEMENT, category="continuity",
              status=INCONCLUSIVE,
              applicability="No set of navigation links recurs on at least %d of the %d "
                            "sampled pages, so there is no persistent menu to compare "
                            "against. This is common where navigation is rendered "
                            "client-side or differs by section." % (threshold, len(pages)))
        return
    similarities = [(p, len(base & p["_nav_targets"]) / len(base)) for p in pages]
    inconsistent = [(p, s) for p, s in similarities if s < 0.5]
    if not inconsistent:
        r.add("EN-03", "Navigation is consistent across pages", pillar=ENGAGEMENT,
              category="continuity", status=PASS,
              evidence="All %d pages with a nav region carry at least half of the %d-link "
                       "persistent menu." % (len(pages), len(base)),
              applicable=len(pages))
        return
    r.add("EN-03", "Navigation changes substantially between pages", pillar=ENGAGEMENT,
          category="continuity", status=FAIL, impact_base=2, confidence="medium",
          affected=len(inconsistent), applicable=len(pages),
          evidence="%d of %d pages with a nav region carry under half of the site's "
                   "%d-link persistent menu; the least consistent is %s at %.0f%% coverage."
                   % (len(inconsistent), len(pages), len(base),
                      short(inconsistent[0][0]["url"], ctx["origin"]),
                      100 * min(s for _, s in inconsistent)),
          mechanism="Persistent navigation is what lets a visitor keep their bearings after "
                    "landing deep in a site - which is how most assistant and search traffic "
                    "arrives. When the menu changes between pages, the visitor loses the map "
                    "they had just learned and has to re-orient on every step, which is a "
                    "reliable predictor of abandonment.",
          evidence_items=[{"url": p["url"],
                           "observed": "%d nav links, %.0f%% of the persistent menu present"
                                       % (len(p["_nav_targets"]), 100 * s),
                           "expected": "the same primary navigation as the rest of the site"}
                          for p, s in inconsistent[:6]],
          affected_urls=[p["url"] for p, _ in inconsistent],
          remediation={"summary": "Render one primary navigation component on every template.",
                       "steps": ["Move the primary menu into the shared layout so every template inherits it.",
                                 "Keep section-specific navigation as a clearly secondary level rather than replacing the primary menu.",
                                 "Mark the current section as active so the visitor can see where they are."],
                       "expected_benefit": "Visitors landing on any page keep a stable route to "
                                           "the rest of the site.",
                       "effort": "medium"},
          dedupe_key="nav-inconsistent")


def check_dead_ends(snap, ctx, r):
    if not guard(ctx, r, "EN-04", "Onward journeys", "continuity"):
        return
    pages = [p for p in ctx["html_pages"] if ex(p).get("main_word_count", 0) >= 100]
    if not pages:
        r.add("EN-04", "Onward journeys", pillar=ENGAGEMENT, category="continuity",
              status=NOT_APPLICABLE,
              applicability="No sampled page carried enough content for an onward journey to "
                            "be expected.")
        return
    dead = [p for p in pages if len(p["_main_links"]) == 0]
    if not dead:
        r.add("EN-04", "Content pages offer onward links", pillar=ENGAGEMENT,
              category="continuity", status=PASS,
              evidence="All %d content pages contain at least one in-content link onward."
                       % len(pages), applicable=len(pages))
        return
    r.add("EN-04", "Content pages are dead ends", pillar=ENGAGEMENT, category="continuity",
          status=FAIL, impact_base=2, confidence="high", affected=len(dead), applicable=len(pages),
          evidence="%d of %d content pages contain no link in their main content - the only "
                   "way onward is the site-wide menu (%s)."
                   % (len(dead), len(pages),
                      ", ".join(short(p["url"], ctx["origin"]) for p in dead[:4])),
          mechanism="A visitor who has just finished reading is at the point of highest "
                    "intent. A page that ends with nothing to do next converts that intent "
                    "into a back-button press. The same dead end also caps crawl depth: pages "
                    "reachable only from a page with no in-content links are discovered late "
                    "or not at all.",
          evidence_items=[{"url": p["url"],
                           "observed": "%d words of content, 0 in-content links"
                                       % ex(p).get("main_word_count", 0),
                           "expected": "2-4 contextual links to the natural next step"}
                          for p in dead[:6]],
          affected_urls=[p["url"] for p in dead],
          remediation={"summary": "End every content page with contextual next steps.",
                       "steps": ["Add 2-4 in-content links to the pages a reader of *this* page would want next - related topics, the relevant product, the contact or booking route.",
                                 "Link with descriptive anchor text so the link also carries a topical signal.",
                                 "Prefer contextual links inside the copy over a generic 'related posts' block."],
                       "expected_benefit": "Deeper sessions and better crawl reach at the same time.",
                       "effort": "low"},
          dedupe_key="dead-end-pages")


def check_orphan_pages(snap, ctx, r):
    orphans = as_list(as_dict(snap.get("link_graph")).get("orphan_sitemap_urls"))
    total = snap.get("sitemap_url_total", 0) or 0
    if not total:
        r.add("EN-05", "Pages reachable from navigation", pillar=ENGAGEMENT,
              category="continuity", status=NOT_EVALUATED,
              applicability="No sitemap inventory was available, so pages that exist but are "
                            "not linked cannot be identified.")
        return
    # On a large site, almost every sitemap URL is unreachable from a 20-page
    # sample - not because it is orphaned, but because the sample is small.
    # Reporting that would be an artefact of the crawl budget, not a defect.
    crawled = len(ctx["html_pages"])
    if total > max(10, crawled * 2):
        r.add("EN-05", "Pages reachable from navigation", pillar=ENGAGEMENT,
              category="continuity", status=NOT_APPLICABLE,
              applicability="The sitemap lists %d URLs but only %d page(s) were crawled. "
                            "Orphan detection needs a sample comparable to the site's size; "
                            "with this ratio, unreached URLs would reflect the crawl budget "
                            "rather than the site's internal linking, so no conclusion is "
                            "drawn." % (total, crawled))
        return
    if not orphans:
        r.add("EN-05", "Sitemap pages are reachable by following links", pillar=ENGAGEMENT,
              category="continuity", status=PASS,
              evidence="Every sitemap URL seen in the crawl sample was also reachable through "
                       "an internal link (%d sitemap URLs known)." % total,
              applicable=total, unit="sitemap URLs")
        return
    r.add("EN-05", "Pages exist in the sitemap but are not linked from any crawled page",
          pillar=ENGAGEMENT, category="continuity", status=FAIL,
          impact_base=2, confidence="medium", affected=len(orphans), applicable=total,
          unit="sitemap URLs",
          evidence="%d of %d sitemap URLs were not reachable by following links from the %d "
                   "pages crawled (e.g. %s)."
                   % (len(orphans), total, len(ctx["html_pages"]),
                      ", ".join(short(u, ctx["origin"]) for u in orphans[:4])),
          mechanism="A page nobody links to is a page nobody finds. Visitors cannot navigate "
                    "to it at all, and although a sitemap entry lets a crawler reach it, the "
                    "page receives no internal link signal, so it is treated as peripheral and "
                    "rarely selected as a source.",
          evidence_items=[{"url": u, "observed": "in sitemap, not linked from any crawled page",
                           "expected": "linked from at least one relevant page"}
                          for u in orphans[:8]],
          affected_urls=orphans,
          remediation={"summary": "Link the orphaned pages from a relevant hub, or retire them.",
                       "steps": ["Group the orphans by topic and link each from the most relevant existing page or a section index.",
                                 "Add the important ones to navigation; add the rest to a genuine hub page rather than a link dump.",
                                 "Remove URLs from the sitemap that you do not intend anyone to reach."],
                       "expected_benefit": "Turns invisible pages into pages that can be found "
                                           "and cited.",
                       "effort": "low"},
          dedupe_key="orphan-pages",
          applicability="Bounded by the crawl sample: a page linked only from a page that was "
                        "not crawled would appear here. Reported at medium confidence for that "
                        "reason.")


def check_broken_links_visitor_impact(snap, ctx, r):
    checks = as_list(snap.get("link_checks"))
    page_failures = [p for p in ctx["pages"]
                     if p.get("status") and 400 <= p["status"] < 600
                     and p.get("discovery") == "link"]
    broken = [l for l in checks if l.get("status") and 400 <= l["status"] < 600]
    total = len(checks) + len([p for p in ctx["pages"] if p.get("discovery") == "link"])
    if total == 0:
        r.add("EN-06", "Broken links in the visitor path", pillar=ENGAGEMENT,
              category="friction", status=NOT_EVALUATED,
              applicability="No internal links were available to probe.")
        return
    nav_broken = [l for l in broken
                  if any(ref.get("region") == "nav" for ref in l.get("referenced_from", []))]
    all_broken = broken + [{"url": p["url"], "status": p["status"], "reference_count": 1,
                            "referenced_from": []} for p in page_failures]
    if not all_broken:
        r.add("EN-06", "No broken links in the visitor path", pillar=ENGAGEMENT,
              category="friction", status=PASS,
              evidence="%d of %d probed internal links resolve." % (total, total),
              applicable=total, unit="links")
        return
    r.add("EN-06", "Visitors following site links reach error pages", pillar=ENGAGEMENT,
          category="friction", status=FAIL,
          impact_base=3 if nav_broken else 2, confidence="high",
          affected=len(all_broken), applicable=total, unit="links",
          evidence="%d of %d probed internal links return an error%s."
                   % (len(all_broken), total,
                      "; %d of them appear in site-wide navigation" % len(nav_broken)
                      if nav_broken else ""),
          mechanism="From the visitor's side a broken link is a promise the site made and "
                    "broke. A dead link in the main navigation is worse than a dead link in a "
                    "footnote because it is present on every page and is usually on the path "
                    "to a high-intent destination.",
          evidence_items=[{"url": l["url"],
                           "observed": "HTTP %s, referenced from %d page(s)%s"
                                       % (l.get("status"), l.get("reference_count", 1),
                                          " including site navigation"
                                          if any(ref.get("region") == "nav"
                                                 for ref in l.get("referenced_from", [])) else ""),
                           "expected": "a working destination"} for l in all_broken[:8]],
          affected_urls=[l["url"] for l in all_broken],
          remediation={"summary": "Repair navigation links first, then in-content links.",
                       "steps": ["Fix or remove every broken link that appears in site-wide navigation - these cost the most and are cheapest to find.",
                                 "Redirect moved pages once to their new URL.",
                                 "Add a link check to the publish pipeline so new breakage is caught before release."],
                       "expected_benefit": "Removes visible failures on the highest-traffic paths.",
                       "effort": "low"},
          dedupe_key="broken-internal-links")


def check_error_page_recovery(snap, ctx, r):
    probe = as_dict(snap.get("not_found_probe"))
    if not probe.get("evaluated") or probe.get("status") is None:
        r.add("EN-07", "Error-page recovery", pillar=ENGAGEMENT, category="friction",
              status=NOT_EVALUATED,
              applicability="The missing-page probe did not complete, so error-page recovery "
                            "could not be assessed.")
        return
    # A bot wall answers every path the same way, so the "error page" that came
    # back may be a challenge interstitial no human visitor would ever see.
    # Judging the site's error handling from it would describe the WAF instead.
    title = (probe.get("title") or "").lower()
    if probe.get("blocked") or probe.get("status") in (401, 403, 407, 429, 451) or any(
            marker in title for marker in CHALLENGE_MARKERS):
        r.add("EN-07", "Error-page recovery", pillar=ENGAGEMENT, category="friction",
              status=INCONCLUSIVE,
              applicability="The missing-page probe returned HTTP %s, and the response "
                            "looks like a bot challenge or access denial rather than the "
                            "site's own error page. A human visitor following a stale link "
                            "would not see this page, so nothing can be concluded about "
                            "error-page recovery from it. The blocking itself is reported "
                            "by CR-02." % probe.get("status"))
        return

    links = probe.get("internal_link_count", 0)
    words = probe.get("word_count", 0)
    search = bool(probe.get("has_search"))
    # A search box is a route back on its own, so an error page offering one
    # plus a link home is not a dead end even though it carries few links.
    if links >= 3 or (search and links >= 1):
        r.add("EN-07", "The error page offers a route back", pillar=ENGAGEMENT,
              category="friction", status=PASS,
              evidence="The 404 response contains %d internal link(s), %d words%s."
                       % (links, words, " and a search box" if search else ""),
              applicable=1, unit="pages")
        return
    r.add("EN-07", "The error page leaves visitors with no way back", pillar=ENGAGEMENT,
          category="friction", status=FAIL, impact_base=2, confidence="high",
          affected=1, applicable=1, unit="pages",
          evidence="A request for a non-existent path returned a page with %d internal link(s) "
                   "and %d words of text." % (links, words),
          mechanism="Every stale external link, mistyped URL and outdated citation lands here. "
                    "A bare error page turns each of those arrivals into a lost visitor, when "
                    "the same page with navigation and a search box would recover a large "
                    "share of them.",
          evidence_items=[{"url": probe.get("url"),
                           "observed": "%d internal links, %d words" % (links, words),
                           "expected": "site navigation, a search box and links to the main sections",
                           "excerpt": (probe.get("title") or "")[:80]}],
          remediation={"summary": "Give the 404 page the site's navigation and a search entry point.",
                       "steps": ["Render the 404 inside the normal site layout so the header, menu and footer are present.",
                                 "Add a search box and links to the two or three most-visited sections.",
                                 "Keep the HTTP status at 404 while doing so."],
                       "expected_benefit": "Recovers visitors arriving on stale links instead of "
                                           "losing them.",
                       "effort": "low"},
          dedupe_key="error-page-recovery")


# ---------------------------------------------------------------------------
# 3. Discoverability of the pages a visitor needs
# ---------------------------------------------------------------------------

_ROUTE_PATTERNS = {
    "contact or support": (
        r"(contact|support|help|customer[- ]service|get[- ]in[- ]touch|reach[- ]us|kontakt)",
        r"\b(contact|support|help|get in touch|talk to us|customer service|"
        r"email us|call us)\b"),
    "about or company information": (
        r"(about|company|who-we-are|our-story|team|mission|impressum)",
        r"\b(about us|about|our story|who we are|our team|our mission|company)\b"),
    "privacy or terms": (
        r"(privacy|terms|legal|cookie|gdpr|imprint|impressum|conditions)",
        r"\b(privacy|terms|cookie policy|legal|imprint)\b"),
}


def _route_present(pages, url_pattern, text_pattern):
    for p in pages:
        for link in p.get("internal_links", []):
            if re.search(url_pattern, link["url"], re.I) or \
                    re.search(text_pattern, link.get("text") or "", re.I):
                return link
        if re.search(url_pattern, p["url"], re.I):
            return {"url": p["url"], "text": ex(p).get("title") or ""}
    return None


def check_essential_routes(snap, ctx, r):
    if not guard(ctx, r, "EN-08", "Essential page discoverability", "orientation"):
        return
    pages = ctx["html_pages"]
    missing = []
    for label, (url_pattern, text_pattern) in _ROUTE_PATTERNS.items():
        if _route_present(pages, url_pattern, text_pattern) is None:
            missing.append(label)
    if not missing:
        r.add("EN-08", "Essential pages are reachable from the crawled sample",
              pillar=ENGAGEMENT, category="orientation", status=PASS,
              evidence="Links to contact/support, about and privacy/terms were all found "
                       "across %d sampled pages." % len(pages),
              applicable=len(_ROUTE_PATTERNS), unit="routes")
        return
    r.add("EN-08", "Pages visitors look for to decide whether to trust the site are not linked",
          pillar=ENGAGEMENT, category="orientation", status=FAIL,
          impact_base=3 if "contact or support" in missing else 2, confidence="medium",
          affected=len(missing), applicable=len(_ROUTE_PATTERNS), unit="routes",
          evidence="No link to %s was found across %d sampled pages (%d links examined)."
                   % (", ".join(missing), len(pages),
                      sum(len(p.get("internal_links", [])) for p in pages)),
          mechanism="Contact details, an about page and a privacy policy are the checks a "
                    "cautious visitor performs before spending money or sharing data. Their "
                    "absence reads as evasiveness. The same pages are also where the "
                    "organisation states the entity facts - who it is, where it is - that "
                    "assistants need in order to describe it correctly.",
          evidence_items=[{"url": ctx["entry"]["url"] if ctx["entry"] else ctx["origin"],
                           "observed": "no link matching '%s' in the crawl sample" % label,
                           "expected": "a persistent link, usually in the footer"}
                          for label in missing],
          remediation={"summary": "Add persistent footer links to the missing pages and write "
                                  "them properly.",
                       "steps": ["Add contact, about and privacy/terms links to the site-wide footer.",
                                 "On the contact page, publish the details as selectable text (address, phone, email) rather than only a form - a form gives a machine nothing to extract and gives the visitor no alternative if it fails.",
                                 "On the about page, state who the organisation is, where it operates and since when."],
                       "expected_benefit": "Removes a trust barrier for visitors and supplies "
                                           "the entity facts assistants need.",
                       "effort": "low"},
          dedupe_key="essential-routes-missing",
          applicability="Bounded by the crawl sample: a page linked only from a page that was "
                        "not crawled would not be seen. Reported at medium confidence.")


def check_pricing_discoverability(snap, ctx, r):
    if not guard(ctx, r, "EN-09", "Pricing discoverability", "orientation"):
        return
    pages = ctx["html_pages"]
    corpus = " ".join((ex(p).get("text_excerpt") or "") for p in pages)
    commercial_hits = len(_COMMERCE_RE.findall(corpus))
    if commercial_hits < 3:
        r.add("EN-09", "Pricing discoverability", pillar=ENGAGEMENT, category="orientation",
              status=NOT_APPLICABLE,
              applicability="The site shows little commercial-transaction language (%d signal(s) "
                            "across %d pages), so a published price is not expected. This check "
                            "does not apply to informational, documentation or portfolio sites."
                            % (commercial_hits, len(pages)))
        return
    has_price = bool(_PRICE_RE.search(corpus))
    pricing_route = _route_present(pages, r"(pricing|prices|plans|packages|rates|tariff|quote|estimate)",
                                   r"\b(pricing|prices|plans|packages|rates|get a quote|request a quote)\b")
    if has_price or pricing_route:
        r.add("EN-09", "The site addresses cost", pillar=ENGAGEMENT, category="orientation",
              status=PASS,
              evidence="%s%s across %d sampled pages."
                       % ("A price is stated in visible text" if has_price else "No price in text",
                          "; a pricing or quote route is linked" if pricing_route else "",
                          len(pages)), applicable=1, unit="sites")
        return
    r.add("EN-09", "A commercial site publishes neither prices nor a route to a quote",
          pillar=ENGAGEMENT, category="orientation", status=FAIL,
          impact_base=2, confidence="medium", affected=1, applicable=1, unit="sites",
          evidence="The sampled pages contain %d commercial-intent phrase(s) but no price "
                   "figure in visible text and no link to a pricing, plans or quote page."
                   % commercial_hits,
          mechanism="Cost is the question most commercial visitors want answered first, and "
                    "'price' questions are among the most common asked of assistants. A site "
                    "with no price and no stated route to one cannot be used to answer them, "
                    "so the assistant answers from a competitor or a third-party listing - and "
                    "the visitor who was ready to buy leaves to find the number elsewhere.",
          evidence_items=[{"url": ctx["entry"]["url"] if ctx["entry"] else ctx["origin"],
                           "observed": "no price figure and no pricing/quote link in the sample",
                           "expected": "published prices, a price range, or an explicit route "
                                       "to a quote"}],
          remediation={"summary": "Publish prices, a defensible range, or an explicit quote route.",
                       "steps": ["If prices are fixed, publish them as text (not as an image) and mark them up with `Offer`/`priceCurrency`.",
                                 "If they are bespoke, publish a starting-from figure or a range with the variables that move it - this is far more useful than silence.",
                                 "Either way, link a clearly-labelled pricing or quote page from the primary navigation."],
                       "expected_benefit": "Answers the question that decides whether a visit "
                                           "continues, and makes the brand quotable for price "
                                           "questions.",
                       "effort": "medium"},
          dedupe_key="pricing-not-discoverable",
          applicability="Raised only where the site's own language shows commercial intent "
                        "(%d matching phrases)." % commercial_hits)


# ---------------------------------------------------------------------------
# 4. Friction
# ---------------------------------------------------------------------------

def check_scanability(snap, ctx, r):
    if not guard(ctx, r, "EN-10", "Content scanability", "friction"):
        return
    pages = [p for p in ctx["html_pages"] if ex(p).get("main_word_count", 0) >= 300]
    if not pages:
        r.add("EN-10", "Content scanability", pillar=ENGAGEMENT, category="friction",
              status=NOT_APPLICABLE,
              applicability="No sampled page carried 300+ words, so scanability aids are not "
                            "yet necessary.")
        return
    problems = []
    for p in pages:
        e = ex(p)
        words = e.get("main_word_count", 0)
        subheads = len([h for h in (e.get("headings") or []) if h["level"] >= 2])
        long_paras = e.get("long_paragraphs", 0)
        reasons = []
        if subheads == 0 or words / max(1, subheads) > 400:
            reasons.append("%d words with %d subheading(s)" % (words, subheads))
        if long_paras >= 3:
            reasons.append("%d paragraphs over 120 words" % long_paras)
        if reasons:
            problems.append((p, "; ".join(reasons)))
    if not problems:
        r.add("EN-10", "Long pages are broken up for scanning", pillar=ENGAGEMENT,
              category="friction", status=PASS,
              evidence="All %d long-form pages use subheadings at a readable density."
                       % len(pages), applicable=len(pages))
        return
    r.add("EN-10", "Long pages are walls of text", pillar=ENGAGEMENT, category="friction",
          status=FAIL, impact_base=2, confidence="medium",
          affected=len(problems), applicable=len(pages),
          evidence="%d of %d long-form pages lack scanning structure (e.g. %s: %s)."
                   % (len(problems), len(pages), short(problems[0][0]["url"], ctx["origin"]),
                      problems[0][1]),
          mechanism="Visitors scan before they read; they look for a subheading that matches "
                    "their question and stop if they cannot find one. Subheadings are also the "
                    "boundaries a retrieval system uses to cut a page into passages, so an "
                    "unstructured page is both harder to skim and harder to quote a relevant "
                    "extract from.",
          evidence_items=[{"url": p["url"], "observed": reason,
                           "expected": "a subheading roughly every 200-300 words and "
                                       "paragraphs under about 100 words"}
                          for p, reason in problems[:6]],
          affected_urls=[p["url"] for p, _ in problems],
          remediation={"summary": "Add descriptive subheadings and split the longest paragraphs.",
                       "steps": ["Insert an H2/H3 every 200-300 words, phrased as the question that section answers.",
                                 "Split paragraphs over about 100 words; use lists for anything enumerable.",
                                 "Put the answer in the first sentence of each section rather than building up to it."],
                       "expected_benefit": "Better skim-reading for visitors and cleaner passage "
                                           "extraction for retrieval.",
                       "effort": "low"},
          dedupe_key="scanability")


def check_mobile_viewport(snap, ctx, r):
    if not guard(ctx, r, "EN-11", "Mobile readability", "friction"):
        return
    pages = ctx["html_pages"]
    missing = [p for p in pages if not (ex(p).get("meta_name", {}) or {}).get("viewport")]
    if not missing:
        r.add("EN-11", "Pages are configured for mobile viewports", pillar=ENGAGEMENT,
              category="friction", status=PASS,
              evidence="%d of %d sampled pages declare a viewport." % (len(pages), len(pages)),
              applicable=len(pages))
        return
    r.add("EN-11", "Pages will render at desktop width on phones", pillar=ENGAGEMENT,
          category="friction", status=FAIL, impact_base=3, confidence="high",
          affected=len(missing), applicable=len(pages),
          evidence="%d of %d sampled pages declare no mobile viewport."
                   % (len(missing), len(pages)),
          mechanism="Without a viewport declaration a phone renders the page at roughly 980px "
                    "and scales it down, so body text arrives at a few pixels tall and every "
                    "tap target is too small. On mobile traffic - usually the majority - this "
                    "produces an immediate bounce whatever the content is worth.",
          evidence_items=[{"url": p["url"], "observed": "no viewport meta tag",
                           "expected": "width=device-width, initial-scale=1"} for p in missing[:6]],
          affected_urls=[p["url"] for p in missing],
          remediation={"summary": "Add the viewport meta tag, then verify no horizontal overflow.",
                       "steps": ['Add `<meta name="viewport" content="width=device-width, initial-scale=1">` to the base template.',
                                 "Check each template at 375px wide for horizontal scrolling caused by fixed widths."],
                       "expected_benefit": "Legible pages on the devices most visitors use.",
                       "effort": "low"},
          dedupe_key="viewport-missing")


def check_form_usability(snap, ctx, r):
    if not guard(ctx, r, "EN-12", "Form usability", "friction"):
        return
    forms = []
    for p in ctx["html_pages"]:
        for form in ex(p).get("forms") or []:
            inputs = [i for i in form.get("inputs", [])
                      if i.get("type") not in ("hidden", "submit", "button", "image")]
            if inputs:
                forms.append((p, form, inputs))
    if not forms:
        r.add("EN-12", "Form usability", pillar=ENGAGEMENT, category="friction",
              status=NOT_APPLICABLE,
              applicability="No form with visible inputs was found in the sample. (Forms are "
                            "inspected only; this audit never submits one.)")
        return
    problems = []
    for p, form, inputs in forms:
        unlabelled = [i for i in inputs if not i.get("labelled")]
        if form.get("labels", 0) >= len(inputs):
            unlabelled = []          # enough <label> elements to cover the inputs
        reasons = []
        if unlabelled:
            reasons.append("%d of %d inputs have no label, aria-label or title"
                           % (len(unlabelled), len(inputs)))
        if len(inputs) > 10:
            reasons.append("%d fields in one form" % len(inputs))
        if not form.get("has_submit"):
            reasons.append("no submit control in the markup")
        if reasons:
            problems.append((p, form, "; ".join(reasons)))
    if not problems:
        r.add("EN-12", "Forms are labelled and reasonably sized", pillar=ENGAGEMENT,
              category="friction", status=PASS,
              evidence="All %d form(s) with visible inputs are labelled and have a submit "
                       "control." % len(forms), applicable=len(forms), unit="forms")
        return
    r.add("EN-12", "Forms are hard to complete", pillar=ENGAGEMENT, category="friction",
          status=FAIL, impact_base=2, confidence="medium",
          affected=len(problems), applicable=len(forms), unit="forms",
          evidence="%d of %d forms have usability defects (e.g. on %s: %s)."
                   % (len(problems), len(forms), short(problems[0][0]["url"], ctx["origin"]),
                      problems[0][2]),
          mechanism="A form is where intent turns into a lead, and every unlabelled or "
                    "unnecessary field is a place to abandon. Unlabelled inputs are also "
                    "unusable with a screen reader and lose their prompt as soon as the "
                    "visitor starts typing, when the placeholder disappears.",
          evidence_items=[{"url": p["url"],
                           "observed": "%s (action=%s)" % (reason, form.get("action") or "same page"),
                           "expected": "a visible label per field and only necessary fields"}
                          for p, form, reason in problems[:6]],
          affected_urls=[p["url"] for p, _, _ in problems],
          remediation={"summary": "Label every field visibly and cut the form to what you will "
                                  "act on.",
                       "steps": ["Pair each input with a `<label for=...>`; keep the placeholder as an example, not as the label.",
                                 "Remove fields that are not used in the first response to the enquiry.",
                                 "Mark required fields explicitly and validate inline rather than only on submit."],
                       "expected_benefit": "More completed enquiries from the same traffic.",
                       "effort": "low"},
          dedupe_key="form-usability")


def check_image_alt_accessibility(snap, ctx, r):
    if not guard(ctx, r, "EN-13", "Image text alternatives", "friction"):
        return
    total, missing_pages, missing_count = 0, [], 0
    for p in ctx["html_pages"]:
        images = [i for i in (ex(p).get("images") or [])
                  if not i.get("aria_hidden") and (i.get("role") or "") != "presentation"]
        total += len(images)
        undescribed = [i for i in images if i.get("alt") is None]
        if undescribed:
            missing_pages.append((p, len(undescribed), len(images)))
            missing_count += len(undescribed)
    if total == 0:
        r.add("EN-13", "Image text alternatives", pillar=ENGAGEMENT, category="friction",
              status=NOT_APPLICABLE, applicability="No images were found in the sample.")
        return
    if not missing_pages:
        r.add("EN-13", "Images declare text alternatives", pillar=ENGAGEMENT,
              category="friction", status=PASS,
              evidence="All %d images across %d pages declare an alt attribute."
                       % (total, len(ctx["html_pages"])), applicable=total, unit="images")
        return
    r.add("EN-13", "Images have no text alternative", pillar=ENGAGEMENT, category="friction",
          status=FAIL, impact_base=2, confidence="high",
          affected=missing_count, applicable=total, unit="images",
          evidence="%d of %d images across %d page(s) have no alt attribute at all (an "
                   "explicitly empty alt=\"\" is treated as a correct decorative declaration "
                   "and is not counted)."
                   % (missing_count, total, len(missing_pages)),
          mechanism="A missing alt attribute leaves screen-reader users with a filename or "
                    "nothing, so any information the image carries is lost to them - and to "
                    "every text-only consumer of the page, including crawlers. Where the image "
                    "is a link, the missing alt also removes the link's only label.",
          evidence_items=[{"url": p["url"],
                           "observed": "%d of %d images have no alt attribute" % (n, tot),
                           "expected": 'descriptive alt text, or alt="" for decorative images'}
                          for p, n, tot in missing_pages[:6]],
          affected_urls=[p["url"] for p, _, _ in missing_pages],
          remediation={"summary": "Add alt text everywhere, using an empty alt for decoration.",
                       "steps": ["Describe what the image conveys in context, not what it depicts in the abstract.",
                                 'Use `alt=""` for purely decorative images so assistive technology skips them.',
                                 "For an image used as a link, the alt text must describe the destination.",
                                 "Enforce a non-null alt attribute in the CMS or component API."],
                       "expected_benefit": "Restores content that is currently invisible to "
                                           "assistive technology and to text extractors.",
                       "effort": "low"},
          dedupe_key="facts-in-images")


def check_breadcrumbs(snap, ctx, r):
    if not guard(ctx, r, "EN-14", "Positional context on deep pages", "continuity"):
        return
    deep = [p for p in ctx["html_pages"]
            if len([s for s in urlsplit(p["url"]).path.split("/") if s]) >= 2]
    if len(deep) < 2:
        r.add("EN-14", "Positional context on deep pages", pillar=ENGAGEMENT,
              category="continuity", status=NOT_APPLICABLE,
              applicability="The site's crawled structure is shallow (%d page(s) two or more "
                            "levels deep), so breadcrumbs are not yet warranted." % len(deep))
        return
    def has_breadcrumb(page):
        e = ex(page)
        for raw in e.get("jsonld_blocks") or []:
            if "breadcrumb" in raw.lower():
                return True
        for anchor in e.get("anchors") or []:
            if "breadcrumb" in (anchor.get("rel") or "").lower():
                return True
        if any("breadcrumb" in (t or "").lower() for t in (e.get("microdata_types") or [])):
            return True
        nav_text = (e.get("nav_text_excerpt") or "")
        return bool(re.search(r"(?:^|\s)(?:home|start)\s*[/>»›|]\s*\w", nav_text, re.I))
    without = [p for p in deep if not has_breadcrumb(p)]
    if not without:
        r.add("EN-14", "Deep pages show where the visitor is", pillar=ENGAGEMENT,
              category="continuity", status=PASS,
              evidence="All %d deep page(s) expose a breadcrumb trail." % len(deep),
              applicable=len(deep))
        return
    r.add("EN-14", "Deep pages give no sense of where the visitor is",
          pillar=ENGAGEMENT, category="continuity", status=FAIL,
          impact_base=1, confidence="medium", affected=len(without), applicable=len(deep),
          evidence="%d of %d pages two or more levels deep show no breadcrumb trail in "
                   "markup or in navigation text." % (len(without), len(deep)),
          mechanism="Most traffic from search and from assistant citations lands deep, not on "
                    "the home page. A breadcrumb is the cheapest way to answer 'where am I and "
                    "what else is near this?' - without it the visitor's only options are the "
                    "top-level menu or the back button. Breadcrumb structured data also gives "
                    "retrieval systems the page's place in the site hierarchy.",
          evidence_items=[{"url": p["url"], "observed": "no breadcrumb trail",
                           "expected": "a visible trail plus BreadcrumbList structured data"}
                          for p in without[:6]],
          affected_urls=[p["url"] for p in without],
          remediation={"summary": "Add a breadcrumb trail to deep templates, in markup and in JSON-LD.",
                       "steps": ["Render the trail from the page's real hierarchy, with every ancestor a working link.",
                                 "Mirror it as `BreadcrumbList` JSON-LD.",
                                 "Keep it above the H1 and do not include the current page as a link."],
                       "expected_benefit": "Deep-landing visitors can place themselves and "
                                           "explore sideways instead of leaving.",
                       "effort": "low"},
          dedupe_key="breadcrumbs-missing")


CHECKS = [
    check_primary_action,
    check_link_labels,
    check_navigation_consistency,
    check_dead_ends,
    check_orphan_pages,
    check_broken_links_visitor_impact,
    check_error_page_recovery,
    check_essential_routes,
    check_pricing_discoverability,
    check_scanability,
    check_mobile_viewport,
    check_form_usability,
    check_image_alt_accessibility,
    check_breadcrumbs,
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", default="-")
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)

    results = Results(PRODUCER)
    try:
        snapshot = load_snapshot(args.snapshot)
    except Exception as exc:
        results.errors.append({"check": "load_snapshot",
                               "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
        snapshot = {}
    if snapshot:
        try:
            context = build_context(snapshot)
        except Exception as exc:
            results.errors.append({"check": "build_context",
                                   "error": "%s: %s" % (type(exc).__name__,
                                                          str(exc)[:200])})
            context = {"pages": [], "html_pages": [], "entry": None,
                       "origin": "", "site": "", "coverage": {}}
        run_checks(results, snapshot, context, CHECKS)

    payload = json.dumps(results.to_dict(), indent=2, ensure_ascii=False)
    if args.out in ("-", "", None):
        sys.stdout.write(payload + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
