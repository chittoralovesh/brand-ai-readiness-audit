#!/usr/bin/env python3
"""Is this content current, self-consistent, and corroborated anywhere else?

Reads a site snapshot produced by `audit-orchestrator` and writes a check-result
document to stdout (or `--out`). Standard library only; no network access.

    python check_freshness.py --snapshot snapshot.json \
        [--research research.json] --out freshness.json

Two failure modes live here, and they are related.

*Staleness* - a page with no date, or with dates that contradict each other, is
treated as being of unknown age. Retrieval systems discount unknown-age content
for anything time-sensitive, so a current page loses to a dated competitor.

*Uncorroborated identity* - a fact stated in exactly one place is fragile, and a
brand that shares its name with anything else gets merged with it. The on-site
half of that (disambiguating attributes, conflicting contact details) is
deterministic and always runs. The off-site half needs a web search tool; when
one is unavailable this skill reports NOT_EVALUATED and says so in coverage. It
never invents off-site evidence, and it never treats absence from a small search
sample as proof that a brand is unindexed.

`--research` accepts an optional JSON document produced by the orchestrator's
agent-side research pass. See references/offsite-research.md for its schema.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

PRODUCER = "freshness-corroboration"
SCHEMA_VERSION = "1.0"

PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_EVALUATED = "NOT_EVALUATED"
INCONCLUSIVE = "INCONCLUSIVE"

AI_DISCOVERABILITY = "ai_discoverability"
ENGAGEMENT = "on_site_engagement"

STALE_MONTHS = 24          # editorial silence beyond this is reported
COPYRIGHT_LAG_YEARS = 2    # a copyright year this far behind is reported


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


def load_json(path):
    if path in ("-", "", None):
        return json.loads(sys.stdin.read())
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
_YEAR_RE = re.compile(r"\b(19[89]\d|20\d{2})\b")
_COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)\s*(?:[^\d]{0,20})?(\d{4})(?:\s*[-–—]\s*(\d{4}))?", re.I)
_MONTH_NAME_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})"
    r"|\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{4})", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{3,4}[\s.\-]\d{3,4}(?:[\s.\-]\d{2,4})?")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PLACEHOLDER_RE = re.compile(
    r"\b(lorem ipsum|dolor sit amet|coming soon|under construction|"
    r"placeholder text|your text here|insert (?:text|content) here|"
    r"sample text|todo:|tbd\b|xxx+\b|test page)\b", re.I)
_IDENTIFIER_RE = re.compile(
    r"\b(?:vat|gst|abn|acn|ein|siret|siren|ustid|company (?:number|no\.?|reg)|"
    r"registration (?:number|no\.?)|charity (?:number|no\.?)|"
    r"registered in|incorporated in|founded in|established in|since)\b", re.I)


def parse_date(value):
    """Best-effort ISO/date parse. Returns a date or None - never raises."""
    if not value or not isinstance(value, str):
        return None
    m = _ISO_RE.search(value)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc).date()
        except ValueError:
            return None
    m = _MONTH_NAME_RE.search(value)
    if m:
        try:
            if m.group(1):
                return datetime(int(m.group(3)), _MONTHS[m.group(1).lower()[:3]],
                                int(m.group(2)), tzinfo=timezone.utc).date()
            return datetime(int(m.group(6)), _MONTHS[m.group(5).lower()[:3]],
                            int(m.group(4)), tzinfo=timezone.utc).date()
        except (ValueError, KeyError):
            return None
    return None


def ex(page):
    return page.get("extraction") or {}


def short(url, origin):
    if origin and url.startswith(origin):
        return url[len(origin):] or "/"
    return url


def page_text(page):
    return (ex(page).get("main_text_excerpt") or ex(page).get("text_excerpt") or "")


def flatten_jsonld(node, out, depth=0):
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


def page_dates(page):
    """Every date signal on a page, tagged with where it came from."""
    signals = []
    e = ex(page)
    nodes = []
    for raw in e.get("jsonld_blocks") or []:
        try:
            flatten_jsonld(json.loads(raw), nodes)
        except Exception:
            continue
    for node in nodes:
        for field in ("datePublished", "dateModified", "dateCreated", "uploadDate"):
            value = node.get(field)
            if isinstance(value, str):
                parsed = parse_date(value)
                if parsed:
                    signals.append((field, parsed, value, "json-ld"))
    for prop in ("article:published_time", "article:modified_time", "og:updated_time"):
        value = (e.get("meta_property") or {}).get(prop)
        parsed = parse_date(value or "")
        if parsed:
            signals.append((prop, parsed, value, "meta"))
    for element in e.get("time_elements") or []:
        parsed = parse_date(element.get("datetime") or "") or parse_date(element.get("text") or "")
        if parsed:
            signals.append(("time element", parsed, element.get("datetime")
                            or element.get("text"), "html"))
    header = (page.get("headers") or {}).get("last-modified")
    if header:
        m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", header)
        if m:
            try:
                signals.append(("Last-Modified header",
                                datetime(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]],
                                         int(m.group(1)), tzinfo=timezone.utc).date(),
                                header, "header"))
            except (ValueError, KeyError):
                pass
    body_date = parse_date(page_text(page)[:900])
    if body_date:
        signals.append(("visible text", body_date, "", "text"))
    return signals


# Fields that assert *when this page was published or changed*. A bare <time>
# element or a date in prose usually says when something else happens - an
# upcoming event, a conference, a deadline - so it is evidence that a date
# exists, but not evidence about the page's own age. Conflating the two made
# every site with an events list look like it had dates set in the future.
PUBLICATION_FIELDS = frozenset({
    "datepublished", "datemodified", "datecreated", "uploaddate",
    "article:published_time", "article:modified_time", "og:updated_time",
    "last-modified header",
})


def publication_dates(page):
    """Only the date signals that describe this page's own age."""
    return [(field, when, raw, src) for field, when, raw, src in page.get("_dates", [])
            if field.lower() in PUBLICATION_FIELDS]


