#!/usr/bin/env python3
"""Can a machine extract - and safely quote - a specific fact from this site?

Reads a site snapshot produced by `audit-orchestrator` and writes a check-result
document to stdout (or `--out`). Standard library only; no network access.

    python check_citability.py --snapshot snapshot.json --out citability.json

`crawl-render-audit` establishes that a crawler was let in and could read the
page. This skill asks the next question: given that the text arrived, can a
retrieval system pull out *the entity, the offering, the price, the location,
the author* and attach them to the right brand with enough confidence to repeat
them in an answer?

The controlling design rule is **applicability before assertion**. A portfolio
site does not need Product markup; a documentation site does not need
LocalBusiness. Every schema expectation in this file is gated on evidence from
the page itself that the entity in question actually exists there, and the gate
is reported alongside the finding.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

PRODUCER = "ai-citability-audit"
SCHEMA_VERSION = "1.0"

PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_EVALUATED = "NOT_EVALUATED"
INCONCLUSIVE = "INCONCLUSIVE"

AI_DISCOVERABILITY = "ai_discoverability"
ENGAGEMENT = "on_site_engagement"


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
# Structured-data helpers
# ---------------------------------------------------------------------------

# Entities that answer "who publishes this?".
PUBLISHER_TYPES = {"organization", "corporation", "localbusiness", "onlinebusiness",
                   "person", "ngo", "governmentorganization", "nonprofit",
                   "educationalorganization", "medicalorganization", "store",
                   "restaurant", "professionalservice", "sportsorganization",
                   "performinggroup", "brand", "collegeoruniversity", "airline",
                   "librarysystem", "newsmediaorganization", "researchorganization",
                   "fundingscheme", "consortium"}
# Entities that describe the *document*, not the publisher. Useful, but they
# cannot stand in for an identity node.
DOCUMENT_TYPES = {"website", "webpage", "collectionpage", "aboutpage",
                  "softwareapplication", "webapplication", "mobileapplication"}
IDENTITY_TYPES = PUBLISHER_TYPES | DOCUMENT_TYPES
LOCAL_TYPES = {"localbusiness", "store", "restaurant", "professionalservice",
               "medicalclinic", "dentist", "hotel", "lodgingbusiness",
               "automotivebusiness", "healthandbeautybusiness", "foodestablishment",
               "sportsactivitylocation", "realestateagent", "legalservice",
               "financialservice", "homeandconstructionbusiness", "place"}
PRODUCT_TYPES = {"product", "offer", "aggregateoffer", "productmodel",
                 "individualproduct", "productgroup", "vehicle", "book", "course",
                 "service", "menuitem", "softwareapplication", "creativework"}
ARTICLE_TYPES = {"article", "blogposting", "newsarticle", "techarticle",
                 "scholarlyarticle", "report", "liveblogposting", "posting",
                 "socialmediaposting", "review", "webpage", "aboutpage",
                 "collectionpage", "howto", "recipe", "podcastepisode", "videoobject"}

# Third-party hosts that act as independent corroboration of an entity.
IDENTITY_HOSTS = (
    "wikipedia.org", "wikidata.org", "linkedin.com", "crunchbase.com",
    "github.com", "x.com", "twitter.com", "facebook.com", "instagram.com",
    "youtube.com", "tiktok.com", "pinterest.com", "yelp.com", "trustpilot.com",
    "g2.com", "capterra.com", "glassdoor.com", "bbb.org", "companieshouse.gov.uk",
    "opencorporates.com", "producthunt.com", "stackoverflow.com", "medium.com",
    "threads.net", "mastodon.social", "bsky.app", "apple.com", "play.google.com",
    "goodreads.com", "imdb.com", "orcid.org", "scholar.google.com", "researchgate.net",
)

_PRICE_RE = re.compile(
    r"(?:[$€£¥₹₽₩¢]|\b(?:usd|eur|gbp|inr|aud|cad|chf|jpy|sek|nok|dkk|pln|zar|brl|mxn|aed|sgd)\b)"
    r"\s?\d[\d,.\s]*|\d[\d,.]*\s?(?:usd|eur|gbp|inr|aud|cad|dollars?|euros?|pounds?|rupees?)\b",
    re.I)
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{3,4}[\s.\-]\d{3,4}(?:[\s.\-]\d{2,4})?")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_POSTCODE_RE = re.compile(
    r"\b(?:\d{5}(?:-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}|\d{4}\s?[A-Z]{2}|\d{6})\b")
_STREET_RE = re.compile(
    r"\b\d{1,5}[a-z]?\s+[A-Z][\w'.\-]*(?:\s+[A-Z]?[\w'.\-]+){0,4}\s+"
    r"(street|st|road|rd|avenue|ave|boulevard|blvd|lane|ln|drive|dr|way|court|ct|"
    r"place|pl|square|sq|terrace|parkway|highway|hwy|strasse|straße|rue|via|calle)\b",
    re.I)
_CART_RE = re.compile(
    r"\b(add to (?:cart|bag|basket)|buy now|add to trolley|in stock|out of stock|"
    r"free shipping|sku\b|order now|checkout|pre-?order)\b", re.I)
_BYLINE_RE = re.compile(r"\b(by|written by|author|posted by|reviewed by)\s+[A-Z][\w'\-]+", re.I)
_DATE_TEXT_RE = re.compile(
    r"\b(?:\d{1,2}\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)
_GENERIC_H1 = re.compile(
    r"^(home|homepage|welcome|welcome!|hello|index|untitled|main|start|"
    r"welcome to (?:our|the)?\s*\w*(?:\s*website)?|coming soon)$", re.I)


def flatten_jsonld(node, out, depth=0):
    """Collect every schema.org node in a JSON-LD document, including @graph."""
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node:
            flatten_jsonld(item, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    if "@graph" in node:
        flatten_jsonld(node["@graph"], out, depth + 1)
    if "@type" in node or "@id" in node:
        out.append(node)
    for key, value in node.items():
        if key in ("@context", "@type"):
            continue
        if isinstance(value, (dict, list)):
            flatten_jsonld(value, out, depth + 1)


def type_tokens(node) -> set:
    raw = node.get("@type") if isinstance(node, dict) else None
    if raw is None:
        return set()
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).split("/")[-1].split("#")[-1].strip().lower() for v in values if v}


def parse_page_jsonld(page):
    """Return (nodes, errors) for one page's JSON-LD blocks."""
    nodes, errors = [], []
    for index, raw in enumerate(ex(page).get("jsonld_blocks") or []):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception as exc:
            errors.append({"index": index, "error": str(exc)[:160],
                           "excerpt": text[:160]})
            continue
        flatten_jsonld(data, nodes)
    return nodes, errors


def ex(page):
    return page.get("extraction") or {}


def short(url, origin):
    if origin and url.startswith(origin):
        return url[len(origin):] or "/"
    return url


def page_text(page):
    return (ex(page).get("main_text_excerpt") or ex(page).get("text_excerpt") or "")


def all_text(page):
    return " ".join(filter(None, [ex(page).get("text_excerpt"),
                                  ex(page).get("main_text_excerpt")]))


def norm_name(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


_TITLE_SPLIT = re.compile(r"\s+[|–—·:\-]\s+")


def common_title_affix(pages) -> str:
    """The title segment the site repeats on most pages - i.e. its own name.

    Returns "" unless at least two pages share the same leading or trailing
    segment, so a single-page sample never produces a spurious brand string.
    """
    segments = []
    for p in pages:
        title = (ex(p).get("title") or "").strip()
        if not title:
            continue
        parts = [s.strip() for s in _TITLE_SPLIT.split(title) if s.strip()]
        if len(parts) > 1:
            segments.append((parts[0], parts[-1]))
    if len(segments) < 2:
        return ""
    counts = {}
    for first, last in segments:
        counts[first] = counts.get(first, 0) + 1
        counts[last] = counts.get(last, 0) + 1
    best, hits = max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))
    return best if hits >= 2 else ""


# ---------------------------------------------------------------------------
# Page-role inference. These gates are what stop the skill inventing defects.
# ---------------------------------------------------------------------------

