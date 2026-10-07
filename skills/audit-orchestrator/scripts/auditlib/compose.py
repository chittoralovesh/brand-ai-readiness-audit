"""Turn many skills' check results into one prioritised audit report.

This is the composition the entrypoint skill exists to perform. In order:

1. **Select.** Only `FAIL` becomes a finding. `PASS`, `NOT_APPLICABLE`,
   `NOT_EVALUATED` and `INCONCLUSIVE` go to coverage - never to findings.
   No evidence, no finding.
2. **De-duplicate.** Two skills legitimately observe the same defect from
   different angles (a broken link costs a crawler *and* a visitor). Checks
   declaring the same `dedupe_key` merge into one finding that carries both
   consequences and the union of the evidence.
3. **Roll up to root causes.** Where one defect mechanically produces others -
   a page that only exists after JavaScript has no title, no headings and no
   structured data *because of that* - the symptoms fold into the root-cause
   finding instead of padding the report with derived defects.
4. **Grade.** One severity policy (`severity.py`) applied to every finding.
5. **Correlate.** Combinations that mean more than their parts are described in
   `root_cause_analysis` with an ordered fix sequence. They do not create extra
   findings.
6. **Order and identify.** Deterministic sort, then `F-001...` assigned - so an
   unchanged site produces identical IDs on every run.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import fixprompt
from . import severity as sev

PILLAR_LABEL = {"ai_discoverability": "AI discoverability",
                "on_site_engagement": "On-site engagement"}

# A root-cause tag suppresses its symptom tag when the root cause is broad
# enough to explain them. Kept explicit and small: rollup must be auditable.
ROLLUPS = [
    {
        "root_tag": "render-gap-root-cause",
        "symptom_tag": "render-gap-symptom",
        "min_ratio": 0.5,
        "explanation": ("These defects are consequences of the same cause: the server "
                        "response contains no content, so everything derived from page "
                        "content is missing too. Fixing the render gap resolves them "
                        "together; fixing them individually would not."),
    },
]


def _ratio(check):
    return sev.breadth_ratio(check.get("breadth") or {}) or 0.0


def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


# ---------------------------------------------------------------------------
# 1-2. Selection and de-duplication
# ---------------------------------------------------------------------------

def collect(check_documents: list) -> tuple:
    """Split every check into (failures, non_failures) tagged with its producer."""
    failures, others = [], []
    for document in check_documents:
        producer = document.get("producer", "unknown")
        for check in document.get("checks", []) or []:
            record = dict(check)
            record["producer"] = producer
            (failures if record.get("status") == "FAIL" else others).append(record)
    return failures, others


def merge_duplicates(failures: list) -> list:
    """Fold checks sharing a `dedupe_key` into one finding."""
    groups = {}
    for check in failures:
        groups.setdefault(check.get("dedupe_key") or check["check_id"], []).append(check)

    merged = []
    for key, members in sorted(groups.items()):
        members.sort(key=lambda c: (-sev.score(c), c["check_id"]))
        primary = dict(members[0])
        if len(members) > 1:
            secondary = members[1:]
            primary["merged_from"] = [
                {"check_id": m["check_id"], "producer": m["producer"],
                 "pillar": m["pillar"], "evidence": _clean(m.get("evidence"))}
                for m in secondary]
            seen_urls = list(primary.get("affected_urls") or [])
            for m in secondary:
                for url in m.get("affected_urls") or []:
                    if url not in seen_urls:
                        seen_urls.append(url)
            primary["affected_urls"] = seen_urls[:25]
            items = list(primary.get("evidence_items") or [])
            keys = {(i.get("url"), i.get("observed")) for i in items}
            for m in secondary:
                for item in m.get("evidence_items") or []:
                    if (item.get("url"), item.get("observed")) not in keys:
                        items.append(item)
                        keys.add((item.get("url"), item.get("observed")))
            primary["evidence_items"] = items[:12]
            primary["tags"] = sorted(set(primary.get("tags") or [])
                                     | {t for m in secondary for t in (m.get("tags") or [])})
            # A defect seen from both pillars is worse than one seen from either.
            other_pillars = {m["pillar"] for m in secondary} - {primary["pillar"]}
            if other_pillars:
                primary["also_affects"] = sorted(other_pillars)
                extra = " ".join(_clean(m.get("mechanism")) for m in secondary
                                 if m["pillar"] in other_pillars)
                primary["mechanism"] = _clean(primary.get("mechanism")) + \
                    ("\n\nSame defect, second consequence (%s): %s"
                     % (", ".join(PILLAR_LABEL.get(p, p) for p in sorted(other_pillars)),
                        extra) if extra else "")
        merged.append(primary)
    return merged


# ---------------------------------------------------------------------------
# 3. Root-cause rollup
# ---------------------------------------------------------------------------

def roll_up(findings: list) -> tuple:
    """Fold symptom findings into their root cause. Returns (kept, rollups)."""
    kept, rollups = list(findings), []
    for rule in ROLLUPS:
        roots = [f for f in kept if rule["root_tag"] in (f.get("tags") or [])
                 and _ratio(f) >= rule["min_ratio"]]
        if not roots:
            continue
        root = max(roots, key=lambda f: (sev.score(f), f["check_id"]))
        symptoms = [f for f in kept
                    if rule["symptom_tag"] in (f.get("tags") or []) and f is not root]
        if not symptoms:
            continue
        root.setdefault("contributing_symptoms", []).extend(
            {"check_id": s["check_id"], "producer": s["producer"], "title": s["title"],
             "evidence": _clean(s.get("evidence"))} for s in symptoms)
        root["rollup_note"] = rule["explanation"]
        rollups.append({
            "root_check_id": root["check_id"],
            "root_title": root["title"],
            "folded_check_ids": [s["check_id"] for s in symptoms],
            "reason": rule["explanation"],
        })
        kept = [f for f in kept if f not in symptoms]
    return kept, rollups


# ---------------------------------------------------------------------------
# 4. Grading and finding construction
# ---------------------------------------------------------------------------

def build_finding(check: dict) -> dict:
    severity, value, explanation = sev.severity_of(check)
    remediation = check.get("remediation") or {}
    effort = (remediation.get("effort") or "medium").lower()
    breadth = check.get("breadth") or {}
    ratio = sev.breadth_ratio(breadth)

    evidence = _clean(check.get("evidence"))
    for symptom in check.get("contributing_symptoms") or []:
        evidence += " Downstream effects observed on the same pages: %s" % _clean(symptom["evidence"])
    for merged in check.get("merged_from") or []:
        evidence += " Independently confirmed by %s: %s" % (merged["check_id"],
                                                            _clean(merged["evidence"]))

    finding = {
        "id": "",                                   # assigned after sorting
        "title": check["title"],
        "severity": severity,
        "evidence": evidence,
        "suggested_action": {
            "summary": remediation.get("summary", ""),
            "priority": sev.remediation_priority(severity, effort, breadth),
            "effort": effort,
            "steps": remediation.get("steps", []),
            "expected_benefit": remediation.get("expected_benefit", ""),
        },
        # --- extensions beyond the required schema -------------------------
        "category": check.get("pillar", ""),
        "category_label": PILLAR_LABEL.get(check.get("pillar", ""), check.get("pillar", "")),
        "subcategory": check.get("category", ""),
        "check_id": check["check_id"],
        "dedupe_key": check.get("dedupe_key") or check["check_id"],
        "produced_by": check.get("producer", ""),
        "confidence": check.get("confidence", "high"),
        "severity_score": value,
        "severity_rationale": explanation,
        "impact": _clean(check.get("mechanism")),
        "mechanism": _clean(check.get("mechanism")),
        "breadth": {
            "affected": breadth.get("affected", 0),
            "applicable": breadth.get("applicable", 0),
            "unit": breadth.get("unit", "pages"),
            "ratio": round(ratio, 3) if ratio is not None else None,
        },
        "evidence_items": check.get("evidence_items") or [],
        "affected_urls": check.get("affected_urls") or [],
        "applicability": check.get("applicability", ""),
        "reproducibility": "Deterministic: re-running the audit against an unchanged site "
                           "reproduces this finding from the same observations.",
    }
    if check.get("also_affects"):
        finding["also_affects"] = [PILLAR_LABEL.get(p, p) for p in check["also_affects"]]
    if check.get("merged_from"):
        finding["merged_from"] = check["merged_from"]
    if check.get("contributing_symptoms"):
        finding["contributing_symptoms"] = check["contributing_symptoms"]
        finding["rollup_note"] = check.get("rollup_note", "")
    return finding


def assign_ids(findings: list) -> list:
    ordered = sorted(findings, key=sev.sort_key)
    for index, finding in enumerate(ordered, start=1):
        finding["id"] = "F-%03d" % index
        finding["priority_rank"] = index
    return ordered


# ---------------------------------------------------------------------------
# 5. Cross-skill correlation
# ---------------------------------------------------------------------------

def correlate(findings: list, non_failures: list, snapshot: dict) -> list:
    """Compound diagnoses. These reference existing findings; they never add
    new ones, so the severity counts stay honest.

    Correlation keys on `dedupe_key`, not on check id: a check id can carry
    several distinct verdicts (a missing sitemap and an undeclared sitemap are
    both CR-27), and correlating on the id would mix them up.
    """
    have = {f.get("dedupe_key"): f for f in findings}
    statuses = {c["check_id"]: c["status"] for c in non_failures}
    out = []

    def ids(*keys):
        return [have[k]["id"] for k in keys if k in have]

    # (a) A site that is perfectly reachable but has nothing worth quoting.
    access_ok = (all(statuses.get(cid) in ("PASS", "NOT_APPLICABLE", None)
                     for cid in ("CR-01", "CR-02", "CR-05", "CR-08"))
                 and "robots-blocks-ai" not in have)
    nothing_to_quote = [k for k in ("structured-data-missing", "fact-density",
                                    "value-proposition", "thin-content")
                        if k in have]
    if access_ok and len(nothing_to_quote) >= 2:
        out.append({
            "id": "RC-01",
            "title": "The site is fully crawlable but gives assistants nothing to quote",
            "diagnosis": "Crawl access is healthy - robots.txt, status codes and indexing "
                         "directives all pass - so the reason this brand is absent from "
                         "assistant answers is not access. It is that the pages contain no "
                         "typed values and few checkable statements, so a retrieval system "
                         "that reaches them still has nothing it can extract and attribute.",
            "why_it_matters": "Teams in this position usually spend effort on crawl and "
                              "indexing work that changes nothing, because the bottleneck is "
                              "one step later in the pipeline.",
            "related_findings": ids(*nothing_to_quote),
            "fix_order": ["Add a single quotable sentence to the entry page stating what the "
                          "organisation does, for whom, and one verifiable specific.",
                          "Publish an Organization identity node with sameAs so the brand "
                          "resolves to one entity.",
                          "Add concrete figures, dates and definitions to the pages that "
                          "currently carry only positioning language."],
        })

    # (b) Contradictory indexation signals.
    noindex = have.get("noindex")
    if noindex:
        sitemap_urls = {e.get("loc") for e in (snapshot.get("sitemap_urls") or [])}
        overlap = sorted(set(noindex.get("affected_urls") or []) & sitemap_urls)
        if overlap:
            out.append({
                "id": "RC-02",
                "title": "The site simultaneously asks for and refuses indexing of the same URLs",
                "diagnosis": "%d URL(s) are listed in the XML sitemap - an explicit request to "
                             "index them - while also carrying a `noindex` directive that "
                             "forbids it. `noindex` wins, so the sitemap entry is wasted and "
                             "the contradiction usually means one of the two was set by a "
                             "different system or left over from a previous configuration."
                             % len(overlap),
                "why_it_matters": "Conflicting signals of this kind are the classic symptom of "
                                  "a staging configuration reaching production, and they are "
                                  "invisible in the browser.",
                "related_findings": ids("noindex", "sitemap-missing", "sitemap-broken"),
                "evidence_urls": overlap[:10],
                "fix_order": ["Decide, per URL, whether it should be indexed.",
                              "Remove `noindex` from the ones that should; remove the others "
                              "from the sitemap.",
                              "Trace which system injects the directive so it does not return."],
            })

    # (c) Discovery depends entirely on one page.
    if "sitemap-missing" in have and ("dead-end-pages" in have or "orphan-pages" in have):
        out.append({
            "id": "RC-03",
            "title": "Content discovery has a single point of failure",
            "diagnosis": "There is no sitemap, and content pages do not link onward, so the "
                         "only route a crawler has to the rest of the site is the navigation "
                         "on the entry page. Anything not in that menu is found late or not "
                         "at all, and new pages wait for a re-crawl of the home page.",
            "why_it_matters": "This is why newly published pages can stay invisible for weeks "
                              "on an otherwise healthy site.",
            "related_findings": ids("sitemap-missing", "dead-end-pages", "orphan-pages"),
            "fix_order": ["Publish a sitemap with accurate lastmod and declare it in robots.txt.",
                          "Add contextual in-content links from the pages that currently end "
                          "without one.",
                          "Link the orphaned pages from a relevant hub."],
        })

    # (d) The brand describes itself differently in different places.
    identity_bits = [k for k in ("entity-name-inconsistent", "structured-data-mismatch",
                                 "contact-inconsistent", "entity-ambiguity",
                                 "identity-missing", "no-external-identity") if k in have]
    if len(identity_bits) >= 2:
        out.append({
            "id": "RC-04",
            "title": "The brand's identity is stated inconsistently, which invites mistaken identity",
            "diagnosis": "The site presents conflicting or under-specified versions of who it "
                         "is - across its own name, its structured data, its contact details, "
                         "or the attributes that would distinguish it from a similarly-named "
                         "entity. Entity resolution fails by merging, so the practical outcome "
                         "is not silence but an assistant confidently attributing another "
                         "organisation's facts to this brand.",
            "why_it_matters": "Misrepresentation is harder to detect and more damaging than "
                              "absence, because nothing on the brand's own analytics shows it "
                              "happening.",
            "related_findings": ids(*identity_bits),
            "fix_order": ["Fix the canonical name string first - every other identity signal "
                          "depends on it.",
                          "Publish one Organization node with a stable @id, complete "
                          "attributes and sameAs links.",
                          "Make the contact and location facts identical everywhere they "
                          "appear, on-site and on external profiles."],
        })
    return out


# ---------------------------------------------------------------------------
# 6. Proactive recommendations (opportunities, not defects)
# ---------------------------------------------------------------------------

def proactive_recommendations(findings: list, non_failures: list, snapshot: dict) -> list:
    have = {f.get("dedupe_key") for f in findings} | {f["check_id"] for f in findings}
    statuses = {c["check_id"]: c for c in non_failures}
    pages = [p for p in (snapshot.get("pages") or [])
             if p.get("ok") and isinstance(p.get("extraction"), dict)]
    out = []

    def add(rec_id, title, rationale, mechanism, priority, evidence, steps, effort="low"):
        out.append({"id": rec_id, "title": title, "rationale": rationale,
                    "mechanism": mechanism, "priority": priority, "evidence": evidence,
                    "steps": steps, "effort": effort,
                    "applies_to": snapshot.get("site", "")})

    # 1. A machine-readable context file for LLM fetchers.
    llms = (snapshot.get("well_known") or {}).get("llms_txt") or {}
    if llms.get("evaluated") and not llms.get("ok") and len(pages) >= 3:
        add("PR-01", "Publish an llms.txt context file at the site root",
            "The site has no /llms.txt. This is an emerging convention rather than a "
            "standard, so its absence is not a defect - but it is a cheap way to tell "
            "an LLM fetcher which pages carry the authoritative facts.",
            "An assistant fetching a site under time pressure picks a small number of "
            "URLs. A short Markdown index at a predictable path lets you nominate which "
            "ones - your canonical product, pricing, about and documentation pages - "
            "rather than leaving the choice to whatever the crawler happened to reach.",
            "medium",
            "GET %s returned HTTP %s." % (llms.get("url"), llms.get("status")),
            ["Create /llms.txt as Markdown: an H1 with the organisation name, a one-line "
             "blockquote summary, then linked sections for the pages that hold the "
             "authoritative facts.",
             "Keep it short and link only to canonical URLs.",
             "Serve it as text/plain or text/markdown and regenerate it when the "
             "information architecture changes."])

    # 2. Question-shaped content for answer extraction.
    q_headings = sum(1 for p in pages
                     for h in (p.get("extraction", {}).get("headings") or [])
                     if h.get("text", "").strip().endswith("?"))
    if q_headings == 0 and len(pages) >= 3:
        add("PR-02", "Add question-shaped headings that answer real user questions",
            "No heading anywhere in the sample is phrased as a question, so the site's "
            "content is not organised around the questions people actually ask.",
            "Retrieval matches a user's question against passages. A section headed with "
            "the question, answered in the first sentence beneath it, is the single "
            "highest-yield format for being selected and quoted - and it costs nothing "
            "beyond restructuring copy you already have.",
            "high",
            "0 question-shaped headings across %d sampled pages." % len(pages),
            ["List the 10 questions your sales or support team answers most often.",
             "Give each one an H2 phrased exactly as the question, with a direct answer "
             "in the first sentence and detail after it.",
             "Where several belong together, group them on one page and mark that page "
             "up as FAQPage.",
             "Keep each answer self-contained so a quote of it remains accurate out of "
             "context."])

    # 3. Strengthen a thin corroboration profile.
    same_as_count = 0
    for p in pages:
        for raw in p.get("extraction", {}).get("jsonld_blocks") or []:
            same_as_count += raw.count("sameAs")
    external = (snapshot.get("link_graph") or {}).get("external_hosts") or {}
    if "AC-18" not in have and same_as_count and len(external) < 4:
        add("PR-03", "Broaden the set of independent sources that describe the brand",
            "The site declares identity links, but only %d distinct external host(s) "
            "appear across the sample, so corroboration rests on a narrow base."
            % len(external),
            "Machines weight a fact by how many unrelated sources repeat it. Widening "
            "from a couple of profiles to a handful of category-relevant, independently "
            "maintained records raises the confidence attached to every claim the brand "
            "makes about itself.",
            "medium",
            "%d distinct external host(s) linked from %d sampled pages."
            % (len(external), len(pages)),
            ["Identify the 3-5 sources an expert in your category would check first "
             "(registry, professional body, category directory, review platform, "
             "app or package registry).",
             "Publish identical name, description and URL on each.",
             "Add each to the Organization `sameAs` array."], "medium")

    # 4. Machine-readable updates feed for editorial sites.
    editorial = [p for p in pages
                 if re.search(r"/(blog|news|article|insights|press|updates)(/|$)",
                              p["url"], re.I)]
    has_feed = any(
        "rss" in (l.get("type") or "").lower() or "atom" in (l.get("type") or "").lower()
        or "feed" in (l.get("href") or "").lower()
        for p in pages for l in (p.get("extraction", {}).get("links") or []))
    if editorial and not has_feed:
        add("PR-04", "Publish a feed so updates are discovered without a re-crawl",
            "The site publishes editorial content but exposes no RSS/Atom/JSON feed.",
            "A feed is a push-shaped discovery channel: aggregators, newsletters and "
            "several retrieval pipelines poll it and pick up new articles within hours "
            "instead of waiting for the next crawl of an index page. It also gives every "
            "item an unambiguous date.",
            "low",
            "%d editorial page(s) sampled; no feed link found in any <head>." % len(editorial),
            ["Generate an RSS or JSON feed of the most recent items with title, link, "
             "summary and publication date.",
             'Advertise it with `<link rel="alternate" type="application/rss+xml">` in the '
             "head of the section index."])

    # 5. Structured contact point where contact details exist only as prose.
    contact_text = any(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", p.get("extraction", {})
                                 .get("text_excerpt", "") or "") for p in pages)
    has_contact_schema = any("contactPoint" in raw or "telephone" in raw
                             for p in pages
                             for raw in p.get("extraction", {}).get("jsonld_blocks") or [])
    if contact_text and not has_contact_schema:
        add("PR-05", "Express contact details as structured data, not only as text",
            "Contact details appear in the page text but not in structured data.",
            "A `contactPoint` states which number is for sales and which for support, "
            "in which languages and for which regions. Prose leaves an extractor to "
            "guess, and a wrong guess sends a prospective customer to the wrong place.",
            "medium",
            "Contact details found in visible text on the sampled pages; no "
            "`contactPoint`/`telephone` property in any JSON-LD block.",
            ["Add `contactPoint` entries to the Organization node with `contactType`, "
             "`telephone`, `email`, `areaServed` and `availableLanguage`.",
             "Keep the values identical to the visible ones."])

    # 6. Evergreen pages carry no modification date.
    dated = 0
    for p in pages:
        e = p.get("extraction", {})
        if (e.get("time_elements") or []) or any(
                "dateModified" in raw for raw in e.get("jsonld_blocks") or []):
            dated += 1
    if pages and dated <= max(1, len(pages) // 5) and "FC-01" not in have:
        add("PR-06", "Date the evergreen pages, not just the blog",
            "%d of %d sampled pages expose no modification date." % (len(pages) - dated,
                                                                     len(pages)),
            "Recency is applied to every page, not only to articles. A pricing, product "
            "or policy page with a visible and machine-readable 'last reviewed' date is "
            "treated as maintained; the same page undated is treated as unknown age and "
            "is discounted for questions where currency matters.",
            "low",
            "%d of %d sampled pages carry a machine-readable date." % (dated, len(pages)),
            ["Add a visible 'Last reviewed' date to pages whose accuracy matters, inside "
             "`<time datetime>`.",
             "Mirror it as `dateModified` in the page's structured data.",
             "Update it only when the content actually changes."])

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(out, key=lambda rec: (order.get(rec["priority"], 3), rec["id"]))


# ---------------------------------------------------------------------------
# Coverage and limitations
# ---------------------------------------------------------------------------

def build_coverage(check_documents: list, failures: list, non_failures: list,
                   snapshot: dict) -> dict:
    counts = {"FAIL": len(failures), "PASS": 0, "NOT_APPLICABLE": 0,
              "NOT_EVALUATED": 0, "INCONCLUSIVE": 0}
    for check in non_failures:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    return {
        "checks_by_status": counts,
        "checks_total": len(failures) + len(non_failures),
        "skills_reporting": sorted({d.get("producer", "unknown") for d in check_documents}),
        "checks": sorted(
            [{"check_id": c["check_id"], "title": c["title"], "status": c["status"],
              "produced_by": c.get("producer", ""), "pillar": c.get("pillar", ""),
              "note": c.get("applicability") or c.get("evidence", "")[:200]}
             for c in failures + non_failures],
            key=lambda c: c["check_id"]),
        "crawl": snapshot.get("coverage", {}),
    }


def build_limitations(non_failures: list, snapshot: dict, extra: list = None) -> list:
    out = list(extra or [])
    for check in non_failures:
        if check["status"] in ("NOT_EVALUATED", "INCONCLUSIVE"):
            out.append({
                "scope": check["check_id"],
                "title": check["title"],
                "status": check["status"],
                "reason": check.get("applicability") or "no evidence was available",
                "produced_by": check.get("producer", ""),
            })
    coverage = snapshot.get("coverage", {}) or {}
    config = snapshot.get("config", {}) or {}
    if coverage.get("pages_skipped_by_robots"):
        out.append({
            "scope": "crawl", "status": "NOT_EVALUATED",
            "title": "Pages withheld by robots.txt were not fetched",
            "reason": "%d URL(s) were disallowed for this auditor's user agent and were "
                      "therefore not retrieved. The audit respects robots.txt without "
                      "exception, so anything behind those rules is outside its coverage."
                      % coverage.get("pages_skipped_by_robots", 0)})
    if coverage.get("pages_skipped_unsafe"):
        out.append({
            "scope": "crawl", "status": "NOT_EVALUATED",
            "title": "State-changing URLs were deliberately not visited",
            "reason": "%d URL(s) matching cart, checkout, account, login or logout patterns "
                      "were skipped. The audit is strictly read-only."
                      % coverage.get("pages_skipped_unsafe", 0)})
    if coverage.get("pages_skipped_for_time"):
        out.append({
            "scope": "crawl", "status": "NOT_EVALUATED",
            "title": "Some planned fetches were dropped to stay inside the time budget",
            "reason": "%d fetch(es) were skipped so the audit could finish and return a "
                      "valid report." % coverage.get("pages_skipped_for_time", 0)})
    fetched = coverage.get("pages_fetched", 0)
    total_known = snapshot.get("sitemap_url_total", 0) or 0
    if total_known > fetched:
        out.append({
            "scope": "crawl", "status": "PARTIAL",
            "title": "Findings describe a bounded sample, not the whole site",
            "reason": "%d page(s) were analysed out of at least %d known URLs (crawl caps: "
                      "%d pages, depth %d). Every finding's evidence states its own "
                      "denominator; ratios should be read as 'of the sample'."
                      % (fetched, total_known, config.get("max_pages", 0),
                         config.get("max_depth", 0))})
    return out


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def executive_summary(findings: list, root_causes: list, recommendations: list,
                      snapshot: dict) -> dict:
    """A short, plain-English orientation for a non-expert reader.

    The rubric asks for a report a non-expert could act on. A ranked list of 27
    findings is not that on its own: it needs a headline that says what the
    single biggest problem is and what to do first.
    """
    counts = summarise(findings)
    discoverability = [f for f in findings if f["category"] == "ai_discoverability"]
    engagement = [f for f in findings if f["category"] == "on_site_engagement"]
    top = findings[:3]

    if not findings:
        headline = ("No evidence-backed defect was found in the checks that could be "
                    "evaluated on this site. Review the coverage and limitations sections "
                    "to see what was and was not assessed.")
    else:
        worst = findings[0]
        headline = (
            "%d finding(s): %d critical, %d high, %d medium, %d low. The most consequential "
            "is %s - %s. %d finding(s) affect how machines find and cite the site; %d affect "
            "what happens to a visitor once they arrive."
            % (counts["total_findings"], counts["critical"], counts["high"],
               counts["medium"], counts["low"], worst["id"], worst["title"].lower(),
               len(discoverability), len(engagement)))
    quick_wins = [f for f in findings
                  if f["severity"] in ("medium", "low")
                  and f["suggested_action"].get("effort") == "low"
                  and (f["breadth"].get("ratio") or 0) >= 0.8][:4]
    summary_block = {
        "headline": headline,
        "start_here": [
            {"finding_id": f["id"], "title": f["title"], "severity": f["severity"],
             "effort": f["suggested_action"].get("effort", ""),
             "do_this": f["suggested_action"].get("summary", ""),
             "why": (f.get("mechanism") or "").split(". ")[0][:220]}
            for f in top],
        "quick_wins": [
            {"finding_id": f["id"], "title": f["title"],
             "do_this": f["suggested_action"].get("summary", "")}
            for f in quick_wins],
        "root_causes_identified": len(root_causes),
        "opportunities_beyond_defects": len(recommendations),
        "how_to_read_this": (
            "Every finding states its own denominator (for example '5 of 6 sampled pages'), "
            "so ratios describe the crawl sample listed under `coverage.crawl`, not the whole "
            "site. `severity` combines how much damage the defect can do, how much of the "
            "sample it touches, and how strong the evidence is; `severity_rationale` shows "
            "that arithmetic. Work down `priority_rank`, and read `root_cause_analysis` first "
            "where several findings share one cause."),
    }
    if counts["low"] >= 5:
        summary_block["note_on_low_severity"] = (
            "%d of the %d findings are low severity. They are listed for completeness and "
            "each is individually small; none of them is why this site would be missed or "
            "misread. Treat everything below `priority_rank` %d as a backlog to work "
            "through, not as a list of urgent problems."
            % (counts["low"], counts["total_findings"],
               next((f["priority_rank"] for f in findings if f["severity"] == "low"), 1)))
    return summary_block


def summarise(findings: list) -> dict:
    summary = {"total_findings": len(findings), "critical": 0, "high": 0,
               "medium": 0, "low": 0}
    for finding in findings:
        summary[finding["severity"]] = summary.get(finding["severity"], 0) + 1
    return summary


def build_report(snapshot: dict, check_documents: list, *, metrics: dict,
                 limitations_extra: list = None, agent_findings: list = None) -> dict:
    failures, non_failures = collect(check_documents)
    merged = merge_duplicates(failures)
    merged, rollups = roll_up(merged)
    findings = [build_finding(check) for check in merged]
    findings.extend(agent_findings or [])
    findings = assign_ids(findings)
    # Every finding then carries a command that proves it fixed, and a prompt an
    # agent can run against the site's own repository. Added here, after IDs are
    # assigned, so analyzers stay pure and one policy governs all of them.
    fixprompt.attach(findings, snapshot)

    root_causes = correlate(findings, non_failures, snapshot)
    recommendations = proactive_recommendations(findings, non_failures, snapshot)
    report = {
        "schema_version": "1.0",
        "report_type": "brand-ai-readiness-audit",
        "site": snapshot.get("site") or snapshot.get("target_url", ""),
        "audited_url": snapshot.get("final_url") or snapshot.get("target_url", ""),
        "audited_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summarise(findings),
        "executive_summary": executive_summary(findings, root_causes, recommendations,
                                               snapshot),
        "findings": findings,
        "root_cause_analysis": root_causes,
        "proactive_recommendations": recommendations,
        "coverage": build_coverage(check_documents, failures, non_failures, snapshot),
        "limitations": build_limitations(non_failures, snapshot, limitations_extra),
        "finding_rollups": rollups,
        "audit_metrics": metrics,
    }
    report["summary"]["by_category"] = {
        PILLAR_LABEL.get(key, key): sum(1 for f in findings if f.get("category") == key)
        for key in ("ai_discoverability", "on_site_engagement")
    }
    report["summary"]["proactive_recommendations"] = len(report["proactive_recommendations"])
    return report