def infer_editorial(page):
    """Pages whose value depends on being current."""
    path = urlsplit(page["url"].lower()).path
    e = ex(page)
    editorial_path = re.search(
        r"/(blog|news|article|articles|post|posts|insights|stories|press|newsroom|"
        r"research|updates|events|journal|release|changelog|announcements)(/|$)", path)
    return bool(editorial_path) and e.get("main_word_count", 0) >= 150


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
        p["_dates"] = page_dates(p)
        p["_editorial"] = infer_editorial(p)
    return {"pages": pages, "html_pages": html_pages,
            "entry": next((p for p in html_pages if p.get("depth") == 0), None),
            "origin": snapshot.get("origin") or "", "site": snapshot.get("site") or "",
            "today": datetime.now(timezone.utc).date()}


def guard(ctx, r, check_id, title, category="freshness"):
    if ctx["html_pages"]:
        return True
    r.add(check_id, title, pillar=AI_DISCOVERABILITY, category=category,
          status=NOT_EVALUATED,
          applicability="No HTML page was successfully parsed, so freshness signals could "
                        "not be read.")
    return False


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def check_dates_present(snap, ctx, r):
    if not guard(ctx, r, "FC-01", "Machine-readable dates on time-sensitive pages"):
        return
    editorial = [p for p in ctx["html_pages"] if p["_editorial"]]
    if not editorial:
        r.add("FC-01", "Machine-readable dates on time-sensitive pages",
              pillar=AI_DISCOVERABILITY, category="freshness", status=NOT_APPLICABLE,
              applicability="No sampled page was time-sensitive editorial content "
                            "(substantial text under a blog/news/press/updates path), so a "
                            "publication date is not expected.")
        return
    undated = [p for p in editorial
               if not any(src in ("json-ld", "meta", "html") for _, _, _, src in p["_dates"])]
    if not undated:
        r.add("FC-01", "Time-sensitive pages carry machine-readable dates",
              pillar=AI_DISCOVERABILITY, category="freshness", status=PASS,
              evidence="%d of %d editorial pages expose a date in structured data, meta tags "
                       "or a <time> element." % (len(editorial), len(editorial)),
              applicable=len(editorial))
        return
    r.add("FC-01", "Time-sensitive pages publish no machine-readable date",
          pillar=AI_DISCOVERABILITY, category="freshness", status=FAIL,
          impact_base=3, confidence="high", affected=len(undated), applicable=len(editorial),
          evidence="%d of %d editorial pages expose no date a machine can read - no "
                   "`datePublished`/`dateModified` in structured data, no "
                   "`article:published_time`, no `<time datetime>` (%s)."
                   % (len(undated), len(editorial),
                      ", ".join(short(p["url"], ctx["origin"]) for p in undated[:4])),
          mechanism="Recency is one of the strongest ranking and selection signals for "
                    "anything that could go out of date. A page with no parseable date is "
                    "scored as being of unknown age, which is treated as worse than old: it "
                    "loses to a competitor's dated page even when yours is newer and better. "
                    "A date rendered only as prose inside a paragraph is frequently missed.",
          evidence_items=[{"url": p["url"],
                           "observed": "no machine-readable date (%d date signal(s) of any kind)"
                                       % len(p["_dates"]),
                           "expected": "datePublished and dateModified in ISO-8601"}
                          for p in undated[:6]],
          affected_urls=[p["url"] for p in undated],
          remediation={"summary": "Publish both a publication and a modification date in "
                                  "machine-readable form on every editorial page.",
                       "steps": ["Add `datePublished` and `dateModified` to the page's Article/BlogPosting JSON-LD, in ISO-8601 with a timezone offset.",
                                 "Render the visible date inside `<time datetime=\"YYYY-MM-DD\">` so the human-readable and machine-readable values cannot drift.",
                                 "Update `dateModified` only for substantive edits - refreshing it on every deploy destroys the signal's meaning.",
                                 "Mirror the value in the sitemap `<lastmod>`."],
                       "expected_benefit": "Lets recency work in the site's favour instead of "
                                           "being unknown.",
                       "effort": "low"},
          dedupe_key="dates-missing")