def infer_roles(page) -> set:
    """Which schema.org entity roles does *this page's own content* justify?"""
    e = ex(page)
    url = page["url"].lower()
    path = urlsplit(url).path
    text = all_text(page)
    heads = " ".join(h["text"] for h in (e.get("headings") or []))
    roles = set()

    price_hits = _PRICE_RE.findall(text)
    if (_CART_RE.search(text) and price_hits) or (
            re.search(r"/(product|products|p|item|items|shop|store|collections|dp)/", path)
            and price_hits):
        roles.add("product")
    if re.search(r"/(pricing|plans|packages|tariffs?)\b", path) and price_hits:
        roles.add("offer")

    words = e.get("main_word_count", 0)
    editorial_path = re.search(
        r"/(blog|news|article|articles|post|posts|insights|stories|press|"
        r"newsroom|research|resources|updates|journal|magazine)(/|$)", path)
    dated = bool(_DATE_TEXT_RE.search(text[:1200])) or bool(e.get("time_elements"))
    if words >= 250 and (editorial_path or (dated and _BYLINE_RE.search(text[:800]))):
        roles.add("article")

    q_headings = [h for h in (e.get("headings") or []) if h["text"].strip().endswith("?")]
    if len(q_headings) >= 3 or (re.search(r"/(faq|faqs|help|support|questions)(/|$)", path)
                                and len(q_headings) >= 2):
        roles.add("faq")

    has_address = bool(_STREET_RE.search(text) and _POSTCODE_RE.search(text))
    has_phone = bool(_PHONE_RE.search(text))
    if has_address and (has_phone or re.search(r"\b(opening hours|hours|visit us|directions|"
                                               r"find us|our (?:store|shop|office|clinic|showroom))\b",
                                               text, re.I)):
        roles.add("local_business")
    if has_phone or _EMAIL_RE.search(text) or re.search(r"/(contact|kontakt|contacto)", path):
        roles.add("contact")

    steps = re.findall(r"\b(?:step\s*\d|^\d+\.)\s", heads + " " + text[:2000], re.M | re.I)
    if len(steps) >= 3 and re.search(r"/(how-to|howto|guide|tutorial)", path):
        roles.add("howto")
    return roles


def role_pages(pages, role):
    return [p for p in pages if role in p["_roles"]]


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
        nodes, errors = parse_page_jsonld(p)
        p["_jsonld"] = nodes
        p["_jsonld_errors"] = errors
        p["_types"] = set().union(*[type_tokens(n) for n in nodes]) if nodes else set()
        p["_roles"] = infer_roles(p)
    entry = next((p for p in html_pages if p.get("depth") == 0), None)
    return {"pages": pages, "html_pages": html_pages, "entry": entry,
            "origin": snapshot.get("origin") or "", "site": snapshot.get("site") or "",
            "coverage": as_dict(snapshot.get("coverage"))}


def guard(ctx, r, check_id, title, category=""):
    """Emit NOT_EVALUATED and return False when no HTML was parsed."""
    if ctx["html_pages"]:
        return True
    r.add(check_id, title, pillar=AI_DISCOVERABILITY, category=category or "structured_data",
          status=NOT_EVALUATED,
          applicability="No HTML page was successfully parsed, so page content could not "
                        "be assessed. See the crawl-access findings for the reason.")
    return False


# ---------------------------------------------------------------------------
# Structured data
# ---------------------------------------------------------------------------

def check_structured_data_presence(snap, ctx, r):
    if not guard(ctx, r, "AC-01", "Structured data coverage"):
        return
    pages = ctx["html_pages"]
    with_jsonld = [p for p in pages if p["_jsonld"]]
    with_micro = [p for p in pages if ex(p).get("microdata_types") or ex(p).get("rdfa_types")]
    covered = {p["url"] for p in with_jsonld} | {p["url"] for p in with_micro}
    if covered:
        r.add("AC-01", "Pages publish machine-readable structured data",
              pillar=AI_DISCOVERABILITY, category="structured_data", status=PASS,
              evidence="%d of %d sampled pages expose structured data (%d via JSON-LD, "
                       "%d via microdata/RDFa)."
                       % (len(covered), len(pages), len(with_jsonld), len(with_micro)),
              applicable=len(pages))
        if len(covered) < len(pages):
            uncovered = [p for p in pages if p["url"] not in covered]
            r.add("AC-02", "Structured data is applied inconsistently across templates",
                  pillar=AI_DISCOVERABILITY, category="structured_data", status=FAIL,
                  impact_base=1, confidence="high",
                  affected=len(uncovered), applicable=len(pages),
                  evidence="%d of %d sampled pages carry structured data; %d do not (%s)."
                           % (len(covered), len(pages), len(uncovered),
                              ", ".join(short(p["url"], ctx["origin"]) for p in uncovered[:4])),
                  mechanism="Partial coverage means the templates without markup are read as "
                            "plain prose while their siblings are read as typed entities. The "
                            "unmarked pages lose the exact-value extraction the rest of the "
                            "site enjoys, so facts on them are quoted less reliably.",
                  evidence_items=[{"url": p["url"], "observed": "no JSON-LD, microdata or RDFa",
                                   "expected": "the same entity markup as sibling templates"}
                                  for p in uncovered[:6]],
                  affected_urls=[p["url"] for p in uncovered],
                  remediation={"summary": "Extend the existing markup pattern to the templates that lack it.",
                               "steps": ["Identify which templates render structured data and which do not.",
                                         "Move the markup into a shared layout partial parameterised by page type."],
                               "expected_benefit": "Uniform machine reading across the site.",
                               "effort": "low"},
                  dedupe_key="structured-data-partial")
        return
    r.add("AC-01", "No machine-readable structured data anywhere in the sample",
          pillar=AI_DISCOVERABILITY, category="structured_data", status=FAIL,
          impact_base=3, confidence="high", affected=len(pages), applicable=len(pages),
          evidence="0 of %d sampled pages contain parseable JSON-LD, microdata or RDFa."
                   % len(pages),
          mechanism="Structured data is the only channel that states a fact as a typed value "
                    "rather than as prose a machine must guess at. Without it, a system that "
                    "wants your organisation's name, a price, an address or an author has to "
                    "infer them from layout - which fails silently and produces confident but "
                    "wrong answers about the brand.",
          evidence_items=[{"url": p["url"], "observed": "no structured data of any vocabulary",
                           "expected": "JSON-LD describing the page's primary entity"}
                          for p in pages[:6]],
          affected_urls=[p["url"] for p in pages],
          remediation={
              "summary": "Add JSON-LD for the primary entity of each template, starting with "
                         "site identity on the home page.",
              "steps": ["Home page: one `Organization` (or `Person` for an individual) with `name`, `url`, `logo`, `description` and `sameAs` pointing at your verified profiles.",
                        "Add `WebSite` with `url` and `name` so the site itself is a named entity.",
                        "Then mark up whichever entity each template is actually about - and only that entity.",
                        "Emit the JSON-LD server-side so it is present without JavaScript, and validate that every value also appears in the visible text."],
              "expected_benefit": "Turns the site's core facts into values a machine can read "
                                  "exactly, which is the precondition for being quoted accurately.",
              "effort": "medium"},
          dedupe_key="structured-data-missing", tags=["render-gap-symptom"])


def check_malformed_jsonld(snap, ctx, r):
    if not guard(ctx, r, "AC-03", "JSON-LD validity"):
        return
    pages = ctx["html_pages"]
    with_blocks = [p for p in pages if (ex(p).get("jsonld_blocks") or [])]
    broken = [p for p in with_blocks if p["_jsonld_errors"]]
    if not with_blocks:
        r.add("AC-03", "JSON-LD validity", pillar=AI_DISCOVERABILITY,
              category="structured_data", status=NOT_APPLICABLE,
              applicability="No page emitted a JSON-LD block, so there is nothing to validate. "
                            "Absence is reported by AC-01, not here.")
        return
    if not broken:
        r.add("AC-03", "All JSON-LD blocks parse", pillar=AI_DISCOVERABILITY,
              category="structured_data", status=PASS,
              evidence="%d of %d pages with JSON-LD parsed without error."
                       % (len(with_blocks), len(with_blocks)), applicable=len(with_blocks))
        return
    r.add("AC-03", "JSON-LD blocks are malformed and cannot be parsed",
          pillar=AI_DISCOVERABILITY, category="structured_data", status=FAIL,
          impact_base=3, confidence="high", affected=len(broken), applicable=len(with_blocks),
          evidence="%d of %d pages carrying JSON-LD contain at least one block that fails to "
                   "parse (first error: %s)."
                   % (len(broken), len(with_blocks), broken[0]["_jsonld_errors"][0]["error"]),
          mechanism="A JSON syntax error causes the entire block to be discarded, not "
                    "partially read. The page therefore behaves exactly as if it had no "
                    "structured data at all - while every validation dashboard that only "
                    "checks for the presence of a script tag reports success.",
          evidence_items=[{"url": p["url"],
                           "observed": p["_jsonld_errors"][0]["error"],
                           "expected": "valid JSON",
                           "excerpt": p["_jsonld_errors"][0]["excerpt"][:140]}
                          for p in broken[:6]],
          affected_urls=[p["url"] for p in broken],
          remediation={"summary": "Fix the JSON syntax and validate the output, not the template.",
                       "steps": ["Common causes: a trailing comma, an unescaped quote or apostrophe inside a value, a raw newline in a string, or an unescaped `</script>` in a description.",
                                 "Build the object as a data structure and serialise it with a JSON encoder rather than concatenating strings in the template.",
                                 "Add a build-time or CI check that parses every emitted `application/ld+json` block."],
                       "expected_benefit": "Recovers structured data that is already written but currently ignored.",
                       "effort": "low"},
          dedupe_key="jsonld-malformed")