def check_content_staleness(snap, ctx, r):
    if not guard(ctx, r, "FC-02", "Content recency"):
        return
    editorial = [p for p in ctx["html_pages"] if p["_editorial"]]
    sitemap_lastmods = []
    for entry in as_list(snap.get("sitemap_urls")):
        if not isinstance(entry, dict):
            continue
        parsed = parse_date(entry.get("lastmod") or "")
        if parsed:
            sitemap_lastmods.append(parsed)
    page_latest = [max(d for _, d, _, _ in publication_dates(p))
                   for p in ctx["html_pages"] if publication_dates(p)]
    observed = page_latest + sitemap_lastmods
    if not observed:
        r.add("FC-02", "Content recency", pillar=AI_DISCOVERABILITY, category="freshness",
              status=INCONCLUSIVE,
              applicability="No publication or modification date was observed across %d "
                            "sampled pages or the sitemap, so the age of the content cannot "
                            "be established. Dates that merely appear on a page (event "
                            "listings, for example) are deliberately not used here. The "
                            "absence of publication dates is reported by FC-01."
                            % len(ctx["html_pages"]))
        return
    today = ctx["today"]
    newest = max(observed)
    future = [d for d in observed if (d - today).days > 2]
    months = (today.year - newest.year) * 12 + (today.month - newest.month)
    if future:
        r.add("FC-03", "Dates are set in the future", pillar=AI_DISCOVERABILITY,
              category="freshness", status=FAIL, impact_base=2, confidence="high",
              affected=len(future), applicable=len(observed), unit="date signals",
              evidence="%d of %d observed publication/modification dates are in the future "
                       "(latest: %s, today is %s)."
                       % (len(future), len(observed), max(future).isoformat(),
                          today.isoformat()),
              mechanism="Future dates are treated as unreliable and are commonly ignored "
                        "altogether, which silently removes the recency signal the site was "
                        "trying to publish. They also break sorting in feeds and archives.",
              remediation={"summary": "Correct the date source producing future timestamps.",
                           "steps": ["Check for a timezone or locale bug (for example day/month transposition) in the publishing pipeline.",
                                     "Ensure scheduled-but-unpublished content is not rendered publicly with its future date."],
                           "expected_benefit": "Restores a usable recency signal.",
                           "effort": "low"},
              dedupe_key="future-dates")
    if months >= STALE_MONTHS and editorial:
        r.add("FC-02", "The most recent dated content is years old", pillar=AI_DISCOVERABILITY,
              category="freshness", status=FAIL, impact_base=2, confidence="medium",
              affected=1, applicable=1, unit="sites",
              evidence="The newest date observed anywhere in the sample (%d pages plus %d "
                       "sitemap lastmod values) is %s - about %d months ago."
                       % (len(ctx["html_pages"]), len(sitemap_lastmods),
                          newest.isoformat(), months),
              mechanism="Assistants prefer recent sources for anything that can change, and "
                        "an entire site whose newest signal is years old reads as abandoned. "
                        "That reduces both the chance of being selected as a source and the "
                        "confidence attached to facts taken from it - including facts that are "
                        "still perfectly true.",
              evidence_items=[{"url": ctx["origin"], "observed": "newest date %s" % newest.isoformat(),
                               "expected": "at least one substantive update within ~%d months"
                                           % STALE_MONTHS}],
              remediation={"summary": "Refresh and re-date the highest-value evergreen pages "
                                      "rather than publishing filler.",
                           "steps": ["Pick the pages that answer recurring questions about the offering and review them for accuracy.",
                                     "Where you make a substantive change, update `dateModified` and the visible date; where nothing changed, leave the date alone.",
                                     "Add a short, dated 'what changed' note so the update is verifiable rather than cosmetic."],
                           "expected_benefit": "Restores recency without diluting the site with "
                                               "low-value posts.",
                           "effort": "medium"},
              dedupe_key="content-stale")
    elif editorial:
        r.add("FC-02", "Content shows recent activity", pillar=AI_DISCOVERABILITY,
              category="freshness", status=PASS,
              evidence="The newest observed date is %s (about %d months ago)."
                       % (newest.isoformat(), max(0, months)), applicable=1, unit="sites")
    else:
        r.add("FC-02", "Content recency", pillar=AI_DISCOVERABILITY, category="freshness",
              status=NOT_APPLICABLE,
              applicability="The site has no time-sensitive editorial section in the sample, "
                            "so an old newest-date is not by itself a defect. Newest observed "
                            "date: %s." % newest.isoformat())