def check_site_identity(snap, ctx, r):
    if not guard(ctx, r, "AC-04", "Site identity markup", "entity_identity"):
        return
    entry = ctx["entry"]
    if entry is None:
        r.add("AC-04", "Site identity markup", pillar=AI_DISCOVERABILITY,
              category="entity_identity", status=NOT_EVALUATED,
              applicability="The entry page was not parsed.")
        return
    # `WebSite` and `WebPage` describe the *document*, not the organisation
    # behind it. Only a publisher-type node answers "who is this?", so the two
    # are kept apart: demanding `logo` and `sameAs` from a WebSite node would be
    # asking for properties that entity does not have.
    identity_nodes = [n for n in entry["_jsonld"] if type_tokens(n) & PUBLISHER_TYPES]
    document_nodes = [n for n in entry["_jsonld"] if type_tokens(n) & DOCUMENT_TYPES]
    micro_identity = [t for t in (ex(entry).get("microdata_types") or [])
                      if t.split("/")[-1].lower() in PUBLISHER_TYPES]
    if not identity_nodes and not micro_identity:
        present = ", ".join(sorted(entry["_types"])) or "none"
        r.add("AC-04", "The site does not declare who it is in machine-readable form",
              pillar=AI_DISCOVERABILITY, category="entity_identity", status=FAIL,
              impact_base=3, confidence="high", affected=1, applicable=1, unit="sites",
              evidence="The entry page (%s) publishes no Organization, Person or "
                       "LocalBusiness entity in structured data. %s"
                       % (short(entry["url"], ctx["origin"]),
                          ("It declares %s, which describes the document rather than the "
                           "organisation behind it." % "/".join(sorted(
                               set().union(*[type_tokens(n) for n in document_nodes])))
                           if document_nodes else
                           "%d JSON-LD node(s) were found, none of an identity type "
                           "(types present: %s)." % (len(entry["_jsonld"]), present))),
              mechanism="Entity resolution is the step where an assistant decides *which* "
                        "real-world thing a page is about. With no declared identity node, the "
                        "system must guess from the domain and page text, and a brand whose "
                        "name is a common word or shared with another company is routinely "
                        "merged with the wrong entity - after which every fact it repeats "
                        "about you is really about someone else.",
              evidence_items=[{"url": entry["url"],
                               "observed": "types present: %s" % (", ".join(sorted(entry["_types"])) or "none"),
                               "expected": "an Organization/Person node with name, url, logo and sameAs"}],
              affected_urls=[entry["url"]],
              remediation={
                  "summary": "Publish one identity node on the home page and reference it site-wide.",
                  "steps": ['Add JSON-LD `{"@type":"Organization","@id":"https://host/#organization","name":...,"url":...,"logo":...,"description":...,"sameAs":[...]}`.',
                            "Use the exact legal or trading name you want assistants to repeat, spelled identically everywhere.",
                            "Reference the same `@id` from other nodes (`publisher`, `provider`) instead of repeating the object.",
                            "For a sole trader or personal brand use `Person` rather than `Organization`."],
                  "expected_benefit": "Gives assistants a stable anchor for the brand, which is "
                                      "what prevents confusion with similarly-named entities.",
                  "effort": "low"},
              dedupe_key="identity-missing")
        return

    # Properties are merged across every publisher-type node on the page: a site
    # that declares `name` on one node and `logo` on another has supplied both,
    # and telling it otherwise would be a false positive.
    merged = {}
    for candidate in identity_nodes:
        for key, value in candidate.items():
            if value and key not in merged:
                merged[key] = value
    node = merged
    missing = [f for f in ("name", "url", "description") if not node.get(f)]
    if not (node.get("logo") or node.get("image")):
        missing.append("logo")
    same_as = node.get("sameAs") or []
    if isinstance(same_as, str):
        same_as = [same_as]
    if missing or not same_as:
        gaps = missing + ([] if same_as else ["sameAs"])
        r.add("AC-04", "Site identity is declared but incomplete", pillar=AI_DISCOVERABILITY,
              category="entity_identity", status=FAIL,
              impact_base=2 if not same_as else 1, confidence="high",
              affected=len(gaps), applicable=5, unit="properties",
              evidence="The entry page declares %s but supplies none of: %s (properties "
                       "were counted across all %d identity node(s) on the page)."
                       % ("/".join(sorted(set().union(
                              *[type_tokens(n) for n in identity_nodes]))) or "an identity node",
                          ", ".join(gaps), len(identity_nodes)),
              mechanism="Identity properties are the joins a knowledge graph uses. `sameAs` in "
                        "particular links your page to profiles that already exist in those "
                        "graphs; without it the entity you declare is an island and cannot be "
                        "matched to the record an assistant already holds about you.",
              evidence_items=[{"url": entry["url"],
                               "observed": "identity nodes supply none of: %s" % ", ".join(gaps),
                               "expected": "name, url, description, logo and sameAs"}],
              affected_urls=[entry["url"]],
              remediation={"summary": "Complete the identity node, prioritising `sameAs`.",
                           "steps": ["Add `sameAs` entries for the profiles you control and that already rank for the brand name (company registry, LinkedIn, Wikipedia/Wikidata if present, primary social accounts, app store listings).",
                                     "Add a `description` that states what the organisation does in one sentence, matching the wording on the page.",
                                     "Point `logo` at a stable, directly-fetchable image URL."],
                           "expected_benefit": "Connects the site's declared identity to records "
                                               "assistants already trust.",
                           "effort": "low"},
              dedupe_key="identity-incomplete")
    else:
        r.add("AC-04", "Site identity is declared and linked to external profiles",
              pillar=AI_DISCOVERABILITY, category="entity_identity", status=PASS,
              evidence="The entry page declares %s with name, url, description, logo and %d "
                       "sameAs reference(s)."
                       % ("/".join(sorted(set().union(
                              *[type_tokens(n) for n in identity_nodes]))), len(same_as)),
              applicable=1, unit="sites")