def check_date_consistency(snap, ctx, r):
    if not guard(ctx, r, "FC-04", "Date consistency"):
        return
    dated = [p for p in ctx["html_pages"] if len(p["_dates"]) >= 2]
    if not dated:
        r.add("FC-04", "Date consistency", pillar=AI_DISCOVERABILITY, category="freshness",
              status=NOT_APPLICABLE,
              applicability="No page carried two or more independent date signals, so they "
                            "cannot disagree.")
        return
    conflicts = []
    for p in dated:
        published = [d for f, d, _, _ in p["_dates"] if "publish" in f.lower()]
        modified = [d for f, d, _, _ in p["_dates"] if "modif" in f.lower()]
        if published and modified and min(modified) < max(published):
            conflicts.append((p, "dateModified %s precedes datePublished %s"
                              % (min(modified).isoformat(), max(published).isoformat())))
            continue
        # Comparing a structured date against a visible one is only meaningful on
        # editorial pages. Elsewhere a <time> element usually marks an event, not
        # the page's own date, and the "mismatch" would be manufactured.
        machine = [d for f, d, _, src in publication_dates(p) if src in ("json-ld", "meta")]
        visible = [d for f, d, _, src in p["_dates"] if src in ("html", "text")]
        if machine and visible and p.get("_editorial"):
            gap = abs((max(machine) - max(visible)).days)
            if gap > 45:
                conflicts.append((p, "structured date %s differs from the visible date %s by "
                                  "%d days" % (max(machine).isoformat(),
                                               max(visible).isoformat(), gap)))
    if not conflicts:
        r.add("FC-04", "Date signals agree with each other", pillar=AI_DISCOVERABILITY,
              category="freshness", status=PASS,
              evidence="On all %d pages carrying multiple date signals, the machine-readable "
                       "and visible dates agree." % len(dated), applicable=len(dated))
        return
    r.add("FC-04", "A page's dates contradict each other", pillar=AI_DISCOVERABILITY,
          category="freshness", status=FAIL, impact_base=2, confidence="high",
          affected=len(conflicts), applicable=len(dated),
          evidence="%d of %d pages with multiple date signals contain a contradiction (e.g. %s: %s)."
                   % (len(conflicts), len(dated), short(conflicts[0][0]["url"], ctx["origin"]),
                      conflicts[0][1]),
          mechanism="Contradictory dates make every date on the page untrustworthy. The usual "
                    "outcome is that all of them are discarded, so the page falls back to "
                    "unknown-age - the same position as publishing no date at all, but harder "
                    "to notice.",
          evidence_items=[{"url": p["url"], "observed": reason,
                           "expected": "one consistent publication and modification date"}
                          for p, reason in conflicts[:6]],
          affected_urls=[p["url"] for p, _ in conflicts],
          remediation={"summary": "Render every date on a page from the same field.",
                       "steps": ["Render the visible date from the same value that populates the JSON-LD.",
                                 "Ensure `dateModified` is never earlier than `datePublished`.",
                                 "Normalise everything to ISO-8601 with an explicit offset."],
                       "expected_benefit": "One trustworthy recency signal instead of several "
                                           "conflicting ones.",
                       "effort": "low"},
          dedupe_key="date-conflict")


def check_copyright_year(snap, ctx, r):
    if not guard(ctx, r, "FC-05", "Copyright year currency"):
        return
    today = ctx["today"]
    stale = []
    seen_any = False
    for p in ctx["html_pages"]:
        m = _COPYRIGHT_RE.search(ex(p).get("text_excerpt") or "")
        if not m:
            continue
        seen_any = True
        year = int(m.group(2) or m.group(1))
        if today.year - year >= COPYRIGHT_LAG_YEARS:
            stale.append((p, year, m.group(0)[:80]))
    if not seen_any:
        r.add("FC-05", "Copyright year currency", pillar=ENGAGEMENT, category="freshness",
              status=NOT_APPLICABLE,
              applicability="No copyright line was found in the sampled pages.")
        return
    if not stale:
        r.add("FC-05", "The copyright line is current", pillar=ENGAGEMENT,
              category="freshness", status=PASS,
              evidence="Copyright years found are within %d year(s) of %d."
                       % (COPYRIGHT_LAG_YEARS, today.year),
              applicable=len(ctx["html_pages"]))
        return
    r.add("FC-05", "The footer copyright year is years out of date", pillar=ENGAGEMENT,
          category="freshness", status=FAIL, impact_base=1, confidence="high",
          affected=len(stale), applicable=len(ctx["html_pages"]),
          evidence="%d of %d sampled pages show a copyright year of %d or earlier (current "
                   "year: %d)." % (len(stale), len(ctx["html_pages"]),
                                   max(y for _, y, _ in stale), today.year),
          mechanism="The copyright line is the cheapest signal a visitor uses to judge whether "
                    "a site is still operating. A year several behind reads as abandoned and "
                    "undermines trust in everything else on the page - including prices and "
                    "contact details that are actually correct.",
          evidence_items=[{"url": p["url"], "observed": "copyright year %d" % year,
                           "expected": "the current year, rendered dynamically",
                           "excerpt": excerpt}
                          for p, year, excerpt in stale[:6]],
          affected_urls=[p["url"] for p, _, _ in stale],
          remediation={"summary": "Render the copyright year from the server clock.",
                       "steps": ["Replace the hard-coded year in the footer template with the current year at render or build time.",
                                 "If you display a range, keep the start year fixed and generate the end year."],
                       "expected_benefit": "Removes a visible, misleading abandonment signal.",
                       "effort": "low"},
          dedupe_key="stale-copyright")


def check_placeholder_content(snap, ctx, r):
    if not guard(ctx, r, "FC-06", "Placeholder content"):
        return
    hits = []
    for p in ctx["html_pages"]:
        text = (ex(p).get("text_excerpt") or "")
        match = _PLACEHOLDER_RE.search(text)
        if match:
            start = max(0, match.start() - 40)
            hits.append((p, match.group(0), text[start:match.end() + 60]))
    if not hits:
        r.add("FC-06", "No placeholder or unfinished content found", pillar=ENGAGEMENT,
              category="freshness", status=PASS,
              evidence="0 of %d sampled pages contain placeholder markers."
                       % len(ctx["html_pages"]), applicable=len(ctx["html_pages"]))
        return
    r.add("FC-06", "Pages still contain placeholder or unfinished content",
          pillar=ENGAGEMENT, category="freshness", status=FAIL,
          impact_base=2, confidence="high", affected=len(hits), applicable=len(ctx["html_pages"]),
          evidence="%d of %d sampled pages contain placeholder text or an unfinished-page "
                   "marker (e.g. %r on %s)."
                   % (len(hits), len(ctx["html_pages"]), hits[0][1],
                      short(hits[0][0]["url"], ctx["origin"])),
          mechanism="Placeholder copy is indexed and quoted like any other text, so it can "
                    "surface in an answer attributed to the brand. For a visitor it is an "
                    "unambiguous signal that the site is unfinished, which suppresses any "
                    "action that requires trust.",
          evidence_items=[{"url": p["url"], "observed": "placeholder marker %r" % marker,
                           "expected": "finished content", "excerpt": excerpt[:140]}
                          for p, marker, excerpt in hits[:6]],
          affected_urls=[p["url"] for p, _, _ in hits],
          remediation={"summary": "Replace or unpublish the placeholder pages.",
                       "steps": ["Replace the placeholder copy with real content, or return 404/410 for the page and remove it from the sitemap and navigation until it is ready.",
                                 "Add a pre-publish check that fails the build on placeholder markers."],
                       "expected_benefit": "Stops unfinished text being indexed and quoted as "
                                           "the brand's own words.",
                       "effort": "low"},
          dedupe_key="placeholder-content")


def check_sitemap_lastmod(snap, ctx, r):
    sitemaps = [s for s in as_list(snap.get("sitemaps"))
                if isinstance(s, dict) and s.get("ok") and s.get("url_count")]
    if not sitemaps:
        r.add("FC-07", "Sitemap freshness signals", pillar=AI_DISCOVERABILITY,
              category="freshness", status=NOT_APPLICABLE,
              applicability="No usable sitemap was retrieved; its absence is reported by the "
                            "crawl-access checks.")
        return
    total_urls = sum(s["url_count"] for s in sitemaps)
    total_lastmod = sum(s.get("lastmod_count", 0) for s in sitemaps)
    identical = [s for s in sitemaps
                 if s.get("lastmod_count", 0) > 3
                 and s.get("newest_lastmod") == s.get("oldest_lastmod")]
    if total_lastmod == 0:
        r.add("FC-07", "The sitemap carries no last-modified information",
              pillar=AI_DISCOVERABILITY, category="freshness", status=FAIL,
              impact_base=1, confidence="high", affected=total_urls, applicable=total_urls,
              unit="sitemap URLs",
              evidence="0 of %d sitemap entries include a `<lastmod>` value." % total_urls,
              mechanism="`lastmod` is how a crawler decides which URLs are worth re-fetching. "
                        "Without it, updates are found on the crawler's own schedule rather "
                        "than yours, so a corrected price or a new page can take days or weeks "
                        "to be reflected in answers.",
              remediation={"summary": "Emit an accurate `<lastmod>` per URL.",
                           "steps": ["Set `<lastmod>` from the content's real modification timestamp.",
                                     "Do not stamp every URL with the build time - a sitemap where everything changed at once is ignored."],
                           "expected_benefit": "Faster propagation of updates.",
                           "effort": "low"},
              dedupe_key="sitemap-lastmod-missing")
        return
    if identical:
        r.add("FC-07", "Every sitemap entry claims the same modification time",
              pillar=AI_DISCOVERABILITY, category="freshness", status=FAIL,
              impact_base=1, confidence="high",
              affected=sum(s.get("lastmod_count", 0) for s in identical), applicable=total_urls,
              unit="sitemap URLs",
              evidence="%d sitemap entries all carry the identical lastmod %s."
                       % (sum(s.get("lastmod_count", 0) for s in identical),
                          identical[0].get("newest_lastmod")),
              mechanism="Identical timestamps across every URL is the signature of a build "
                        "timestamp rather than a content timestamp. Crawlers learn to ignore "
                        "the field, which removes the freshness signal for the pages that "
                        "genuinely did change.",
              remediation={"summary": "Derive `lastmod` from content, not from the deploy.",
                           "steps": ["Store a per-record updated_at and emit it in the sitemap.",
                                     "Leave `lastmod` unchanged when a deploy does not change the page."],
                           "expected_benefit": "A `lastmod` value crawlers will act on.",
                           "effort": "low"},
              dedupe_key="sitemap-lastmod-uniform")
        return
    r.add("FC-07", "The sitemap carries per-URL freshness signals", pillar=AI_DISCOVERABILITY,
          category="freshness", status=PASS,
          evidence="%d of %d sitemap entries include a `<lastmod>` value (newest %s)."
                   % (total_lastmod, total_urls, max(s.get("newest_lastmod", "")
                                                     for s in sitemaps)),
          applicable=total_urls, unit="sitemap URLs")