def check_entity_name_consistency(snap, ctx, r):
    if not guard(ctx, r, "AC-05", "Brand name consistency", "entity_identity"):
        return
    entry = ctx["entry"]
    if entry is None:
        r.add("AC-05", "Brand name consistency", pillar=AI_DISCOVERABILITY,
              category="entity_identity", status=NOT_EVALUATED,
              applicability="The entry page was not parsed.")
        return
    e = ex(entry)
    candidates = {}
    for node in entry["_jsonld"]:
        if type_tokens(node) & IDENTITY_TYPES and isinstance(node.get("name"), str):
            candidates.setdefault("structured data (%s)" % "/".join(sorted(type_tokens(node))),
                                  node["name"].strip())
    site_name = (e.get("meta_property") or {}).get("og:site_name")
    if site_name:
        candidates["og:site_name"] = site_name.strip()
    # The brand element of a title is the segment repeated across pages, not
    # simply the last one: both "Brand | Page" and "Page | Brand" are common,
    # so guessing by position produces false mismatches.
    affix = common_title_affix(ctx["html_pages"])
    if affix:
        candidates["title boilerplate"] = affix
    footer = e.get("text_excerpt") or ""
    m = re.search(r"(?:©|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*([A-Z][\w&'.\-]*(?:\s+[A-Z][\w&'.\-]*){0,4})",
                  footer, re.I)
    if m:
        candidates["copyright line"] = m.group(1).strip()

    if len(candidates) < 2:
        r.add("AC-05", "Brand name consistency", pillar=AI_DISCOVERABILITY,
              category="entity_identity", status=INCONCLUSIVE,
              applicability="Fewer than two independent name signals were present on the "
                            "entry page, so consistency cannot be assessed. Signals found: %s."
                            % (", ".join(candidates) or "none"))
        return
    normed = {}
    for source, value in candidates.items():
        normed.setdefault(norm_name(value), []).append((source, value))
    # Treat one name being a prefix of another (e.g. "Acme" vs "Acme Ltd") as
    # consistent: legal suffixes are not an inconsistency.
    keys = sorted(normed, key=len)
    clusters = []
    for key in keys:
        if any(key.startswith(c) or c.startswith(key) for c in clusters):
            continue
        clusters.append(key)
    if len(clusters) <= 1:
        r.add("AC-05", "The brand name is stated consistently", pillar=AI_DISCOVERABILITY,
              category="entity_identity", status=PASS,
              evidence="%d independent name signals on the entry page agree (%s)."
                       % (len(candidates),
                          "; ".join("%s: %r" % (s, v) for s, v in list(candidates.items())[:4])),
              applicable=len(candidates), unit="signals")
        return
    variants = []
    for key in clusters:
        source, value = normed[key][0]
        variants.append("%r (from %s)" % (value, ", ".join(
            src for src, _ in normed[key])))
    r.add("AC-05", "The site states its own name inconsistently", pillar=AI_DISCOVERABILITY,
          category="entity_identity", status=FAIL, impact_base=2, confidence="medium",
          affected=len(clusters), applicable=len(candidates), unit="signals",
          evidence="Across %d name signals on the entry page there are %d irreconcilable "
                   "variants: %s." % (len(candidates), len(clusters), "; ".join(variants)),
          mechanism="Entity resolution matches a page to a known entity partly by name string. "
                    "When the structured data, the title and the copyright line disagree, the "
                    "matcher either splits the brand into several weak entities or picks the "
                    "wrong one - which is how a company ends up described with a competitor's "
                    "or a subsidiary's facts.",
          evidence_items=[{"url": entry["url"], "observed": "%s = %r" % (s, v),
                           "expected": "one canonical brand name across all signals"}
                          for s, v in candidates.items()],
          affected_urls=[entry["url"]],
          remediation={"summary": "Choose one canonical name string and use it in every signal.",
                       "steps": ["Decide the exact public name (including or excluding the legal suffix - pick one).",
                                 "Set it identically in the Organization `name`, `og:site_name`, the title template suffix and the footer copyright line.",
                                 "If you genuinely trade under several names, model them as `alternateName` on one entity rather than as separate names."],
                       "expected_benefit": "One strong entity instead of several weak, "
                                           "confusable ones.",
                       "effort": "low"},
          dedupe_key="entity-name-inconsistent")


def check_role_specific_schema(snap, ctx, r):
    """One check per inferred role - each gated on evidence from the page."""
    if not guard(ctx, r, "AC-06", "Entity-appropriate structured data"):
        return
    pages = ctx["html_pages"]
    specs = [
        ("AC-06", "product", PRODUCT_TYPES, "Product/Offer",
         "pages that show a price together with purchase controls",
         "Product markup carries the exact name, price, availability and identifiers. "
         "Without it a shopping or answer surface has to scrape a price out of layout, "
         "which it usually declines to do - so the product is simply not quoted, or is "
         "quoted with a stale price picked up from a third-party listing.",
         ["Add `Product` with `name`, `description`, `image`, `sku`/`gtin` where you have one, and a nested `Offer` with `price`, `priceCurrency` and `availability`.",
          "Emit exactly the price the page displays, and update it in the same code path that updates the visible price.",
          "Do not add `aggregateRating` unless real, visible reviews back it."], 3),
        ("AC-07", "article", ARTICLE_TYPES, "Article/BlogPosting",
         "long-form pages under an editorial path with a date or byline",
         "Article markup supplies the headline, author, publisher and - critically - the "
         "publication and modification dates. Retrieval systems weight recency heavily "
         "for anything that looks like news or guidance; an undated article is treated "
         "as being of unknown age and loses to a dated competitor even when it is newer.",
         ["Add `Article` (or `BlogPosting`/`NewsArticle`) with `headline`, `datePublished`, `dateModified`, `author` and `publisher`.",
          "Use ISO-8601 dates with a timezone offset.",
          "Point `publisher` at the same `@id` as the site's Organization node."], 3),
        ("AC-08", "local_business",
         LOCAL_TYPES | {"organization", "place", "postaladdress", "contactpoint"},
         "PostalAddress",
         "pages that display a street address alongside a phone number or opening hours",
         "A visible address is prose to a machine. Structured `PostalAddress`, `geo`, "
         "`telephone` and `openingHours` are what let an assistant answer 'where are "
         "they and are they open' - and what keeps your own details, rather than an "
         "out-of-date directory listing, as the source.",
         ["Add `address` as a nested `PostalAddress` on the Organization node, with `streetAddress`, `addressLocality`, `postalCode` and `addressCountry`.",
          "If the location is somewhere customers actually visit, use `LocalBusiness` (or the closest subtype) and add `telephone`, `openingHoursSpecification` and `geo`; if it is only a registered office, `Organization` with an address is the correct and sufficient markup.",
          "Keep the values byte-identical to what the page displays.",
          "For several locations, publish one node per location on its own page."], 2),
    ]
    titles = {"product": "Pages that sell something carry no Product/Offer markup",
              "article": "Editorial pages carry no Article structured data",
              "local_business": "Pages showing a physical address publish no "
                                "machine-readable address"}
    for check_id, role, accepted, label, gate, mechanism, steps, impact in specs:
        candidates = role_pages(pages, role)
        if not candidates:
            r.add(check_id, "%s structured data" % label, pillar=AI_DISCOVERABILITY,
                  category="structured_data", status=NOT_APPLICABLE,
                  applicability="No sampled page matched the precondition for this entity "
                                "type (%s), so the markup is not expected." % gate)
            continue
        marked = [p for p in candidates
                  if (p["_types"] & accepted)
                  or any(t.split("/")[-1].lower() in accepted
                         for t in (ex(p).get("microdata_types") or []))]
        if len(marked) == len(candidates):
            r.add(check_id, "%s structured data is present where it applies" % label,
                  pillar=AI_DISCOVERABILITY, category="structured_data", status=PASS,
                  evidence="%d of %d sampled pages matching '%s' carry %s markup."
                           % (len(marked), len(candidates), gate, label),
                  applicable=len(candidates))
            continue
        missing = [p for p in candidates if p not in marked]
        r.add(check_id, titles[role],
              pillar=AI_DISCOVERABILITY, category="structured_data", status=FAIL,
              impact_base=impact, confidence="high",
              affected=len(missing), applicable=len(candidates),
              evidence="%d of %d sampled pages that qualify as %s contain no %s markup (%s)."
                       % (len(missing), len(candidates), gate, label,
                          ", ".join(short(p["url"], ctx["origin"]) for p in missing[:4])),
              mechanism=mechanism,
              evidence_items=[{"url": p["url"],
                               "observed": "qualifies as %s; types present: %s"
                                           % (role, ", ".join(sorted(p["_types"])) or "none"),
                               "expected": "%s markup" % label,
                               "excerpt": page_text(p)[:120]} for p in missing[:6]],
              affected_urls=[p["url"] for p in missing],
              remediation={"summary": "Add %s markup to the %d qualifying template(s)."
                                      % (label, len(missing)),
                           "steps": steps,
                           "expected_benefit": "Turns the page's defining facts into exact "
                                               "values a machine can quote.",
                           "effort": "medium"},
              dedupe_key="schema-missing-%s" % role,
              applicability="Raised only for pages that demonstrably contain this entity: %s. "
                            "Pages without that evidence are excluded from the denominator."
                            % gate)