# ---------------------------------------------------------------------------
# Consistency and corroboration
# ---------------------------------------------------------------------------

def _normalise_phone(value):
    digits = re.sub(r"[^\d]", "", value or "")
    return digits[-9:] if len(digits) >= 9 else ""


def check_contact_consistency(snap, ctx, r):
    if not guard(ctx, r, "FC-08", "Contact detail consistency", "consistency"):
        return
    site_wide, page_level = {}, {}
    for p in ctx["html_pages"]:
        nav = ex(p).get("nav_text_excerpt") or ""
        main = ex(p).get("main_text_excerpt") or ""
        for phone in {_normalise_phone(v) for v in _PHONE_RE.findall(nav)} - {""}:
            site_wide.setdefault(phone, set()).add(p["url"])
        for phone in {_normalise_phone(v) for v in _PHONE_RE.findall(main)} - {""}:
            page_level.setdefault(phone, set()).add(p["url"])
    emails = {}
    for p in ctx["html_pages"]:
        for email in set(_EMAIL_RE.findall(ex(p).get("text_excerpt") or "")):
            emails.setdefault(email.lower(), set()).add(p["url"])

    contact_pages = [p for p in ctx["html_pages"]
                     if re.search(r"/(contact|kontakt|contacto|contatti|about)", p["url"], re.I)]
    if not site_wide or not contact_pages:
        r.add("FC-08", "Contact detail consistency", pillar=ENGAGEMENT,
              category="consistency", status=NOT_APPLICABLE,
              applicability="A site-wide phone number in the header/footer and a dedicated "
                            "contact/about page are both required to compare them; "
                            "%d site-wide number(s) and %d contact page(s) were found."
                            % (len(site_wide), len(contact_pages)))
        return
    contact_numbers = set()
    for p in contact_pages:
        contact_numbers |= {_normalise_phone(v)
                            for v in _PHONE_RE.findall(ex(p).get("main_text_excerpt") or "")} - {""}
    if not contact_numbers:
        r.add("FC-08", "Contact detail consistency", pillar=ENGAGEMENT,
              category="consistency", status=INCONCLUSIVE,
              applicability="No phone number was detected in the main content of the contact "
                            "page, so it cannot be compared with the site-wide number.")
        return
    if set(site_wide) & contact_numbers:
        r.add("FC-08", "Contact details agree across the site", pillar=ENGAGEMENT,
              category="consistency", status=PASS,
              evidence="The site-wide phone number also appears on the contact page "
                       "(%d distinct number(s) site-wide, %d on contact pages)."
                       % (len(site_wide), len(contact_numbers)),
              applicable=len(ctx["html_pages"]))
        return
    r.add("FC-08", "The site-wide contact number differs from the contact page",
          pillar=ENGAGEMENT, category="consistency", status=FAIL,
          impact_base=2, confidence="medium", affected=2, applicable=len(ctx["html_pages"]),
          evidence="The phone number(s) shown in the site header/footer (%d distinct) do not "
                   "match any number on the contact page (%d distinct). Neither set overlaps."
                   % (len(site_wide), len(contact_numbers)),
          mechanism="When a site states the same fact two different ways, an extractor has no "
                    "way to choose and will often surface whichever it saw first - or neither. "
                    "For contact details specifically, an assistant repeating the wrong number "
                    "sends real customers to a dead line, and the inconsistency also weakens "
                    "the local-business signals that depend on a single consistent NAP "
                    "(name, address, phone).",
          evidence_items=[{"url": sorted(urls)[0], "observed": "site-wide number ending %s" % phone[-4:],
                           "expected": "the same number as the contact page"}
                          for phone, urls in list(site_wide.items())[:4]],
          affected_urls=[p["url"] for p in contact_pages],
          remediation={"summary": "Publish one canonical set of contact details from a single source.",
                       "steps": ["Store contact details in one place (a config or CMS record) and render every occurrence from it.",
                                 "If different numbers are intentional (sales vs support, different regions), label each explicitly on the page and in structured data with `contactPoint` and `contactType`.",
                                 "Make the same values match your external listings."],
                       "expected_benefit": "One unambiguous, correctly-quotable contact fact.",
                       "effort": "low"},
          dedupe_key="contact-inconsistent")


def check_entity_disambiguation(snap, ctx, r):
    """Does the site give a machine enough to tell it from a same-named entity?"""
    if not guard(ctx, r, "FC-09", "Entity disambiguation", "consistency"):
        return
    entry = ctx["entry"]
    if entry is None:
        r.add("FC-09", "Entity disambiguation", pillar=AI_DISCOVERABILITY,
              category="consistency", status=NOT_EVALUATED,
              applicability="The entry page was not parsed.")
        return
    corpus = " ".join(filter(None, [ex(p).get("text_excerpt") for p in ctx["html_pages"]]))
    nodes = []
    for p in ctx["html_pages"]:
        for raw in ex(p).get("jsonld_blocks") or []:
            try:
                flatten_jsonld(json.loads(raw), nodes)
            except Exception:
                continue
    same_as = []
    for node in nodes:
        value = node.get("sameAs")
        if isinstance(value, str):
            same_as.append(value)
        elif isinstance(value, list):
            same_as += [v for v in value if isinstance(v, str)]

    attributes = {
        "a physical or postal location": bool(
            re.search(r"\b(?:street|st\.|road|rd\.|avenue|suite|floor|"
                      r"[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}|\b\d{5}(?:-\d{4})?)\b", corpus)),
        "a founding or established date": bool(
            re.search(r"\b(?:founded|established|since|est\.)\s*(?:in\s*)?(?:19|20)\d{2}\b",
                      corpus, re.I)
            or any(n.get("foundingDate") for n in nodes)),
        "a registration or tax identifier": bool(_IDENTIFIER_RE.search(corpus)),
        "an explicit category or industry statement": bool(
            re.search(r"\b(?:is a|we are a|we are an|is an)\s+[a-z][\w\- ]{4,60}"
                      r"(?:company|agency|platform|firm|studio|charity|nonprofit|"
                      r"practice|clinic|school|publisher|manufacturer|retailer|"
                      r"consultancy|service|marketplace|provider|team|business)\b",
                      corpus, re.I)),
        "links to external profiles (sameAs)": bool(same_as),
    }
    present = [k for k, v in attributes.items() if v]
    missing = [k for k, v in attributes.items() if not v]
    if len(present) >= 3:
        r.add("FC-09", "The site provides enough attributes to disambiguate the entity",
              pillar=AI_DISCOVERABILITY, category="consistency", status=PASS,
              evidence="%d of %d disambiguating attributes are present: %s."
                       % (len(present), len(attributes), "; ".join(present)),
              applicable=len(attributes), unit="attributes")
        return
    r.add("FC-09", "Too little on the site distinguishes this brand from a same-named one",
          pillar=AI_DISCOVERABILITY, category="consistency", status=FAIL,
          impact_base=3 if len(present) == 0 else 2, confidence="medium",
          affected=len(missing), applicable=len(attributes), unit="attributes",
          evidence="Only %d of %d disambiguating attributes were found across %d sampled "
                   "pages. Present: %s. Absent: %s."
                   % (len(present), len(attributes), len(ctx["html_pages"]),
                      "; ".join(present) or "none", "; ".join(missing)),
          mechanism="Entity resolution fails by *merging*, not by returning nothing. When a "
                    "brand name is shared with another company, a product, a place or a common "
                    "word, a system with no distinguishing attributes picks the entity it "
                    "already knows best - and then answers questions about your brand using "
                    "someone else's facts. Location, founding date, registration identifiers, "
                    "an explicit category sentence and external profile links are the "
                    "attributes that break the tie.",
          evidence_items=[{"url": entry["url"], "observed": "no %s found on the site" % attr,
                           "expected": "the attribute stated in text and, where applicable, in "
                                       "structured data"} for attr in missing[:6]],
          affected_urls=[entry["url"]],
          remediation={"summary": "State the attributes that make this entity unmistakable, in "
                                  "text and in structured data.",
                       "steps": ["Add one sentence in the form '<Brand> is a <category> based in <place>, founded in <year>' to the about page and the home page - this single sentence supplies three disambiguators at once.",
                                 "Publish the registered company/charity number and registered address if you have one.",
                                 "Add `sameAs` links to the external profiles that already describe the same entity.",
                                 "Use the same `@id` for the organisation on every page so the attributes accumulate onto one node."],
                       "expected_benefit": "Prevents the brand being merged with a "
                                           "similarly-named entity, which is the root cause of "
                                           "confidently wrong answers about a company.",
                       "effort": "low"},
          dedupe_key="entity-ambiguity", tags=["semantic-review"])