def check_structured_data_matches_page(snap, ctx, r):
    """Structured data that asserts something the page does not show."""
    if not guard(ctx, r, "AC-09", "Structured data fidelity"):
        return
    pages = [p for p in ctx["html_pages"] if p["_jsonld"]]
    if not pages:
        r.add("AC-09", "Structured data fidelity", pillar=AI_DISCOVERABILITY,
              category="structured_data", status=NOT_APPLICABLE,
              applicability="No page emitted parseable JSON-LD, so there are no asserted "
                            "values to compare against the visible page.")
        return
    problems = []
    for p in pages:
        text = all_text(p).lower()
        heads = " ".join(h["text"] for h in (ex(p).get("headings") or [])).lower()
        haystack = text + " " + heads + " " + (ex(p).get("title") or "").lower()
        for node in p["_jsonld"]:
            types = type_tokens(node)
            name = node.get("name") if isinstance(node.get("name"), str) else None
            if name and len(name) > 3 and types & (PRODUCT_TYPES | ARTICLE_TYPES):
                if norm_name(name) and norm_name(name) not in norm_name(haystack):
                    problems.append((p, "name", name,
                                     "the name is not present in the page's visible text"))
            rating = node.get("aggregateRating")
            if isinstance(rating, dict) and rating.get("ratingValue"):
                if not re.search(r"\b(review|rating|stars?|out of 5|\d\.\d\s*/\s*5)\b", text):
                    problems.append((p, "aggregateRating", str(rating.get("ratingValue")),
                                     "a rating is asserted but no review or rating text is "
                                     "visible on the page"))
            offers = node.get("offers")
            offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                price = offer.get("price")
                if price in (None, ""):
                    continue
                digits = re.sub(r"[^\d]", "", str(price)).lstrip("0")
                # Compare against every number on the page, not only strings the
                # price regex recognises: pages write prices as "490 US dollars"
                # or "from 490/month", and requiring a currency-adjacent match
                # produced false "the price is not on the page" findings.
                page_numbers = {re.sub(r"[^\d]", "", m).lstrip("0")
                                for m in re.findall(r"\d[\d,.\s]*", text)}
                page_numbers.discard("")
                if digits and not any(digits == n or digits in n
                                      for n in page_numbers):
                    problems.append((p, "offers.price", str(price),
                                     "the asserted price does not appear anywhere in the "
                                     "page text"))
    if not problems:
        r.add("AC-09", "Structured data matches the visible page", pillar=AI_DISCOVERABILITY,
              category="structured_data", status=PASS,
              evidence="Names, prices and ratings asserted in JSON-LD on %d page(s) were all "
                       "found in the visible content." % len(pages), applicable=len(pages))
        return
    affected = {p["url"] for p, _, _, _ in problems}
    r.add("AC-09", "Structured data asserts values the page does not show",
          pillar=AI_DISCOVERABILITY, category="structured_data", status=FAIL,
          impact_base=3, confidence="medium", affected=len(affected), applicable=len(pages),
          evidence="%d of %d pages with JSON-LD assert a value that is absent from the visible "
                   "content (%d mismatch(es); first: %s = %r on %s - %s)."
                   % (len(affected), len(pages), len(problems), problems[0][1],
                      problems[0][2][:40], short(problems[0][0]["url"], ctx["origin"]),
                      problems[0][3]),
          mechanism="Structured data is trusted only while it agrees with the rendered page. "
                    "Markup that asserts unseen values is treated as manipulation: the "
                    "specific values are discarded, and repeated mismatches reduce trust in "
                    "the rest of the site's markup. An unbacked `aggregateRating` in "
                    "particular is a well-known spam signal and risks a manual action.",
          evidence_items=[{"url": p["url"], "observed": "%s = %r; %s" % (field, value[:60], why),
                           "expected": "the same value visible in the page content"}
                          for p, field, value, why in problems[:8]],
          affected_urls=sorted(affected),
          remediation={"summary": "Generate the markup from the same data that renders the page.",
                       "steps": ["Build the JSON-LD from the view model rather than from a separate source or a hand-written template.",
                                 "Remove `aggregateRating` and `review` unless the reviews are rendered on the same page.",
                                 "Add a test that asserts the JSON-LD price equals the displayed price."],
                       "expected_benefit": "Keeps the markup trusted, which is what makes exact "
                                           "values usable in answers.",
                       "effort": "medium"},
          dedupe_key="structured-data-mismatch")


def check_jsonld_identity_urls(snap, ctx, r):
    if not guard(ctx, r, "AC-10", "Entity URL stability", "entity_identity"):
        return
    pages = [p for p in ctx["html_pages"] if p["_jsonld"]]
    if not pages:
        r.add("AC-10", "Entity URL stability", pillar=AI_DISCOVERABILITY,
              category="entity_identity", status=NOT_APPLICABLE,
              applicability="No JSON-LD nodes to inspect.")
        return
    anonymous = [p for p in pages
                 if not any(n.get("@id") for n in p["_jsonld"])
                 and (p["_types"] & (IDENTITY_TYPES | PRODUCT_TYPES | ARTICLE_TYPES))]
    if not anonymous:
        r.add("AC-10", "Structured entities carry stable identifiers",
              pillar=AI_DISCOVERABILITY, category="entity_identity", status=PASS,
              evidence="Entities on %d of %d pages with JSON-LD declare an `@id`."
                       % (len(pages) - len(anonymous), len(pages)), applicable=len(pages))
        return
    r.add("AC-10", "Structured entities have no stable identifier",
          pillar=AI_DISCOVERABILITY, category="entity_identity", status=FAIL,
          impact_base=1, confidence="high", affected=len(anonymous), applicable=len(pages),
          evidence="%d of %d pages with JSON-LD declare typed entities without an `@id`."
                   % (len(anonymous), len(pages)),
          mechanism="`@id` is what tells a consumer that the Organization on the pricing page "
                    "and the one on the article are the same thing. Without it each page "
                    "creates a fresh anonymous node, so signals never accumulate onto one "
                    "entity and the brand stays weakly defined no matter how many pages "
                    "describe it.",
          evidence_items=[{"url": p["url"], "observed": "typed nodes without @id",
                           "expected": 'a stable URI such as "https://host/#organization"'}
                          for p in anonymous[:6]],
          affected_urls=[p["url"] for p in anonymous],
          remediation={"summary": "Give each real entity one permanent `@id` and reuse it.",
                       "steps": ['Use fragment URIs on canonical URLs: `https://host/#organization`, `https://host/product/x#product`.',
                                 "Reference the organisation from other nodes with `{\"@id\": \"https://host/#organization\"}` instead of repeating its properties."],
                       "expected_benefit": "Signals from every page accumulate onto one entity.",
                       "effort": "low"},
          dedupe_key="entity-no-id")


# ---------------------------------------------------------------------------
# Extractable facts in plain text
# ---------------------------------------------------------------------------

def check_value_proposition(snap, ctx, r):
    if not guard(ctx, r, "AC-11", "Value proposition clarity", "fact_extraction"):
        return
    entry = ctx["entry"]
    if entry is None:
        r.add("AC-11", "Value proposition clarity", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=NOT_EVALUATED,
              applicability="The entry page was not parsed.")
        return
    e = ex(entry)
    hero = (e.get("first_screen_text") or "").strip()
    h1s = [h["text"].strip() for h in (e.get("headings") or []) if h["level"] == 1]
    h1 = h1s[0] if h1s else ""
    words = len(re.findall(r"[^\W\d_]{2,}", hero))
    generic_h1 = bool(h1) and bool(_GENERIC_H1.match(h1.strip()))
    brand = norm_name((e.get("meta_property") or {}).get("og:site_name") or ctx["site"])
    h1_is_brand_only = bool(h1) and norm_name(h1) == brand and brand

    problems = []
    if words < 15:
        problems.append("the first screen of body text contains only %d words" % words)
    if not h1:
        problems.append("there is no H1")
    elif generic_h1:
        problems.append("the H1 is a placeholder (%r)" % h1)
    elif h1_is_brand_only:
        problems.append("the H1 repeats the brand name (%r) without saying what it offers" % h1)
    if not problems:
        r.add("AC-11", "The entry page states what the site offers in extractable text",
              pillar=AI_DISCOVERABILITY, category="fact_extraction", status=PASS,
              evidence="Entry page H1 %r with %d words of opening body text."
                       % (h1[:80], words), applicable=1, unit="pages")
        return
    r.add("AC-11", "The entry page does not state what the site offers",
          pillar=AI_DISCOVERABILITY, category="fact_extraction", status=FAIL,
          impact_base=3, confidence="high", affected=1, applicable=1, unit="pages",
          evidence="On %s, %s." % (short(entry["url"], ctx["origin"]), "; ".join(problems)),
          mechanism="The opening heading and first paragraph are what both a retrieval system "
                    "and a first-time visitor use to decide what this site is. A brand-only or "
                    "placeholder opening gives an assistant nothing to match a question "
                    "against, so the site is never surfaced for the category it competes in - "
                    "and a visitor who does arrive cannot tell within a few seconds whether "
                    "they are in the right place.",
          evidence_items=[{"url": entry["url"],
                           "observed": "H1: %r; opening text: %r" % (h1[:80], hero[:160]),
                           "expected": "a heading naming the offering and the audience, "
                                       "followed by a sentence of concrete detail"}],
          affected_urls=[entry["url"]],
          remediation={"summary": "Rewrite the opening block to name the offering, the audience "
                                  "and the differentiator in plain text.",
                       "steps": ["H1 pattern that works for both audiences: `<what it is> for <who it is for>` - concrete nouns, no slogan.",
                                 "Follow it with 2-3 sentences that state what the thing does, who uses it and one verifiable specific (a number, a place, a named capability).",
                                 "Put this in the server-rendered HTML, above any interactive component.",
                                 "Avoid category-free language ('we empower', 'next-generation solutions') - it matches no query."],
                       "expected_benefit": "Creates the quotable sentence that lets an assistant "
                                           "introduce the brand correctly, and orients a visitor "
                                           "in the first few seconds.",
                       "effort": "low"},
          dedupe_key="value-proposition", tags=["render-gap-symptom", "semantic-review"])


def check_fact_density(snap, ctx, r):
    if not guard(ctx, r, "AC-12", "Quotable fact density", "fact_extraction"):
        return
    pages = [p for p in ctx["html_pages"] if ex(p).get("main_word_count", 0) >= 60]
    if not pages:
        r.add("AC-12", "Quotable fact density", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=NOT_APPLICABLE,
              applicability="No sampled page carried enough text to assess fact density; "
                            "this is covered by the render/thin-content findings instead.")
        return
    sparse = []
    for p in pages:
        text = page_text(p)
        facts = (len(re.findall(r"\b\d[\d,.]*\s*(?:%|percent|years?|months?|days?|hours?|"
                                r"customers?|clients?|users?|countries|locations?|employees?|"
                                r"kg|km|mi|mm|cm|gb|mb|tb|hz|mph|kw)\b", text, re.I))
                 + len(_PRICE_RE.findall(text))
                 + len(_DATE_TEXT_RE.findall(text))
                 + len(re.findall(r"\b(?:is|are|provides?|offers?|includes?|supports?|"
                                  r"delivers?|serves?|specialis(?:es|ing)|specializ(?:es|ing))\b",
                                  text, re.I)))
        if facts <= 1:
            sparse.append((p, facts))
    if not sparse:
        r.add("AC-12", "Pages contain concrete, quotable statements",
              pillar=AI_DISCOVERABILITY, category="fact_extraction", status=PASS,
              evidence="All %d content pages contain at least two concrete factual statements "
                       "(figures, dates, prices or definitional sentences)." % len(pages),
              applicable=len(pages))
        return
    r.add("AC-12", "Content pages contain almost nothing concrete to quote",
          pillar=AI_DISCOVERABILITY, category="fact_extraction", status=FAIL,
          impact_base=2, confidence="medium", affected=len(sparse), applicable=len(pages),
          evidence="%d of %d content pages contain at most one concrete factual statement - "
                   "no figures, dates, prices or explicit definitional sentences (%s)."
                   % (len(sparse), len(pages),
                      ", ".join(short(p["url"], ctx["origin"]) for p, _ in sparse[:4])),
          mechanism="Assistants prefer to quote sentences that assert something checkable. "
                    "Pages built entirely from abstract positioning language offer no such "
                    "sentence, so even when the page is retrieved it is summarised away rather "
                    "than cited - the brand is read but never named.",
          evidence_items=[{"url": p["url"], "observed": "%d concrete statements detected" % facts,
                           "expected": "specific figures, dates, capabilities or definitions",
                           "excerpt": page_text(p)[:140]} for p, facts in sparse[:6]],
          affected_urls=[p["url"] for p, _ in sparse],
          remediation={"summary": "Add checkable specifics to the pages that carry only positioning language.",
                       "steps": ["For each page, add 2-3 sentences containing a number, a date, a place, a named standard or an explicit definition ('X is a Y that does Z').",
                                 "State facts as complete sentences rather than as isolated figures in a graphic.",
                                 "Keep the claim and its qualifier in the same sentence so a quote stays accurate out of context."],
                       "expected_benefit": "Gives assistants a sentence worth quoting with attribution.",
                       "effort": "medium"},
          dedupe_key="fact-density", tags=["semantic-review"])


def check_facts_locked_in_media(snap, ctx, r):
    if not guard(ctx, r, "AC-13", "Facts locked in non-text media", "fact_extraction"):
        return
    pages = ctx["html_pages"]
    image_heavy, missing_alt = [], []
    total_content_images = 0
    for p in pages:
        e = ex(p)
        content_images = [i for i in (e.get("images") or [])
                          if i.get("region") == "main" and not i.get("aria_hidden")
                          and (i.get("role") or "") != "presentation"]
        total_content_images += len(content_images)
        # alt=None means the attribute is absent (a defect); alt="" is an
        # explicit declaration that the image is decorative (not a defect).
        undescribed = [i for i in content_images if i.get("alt") is None]
        if undescribed:
            missing_alt.append((p, len(undescribed), len(content_images)))
        if len(content_images) >= 5 and e.get("main_word_count", 0) < 150:
            image_heavy.append((p, len(content_images), e.get("main_word_count", 0)))

    if total_content_images == 0:
        r.add("AC-13", "Facts locked in non-text media", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=NOT_APPLICABLE,
              applicability="No content-area images were found in the sample.")
    elif not missing_alt and not image_heavy:
        r.add("AC-13", "Images carry text alternatives", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=PASS,
              evidence="All %d content-area images across %d pages declare an alt attribute "
                       "(explicitly empty for decorative images)."
                       % (total_content_images, len(pages)),
              applicable=total_content_images, unit="images")
    else:
        affected_pages = {p["url"] for p, _, _ in missing_alt} | {p["url"] for p, _, _ in image_heavy}
        undescribed_total = sum(n for _, n, _ in missing_alt)
        r.add("AC-13", "Page content is carried by images with no text equivalent",
              pillar=AI_DISCOVERABILITY, category="fact_extraction", status=FAIL,
              impact_base=3 if image_heavy else 2,
              confidence="high" if missing_alt else "medium",
              affected=len(affected_pages), applicable=len(pages),
              evidence="%d of %d content-area images across %d page(s) have no alt attribute"
                       "%s."
                       % (undescribed_total, total_content_images, len(missing_alt),
                          "; %d page(s) present five or more images with under 150 words of "
                          "text, so the substance appears to live in the images"
                          % len(image_heavy) if image_heavy else ""),
              mechanism="Text inside an image is invisible to a text crawler and to a screen "
                        "reader. A price list, menu, specification table or opening-hours "
                        "graphic that exists only as an image cannot be extracted, so the "
                        "assistant either omits the fact or takes it from a third-party source "
                        "that may be out of date.",
              evidence_items=([{"url": p["url"],
                                "observed": "%d of %d content images have no alt attribute"
                                            % (n, total),
                                "expected": "alt text describing the image, or alt=\"\" if decorative"}
                               for p, n, total in missing_alt[:5]]
                              + [{"url": p["url"],
                                  "observed": "%d content images, %d words of text" % (imgs, words),
                                  "expected": "the same information available as text"}
                                 for p, imgs, words in image_heavy[:3]]),
              affected_urls=sorted(affected_pages),
              remediation={"summary": "Give every content image a text alternative, and move "
                                      "factual content out of images entirely.",
                           "steps": ["Add `alt` describing what the image conveys; use `alt=\"\"` only for purely decorative images.",
                                     "Where an image contains a price list, menu, specification or hours, publish the same data as HTML text (a table or list) next to or instead of the image.",
                                     "Prefer real HTML tables over screenshots of tables - they are extractable and they reflow on mobile."],
                           "expected_benefit": "Makes facts currently invisible to machines "
                                               "extractable, and fixes the same barrier for "
                                               "screen-reader users.",
                           "effort": "medium"},
              dedupe_key="facts-in-images")

    # Embedded documents and frames.
    doc_links = []
    for p in pages:
        for link in p.get("internal_links", []) + p.get("external_links", []):
            if re.search(r"\.(pdf|docx?|xlsx?|pptx?)($|\?)", link["url"], re.I) \
                    and link.get("region") == "main":
                doc_links.append((p, link))
    framed = [p for p in pages
              if (ex(p).get("iframes") or []) and ex(p).get("main_word_count", 0) < 150]
    if len(doc_links) >= 3 or framed:
        r.add("AC-14", "Primary content is delivered as documents or embedded frames",
              pillar=AI_DISCOVERABILITY, category="fact_extraction", status=FAIL,
              impact_base=2, confidence="medium",
              affected=len({p["url"] for p, _ in doc_links} | {p["url"] for p in framed}),
              applicable=len(pages),
              evidence="%d links to PDF/Office documents were found in main content, and %d "
                       "page(s) rely on an embedded frame with under 150 words of their own text."
                       % (len(doc_links), len(framed)),
              mechanism="Documents and third-party frames are separate resources. Many "
                        "fetchers do not download them, frame content is attributed to the "
                        "other origin, and neither carries your page's headings or links - so "
                        "the facts inside are effectively published somewhere the brand gets "
                        "no credit for.",
              evidence_items=([{"url": p["url"], "observed": "links to %s" % l["url"][:110],
                                "expected": "the same content available as HTML"}
                               for p, l in doc_links[:5]]
                              + [{"url": p["url"],
                                  "observed": "%d iframe(s), %d words of own text"
                                              % (len(ex(p).get("iframes") or []),
                                                 ex(p).get("main_word_count", 0)),
                                  "expected": "the page's substance in its own HTML"}
                                 for p in framed[:3]]),
              affected_urls=sorted({p["url"] for p, _ in doc_links} | {p["url"] for p in framed}),
              remediation={"summary": "Publish an HTML version of anything that carries facts, "
                                      "and keep the document as a download.",
                           "steps": ["For each document that holds pricing, specifications, policies or menus, create an HTML page with the same content and link the file as a secondary download.",
                                     "Where a frame provides the main content (booking, catalogue, docs), add a server-rendered summary with the key facts and links on the page itself."],
                           "expected_benefit": "Moves facts onto crawlable, citable URLs the "
                                               "brand owns.",
                           "effort": "medium"},
              dedupe_key="content-in-documents")
    else:
        r.add("AC-14", "Primary content is delivered as HTML", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=PASS,
              evidence="Main content did not depend on downloadable documents or embedded "
                       "frames (%d document link(s) in main content)." % len(doc_links),
              applicable=len(pages))