def check_offsite_corroboration(snap, ctx, r, research=None):
    """Off-site corroboration - only when the host environment supplied research."""
    if not research or not research.get("evaluated"):
        r.add("FC-10", "Off-site corroboration of brand facts", pillar=AI_DISCOVERABILITY,
              category="corroboration", status=NOT_EVALUATED,
              applicability="No web-search capability was available to this run, so "
                            "third-party corroboration was not assessed. This is a coverage "
                            "limitation, not a finding: no conclusion is drawn about whether "
                            "the brand is described elsewhere. On-site corroboration "
                            "affordances are covered by FC-09 and AC-18.")
        return
    queries = research.get("queries") or []
    sources = research.get("sources") or []
    conflicts = research.get("conflicting_facts") or []
    corroborated = research.get("corroborated_facts") or []
    independent = {urlsplit(s.get("url", "")).hostname or "" for s in sources}
    independent.discard("")
    independent = {h for h in independent
                   if not h.endswith(ctx["site"]) and ctx["site"] not in h}

    if conflicts:
        r.add("FC-11", "Third-party sources state facts that contradict the site",
              pillar=AI_DISCOVERABILITY, category="corroboration", status=FAIL,
              impact_base=3, confidence="medium",
              affected=len(conflicts), applicable=max(len(corroborated) + len(conflicts), 1),
              unit="facts",
              evidence="%d fact(s) checked against %d independent source(s) disagree with the "
                       "site: %s." % (len(conflicts), len(independent),
                                      "; ".join(c.get("summary", "")[:90] for c in conflicts[:3])),
              mechanism="Where an assistant finds disagreement it favours the version repeated "
                        "by more independent sources - which may be an outdated third-party "
                        "listing rather than the brand's own page. The brand then finds itself "
                        "described with details it has already corrected on its own site.",
              evidence_items=[{"url": c.get("source_url", ""),
                               "observed": c.get("external_value", "")[:120],
                               "expected": c.get("site_value", "")[:120],
                               "excerpt": c.get("summary", "")[:140]} for c in conflicts[:6]],
              remediation={"summary": "Correct the third-party records that disagree, then make "
                                      "the on-site value unambiguous.",
                           "steps": ["List the specific external pages carrying the outdated value and use each platform's own correction route to update it.",
                                     "State the correct value on your site in plain text *and* in structured data so the authoritative version is machine-readable.",
                                     "Re-check the external sources after a few weeks."],
                           "expected_benefit": "Removes the contradiction that causes assistants "
                                               "to repeat outdated facts.",
                           "effort": "medium"},
              dedupe_key="offsite-conflict")
    if len(independent) < 2:
        r.add("FC-10", "Brand facts are not corroborated by independent sources",
              pillar=AI_DISCOVERABILITY, category="corroboration", status=FAIL,
              impact_base=2, confidence="low",
              affected=1, applicable=1, unit="sites",
              evidence="Across %d bounded search(es), %d independent host(s) describing the "
                       "brand were observed (%s)."
                       % (len(queries), len(independent), ", ".join(sorted(independent)) or "none"),
              mechanism="A fact repeated across unrelated sources is treated as more reliable "
                        "than one that exists only on the brand's own site. With little "
                        "third-party coverage, an assistant has nothing to reinforce the "
                        "brand's own claims and will hedge, omit them, or prefer a competitor "
                        "whose claims are echoed elsewhere.",
              remediation={"summary": "Build a small number of authoritative third-party records "
                                      "that state the same facts.",
                           "steps": ["Prioritise sources that already rank for the brand name: industry directories, professional bodies, registries, review platforms relevant to the category.",
                                     "Use identical name, description and URL everywhere so the records reinforce one entity.",
                                     "Link to them from the site and list them in `sameAs`."],
                           "expected_benefit": "Turns single-source claims into corroborated facts.",
                           "effort": "high"},
              dedupe_key="offsite-thin",
              applicability="Reported at low confidence: a small, bounded search sample cannot "
                            "prove absence of coverage. It is evidence of what was *not "
                            "observed*, not proof that nothing exists.")
    else:
        r.add("FC-10", "Independent sources describe the brand consistently",
              pillar=AI_DISCOVERABILITY, category="corroboration", status=PASS,
              evidence="%d independent host(s) were observed describing the brand across %d "
                       "bounded search(es), with %d corroborated fact(s) and no contradictions."
                       % (len(independent), len(queries), len(corroborated)),
              applicable=1, unit="sites")


CHECKS = [
    check_dates_present,
    check_content_staleness,
    check_date_consistency,
    check_copyright_year,
    check_placeholder_content,
    check_sitemap_lastmod,
    check_contact_consistency,
    check_entity_disambiguation,
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", default="-")
    parser.add_argument("--research", default="",
                        help="optional off-site research JSON (see references/)")
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)

    results = Results(PRODUCER)
    try:
        snapshot = load_json(args.snapshot)
    except Exception as exc:
        results.errors.append({"check": "load_snapshot",
                               "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
        snapshot = {}
    research = None
    if args.research:
        try:
            research = load_json(args.research)
        except Exception as exc:
            results.errors.append({"check": "load_research",
                                   "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
            results.note("off-site research file could not be read; corroboration reported as "
                         "NOT_EVALUATED")
    if snapshot:
        try:
            ctx = build_context(snapshot)
        except Exception as exc:
            results.errors.append({"check": "build_context",
                                   "error": "%s: %s" % (type(exc).__name__,
                                                          str(exc)[:200])})
            ctx = {"pages": [], "html_pages": [], "entry": None, "origin": "",
                   "site": "", "today": datetime.now(timezone.utc).date()}
        run_checks(results, snapshot, ctx, CHECKS)
        try:
            check_offsite_corroboration(snapshot, ctx, results, research)
        except Exception as exc:                       # pragma: no cover
            results.errors.append({"check": "check_offsite_corroboration",
                                   "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})

    payload = json.dumps(results.to_dict(), indent=2, ensure_ascii=False)
    if args.out in ("-", "", None):
        sys.stdout.write(payload + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