def check_tabular_facts(snap, ctx, r):
    """Tables that carry data but no header cells.

    A table is the densest way to state a set of related facts, and the one
    shape an extractor handles best - provided it can tell which column a value
    belongs to. That binding is carried by `<th>`. Without header cells a table
    is an untyped grid: the numbers survive, the meaning does not, so "49" can
    no longer be resolved to "Pro tier, monthly, GBP".

    Only pages that actually contain a table are counted, and only tables large
    enough to be carrying data rather than laying out a page.
    """
    if not guard(ctx, r, "AC-19", "Tabular facts", "fact_extraction"):
        return

    with_tables, unlabelled = [], []
    for page in ctx["html_pages"]:
        e = page.get("extraction") or {}
        tables = int(e.get("tables") or 0)
        if tables < 1:
            continue
        with_tables.append(page)
        if int(e.get("table_headers") or 0) == 0:
            unlabelled.append({"page": page, "tables": tables,
                               "captions": int(e.get("table_captions") or 0)})

    if not with_tables:
        r.add("AC-19", "Tabular facts", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=NOT_APPLICABLE,
              applicability="No sampled page contains a `<table>`, so there is no tabular "
                            "data whose column labelling could be assessed. Facts held in "
                            "prose are covered by AC-12, and facts held in images by AC-13.")
        return

    if not unlabelled:
        r.add("AC-19", "Tables label their columns", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=PASS, applicable=len(with_tables),
              evidence="%d of %d sampled pages contain a table, and every one uses `<th>` "
                       "header cells." % (len(with_tables), len(ctx["html_pages"])))
        return

    total_tables = sum(u["tables"] for u in unlabelled)
    r.add("AC-19", "Tables present data without header cells",
          pillar=AI_DISCOVERABILITY, category="fact_extraction", status=FAIL,
          impact_base=2, confidence="medium",
          affected=len(unlabelled), applicable=len(with_tables),
          evidence="%d of %d sampled pages that contain a table use no `<th>` header "
                   "cells at all (%d table(s) in total; e.g. %s)."
                   % (len(unlabelled), len(with_tables), total_tables,
                      short(unlabelled[0]["page"]["url"], ctx["origin"])),
          mechanism="A value in a table means nothing without its column. `<th>` is what "
                    "binds the two: it tells an extractor that the 49 in this cell is the "
                    "monthly price of the tier named in that row. With only `<td>`, the "
                    "grid is a bag of strings whose relationships exist purely in visual "
                    "position - so a comparison or pricing table, which is exactly the "
                    "content an assistant is most often asked to summarise, is the content "
                    "it is least able to quote correctly. The same markup is what lets a "
                    "screen reader announce the column when reading a cell.",
          evidence_items=[{"url": u["page"]["url"],
                           "observed": "%d table(s), 0 <th> cells, %d <caption>"
                                       % (u["tables"], u["captions"]),
                           "expected": "<th> for every column and row header"}
                          for u in unlabelled[:8]],
          affected_urls=[u["page"]["url"] for u in unlabelled],
          remediation={
              "summary": "Mark up header cells with `<th>` and give each data table a "
                         "`<caption>`; move any layout-only tables to CSS.",
              "steps": ["Replace the `<td>` cells in the top row with `<th scope=\"col\">`, "
                        "and any row-label cells with `<th scope=\"row\">`.",
                        "Wrap them in `<thead>` so the header survives sorting and paging.",
                        "Add a `<caption>` naming what the table shows - it is the only "
                        "part of a table that states its subject in words.",
                        "Where a table exists only to position content, rebuild it with "
                        "CSS grid or flexbox; a layout table competes with real data "
                        "tables for an extractor's attention.",
                        "For a pricing or specification table, also emit the same values "
                        "as `Product`/`Offer` JSON-LD so the facts are typed as well as "
                        "labelled."],
              "expected_benefit": "Turns comparison and pricing grids into rows a machine "
                                  "can read as records, which is the format assistants "
                                  "quote most readily.",
              "effort": "low"},
          dedupe_key="tables-unlabelled")


def check_boilerplate_ratio(snap, ctx, r):
    if not guard(ctx, r, "AC-15", "Unique content ratio", "fact_extraction"):
        return
    pages = [p for p in ctx["html_pages"] if ex(p).get("word_count", 0) >= 80]
    if len(pages) < 2:
        r.add("AC-15", "Unique content ratio", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=NOT_APPLICABLE,
              applicability="Fewer than two pages carried enough text to compare unique "
                            "content against site furniture.")
        return
    heavy = []
    for p in pages:
        e = ex(p)
        total = max(1, e.get("word_count", 0))
        main = e.get("main_word_count", 0)
        if main / total < 0.35:
            heavy.append((p, main, total))
    if not heavy:
        r.add("AC-15", "Unique content dominates site furniture", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=PASS,
              evidence="On all %d content pages, at least 35%% of the page text is unique "
                       "content rather than navigation and footer." % len(pages),
              applicable=len(pages))
        return
    r.add("AC-15", "Navigation and footer text outweighs the page's own content",
          pillar=AI_DISCOVERABILITY, category="fact_extraction", status=FAIL,
          impact_base=2, confidence="medium", affected=len(heavy), applicable=len(pages),
          evidence="On %d of %d content pages, under 35%% of the text is the page's own "
                   "content (e.g. %s: %d unique of %d total words)."
                   % (len(heavy), len(pages), short(heavy[0][0]["url"], ctx["origin"]),
                      heavy[0][1], heavy[0][2]),
          mechanism="Near-duplicate detection compares whole documents. When most of every "
                    "page is the same menu and footer, pages look alike to a retrieval system "
                    "and get collapsed; the small unique portion is also diluted in relevance "
                    "scoring, so the page ranks for nothing in particular.",
          evidence_items=[{"url": p["url"],
                           "observed": "%d unique words of %d total (%.0f%%)"
                                       % (main, total, 100.0 * main / max(1, total)),
                           "expected": "the page's own content dominating the document"}
                          for p, main, total in heavy[:6]],
          affected_urls=[p["url"] for p, _, _ in heavy],
          remediation={"summary": "Increase unique content per page and mark furniture semantically.",
                       "steps": ["Wrap the unique content in `<main>` and the repeated blocks in `<nav>`/`<footer>` so extractors can separate them.",
                                 "Trim mega-menus and footer link farms rendered on every page.",
                                 "Add substantive page-specific copy to the thinnest templates."],
                       "expected_benefit": "Pages stop looking like duplicates of each other.",
                       "effort": "medium"},
          dedupe_key="boilerplate-ratio")


def check_social_preview_metadata(snap, ctx, r):
    if not guard(ctx, r, "AC-16", "Preview metadata", "page_metadata"):
        return
    pages = ctx["html_pages"]
    missing = []
    for p in pages:
        props = ex(p).get("meta_property") or {}
        names = ex(p).get("meta_name") or {}
        has_og = bool(props.get("og:title") and (props.get("og:description") or names.get("description")))
        if not has_og:
            missing.append(p)
    if not missing:
        r.add("AC-16", "Pages provide preview metadata", pillar=AI_DISCOVERABILITY,
              category="page_metadata", status=PASS,
              evidence="%d of %d sampled pages declare Open Graph title and description."
                       % (len(pages), len(pages)), applicable=len(pages))
        return
    r.add("AC-16", "Pages provide no preview metadata", pillar=AI_DISCOVERABILITY,
          category="page_metadata", status=FAIL, impact_base=1, confidence="high",
          affected=len(missing), applicable=len(pages),
          evidence="%d of %d sampled pages declare no Open Graph title/description pair."
                   % (len(missing), len(pages)),
          mechanism="Open Graph is the summary layer that chat clients, messaging apps, "
                    "social platforms and several assistant surfaces read when they render a "
                    "link to your page. Without it they fall back to scraping, which produces "
                    "an unbranded or truncated card - so a link that was shared for you lands "
                    "looking untrustworthy.",
          evidence_items=[{"url": p["url"], "observed": "no og:title/og:description",
                           "expected": "og:title, og:description, og:image, og:url"}
                          for p in missing[:6]],
          affected_urls=[p["url"] for p in missing],
          remediation={"summary": "Emit Open Graph tags from the same fields as the title and description.",
                       "steps": ["Add `og:title`, `og:description`, `og:url` (canonical) and `og:type` to the base template.",
                                 "Add `og:image` at 1200x630 with an absolute URL; add `twitter:card=summary_large_image`.",
                                 "Derive the values from the page data so they never drift from the visible content."],
                       "expected_benefit": "Controls how shared links appear wherever the brand "
                                           "is passed around.",
                       "effort": "low"},
          dedupe_key="og-metadata")


def check_authorship(snap, ctx, r):
    if not guard(ctx, r, "AC-17", "Authorship and attribution", "fact_extraction"):
        return
    articles = role_pages(ctx["html_pages"], "article")
    if not articles:
        r.add("AC-17", "Authorship and attribution", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=NOT_APPLICABLE,
              applicability="No sampled page qualified as editorial content (long-form text "
                            "under an editorial path with a date or byline), so author "
                            "attribution is not expected.")
        return
    unattributed = []
    for p in articles:
        text = page_text(p)[:1200]
        has_byline = bool(_BYLINE_RE.search(text))
        has_schema_author = any(n.get("author") for n in p["_jsonld"])
        if not has_byline and not has_schema_author:
            unattributed.append(p)
    if not unattributed:
        r.add("AC-17", "Editorial content is attributed", pillar=AI_DISCOVERABILITY,
              category="fact_extraction", status=PASS,
              evidence="%d of %d editorial pages carry a visible byline or a structured author."
                       % (len(articles), len(articles)), applicable=len(articles))
        return
    r.add("AC-17", "Editorial content has no identifiable author or publisher",
          pillar=AI_DISCOVERABILITY, category="fact_extraction", status=FAIL,
          impact_base=2, confidence="medium", affected=len(unattributed), applicable=len(articles),
          evidence="%d of %d editorial pages state no author, either in visible text or in "
                   "structured data (%s)."
                   % (len(unattributed), len(articles),
                      ", ".join(short(p["url"], ctx["origin"]) for p in unattributed[:4])),
          mechanism="Attribution is one of the few signals a retrieval system has for whether "
                    "advice is accountable. Anonymous guidance competes badly against "
                    "attributed guidance on the same topic, and it gives an assistant nothing "
                    "to name when it explains where an answer came from.",
          evidence_items=[{"url": p["url"], "observed": "no byline and no schema author",
                           "expected": "a named author with a bio page, plus schema `author`"}
                          for p in unattributed[:6]],
          affected_urls=[p["url"] for p in unattributed],
          remediation={"summary": "Attribute editorial content to a named person or the organisation.",
                       "steps": ["Add a visible byline linking to an author page that states the author's relevant background.",
                                 "Mirror it in JSON-LD as `author` (a `Person` with `@id` and `url`), and set `publisher` to the site's Organization node.",
                                 "Where content is genuinely corporate, attribute it explicitly to the organisation rather than leaving it blank."],
                       "expected_benefit": "Converts anonymous pages into accountable, citable sources.",
                       "effort": "low"},
          dedupe_key="authorship")


def check_identity_corroboration_links(snap, ctx, r):
    if not guard(ctx, r, "AC-18", "External identity links", "entity_identity"):
        return
    hosts = as_dict(as_dict(snap.get("link_graph")).get("external_hosts"))
    found = sorted({h for h in hosts for known in IDENTITY_HOSTS if h.endswith(known)})
    same_as = []
    for p in ctx["html_pages"]:
        for node in p["_jsonld"]:
            value = node.get("sameAs")
            if isinstance(value, str):
                same_as.append(value)
            elif isinstance(value, list):
                same_as.extend([v for v in value if isinstance(v, str)])
    if found or same_as:
        r.add("AC-18", "The site links itself to external identity profiles",
              pillar=AI_DISCOVERABILITY, category="entity_identity", status=PASS,
              evidence="%d third-party identity host(s) linked (%s) and %d sameAs reference(s) "
                       "declared." % (len(found), ", ".join(found[:6]) or "none", len(same_as)),
              applicable=1, unit="sites")
        return
    r.add("AC-18", "The site links to no external profile that could corroborate it",
          pillar=AI_DISCOVERABILITY, category="entity_identity", status=FAIL,
          impact_base=2, confidence="medium", affected=1, applicable=1, unit="sites",
          evidence="No outbound links to recognised identity or profile hosts were found "
                   "across %d sampled pages, and no `sameAs` references are declared. "
                   "External hosts linked: %s."
                   % (len(ctx["html_pages"]), ", ".join(sorted(hosts)[:6]) or "none"),
          mechanism="A claim that exists in exactly one place is fragile. Machines weight a "
                    "fact by how many independent sources agree on it, and they resolve "
                    "identity by following links between a site and profiles that already "
                    "exist in a knowledge graph. A site with no outbound identity links gives "
                    "the graph nothing to join on, so it stays an isolated, low-confidence "
                    "entity - and any similarly-named organisation with profiles will "
                    "outrank it as the referent.",
          evidence_items=[{"url": ctx["entry"]["url"] if ctx["entry"] else "",
                           "observed": "no links to profile/registry hosts; no sameAs",
                           "expected": "links to the profiles that describe this same entity"}],
          remediation={"summary": "Link out to the profiles that describe the same entity, and "
                                  "mirror them in `sameAs`.",
                       "steps": ["Claim and complete the profiles that apply to your category (company registry, LinkedIn, industry directories, app stores, review platforms, code hosts, professional bodies).",
                                 "Link to them from the site (footer or an about page) *and* list them in the Organization `sameAs` array.",
                                 "Make the name, description and URL identical across all of them so the records reinforce rather than fragment.",
                                 "Prefer a small number of authoritative profiles over many low-quality directory listings."],
                       "expected_benefit": "Turns a single unverified claim into a corroborated "
                                           "entity that assistants can resolve confidently.",
                       "effort": "medium"},
          dedupe_key="no-external-identity")


CHECKS = [
    check_structured_data_presence,
    check_malformed_jsonld,
    check_site_identity,
    check_entity_name_consistency,
    check_role_specific_schema,
    check_structured_data_matches_page,
    check_jsonld_identity_urls,
    check_value_proposition,
    check_fact_density,
    check_facts_locked_in_media,
    check_tabular_facts,
    check_boilerplate_ratio,
    check_social_preview_metadata,
    check_authorship,
    check_identity_corroboration_links,
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
