"""Analyzer detection accuracy: true positives, and - just as important - the
absence of false positives.

Each test builds a snapshot containing exactly one defect (or exactly one
correct configuration) and asserts the corresponding check's status. Snapshots
are built in memory from real HTML, so the same parser the crawler uses is
exercised.
"""

from __future__ import annotations

import unittest

from helpers import checks_by_id, make_page, minimal_snapshot, run_analyzer

CLEAN_BODY = (
    "<p>Northwind Analytics builds workforce-planning software for hospitals. "
    "The platform forecasts nurse demand from historical admissions and "
    "publishes a staffing plan every morning. Teams in twelve countries use it, "
    "and every plan exports as CSV.</p>"
    "<p>Pricing starts at 490 US dollars per month for a single facility, billed "
    "annually. Support runs on weekdays from 08:00 to 18:00 CET.</p>"
)


def doc(title="Northwind Analytics", *, h1="Workforce planning for hospitals",
        body=CLEAN_BODY, head="", canonical="https://example.test/", lang="en",
        viewport=True, nav=True):
    parts = ['<!doctype html><html lang="%s"><head><meta charset="utf-8">' % lang,
             "<title>%s</title>" % title,
             '<meta name="description" content="Workforce planning for hospitals.">']
    if viewport:
        parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    if canonical:
        parts.append('<link rel="canonical" href="%s">' % canonical)
    parts.append(head)
    parts.append("</head><body>")
    if nav:
        parts.append('<header><nav><a href="/about">About us</a>'
                     '<a href="/contact">Contact</a><a href="/privacy">Privacy</a>'
                     "</nav></header>")
    parts.append("<main><h1>%s</h1>%s"
                 '<a class="btn" href="/demo">Book a demo</a>'
                 '<a href="/about">Read about our forecasting method</a></main>' % (h1, body))
    parts.append("<footer><p>&copy; 2026 Northwind Analytics</p></footer></body></html>")
    return "".join(parts)


ORG_JSONLD = (
    '<script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Organization",'
    '"@id":"https://example.test/#org","name":"Northwind Analytics",'
    '"url":"https://example.test/","logo":"https://example.test/logo.png",'
    '"description":"Workforce planning for hospitals",'
    '"sameAs":["https://www.linkedin.com/company/northwind",'
    '"https://en.wikipedia.org/wiki/Northwind"]}</script>'
)


def snapshot_with(*pages, **overrides):
    return minimal_snapshot(pages=list(pages), **overrides)


class TestCrawlRenderDetection(unittest.TestCase):
    def run_checks(self, snapshot):
        return checks_by_id(run_analyzer("crawl-render-audit", snapshot))

    def test_healthy_page_produces_no_access_or_metadata_failures(self):
        checks = self.run_checks(snapshot_with(
            make_page(html=doc(head=ORG_JSONLD),
                      headers={"content-encoding": "gzip"})))
        for check_id in ("CR-01", "CR-02", "CR-13", "CR-15", "CR-16", "CR-17",
                         "CR-19", "CR-20"):
            with self.subTest(check=check_id):
                self.assertEqual(checks[check_id]["status"], "PASS",
                                 checks[check_id].get("evidence"))

    def test_noindex_is_detected_from_meta_and_from_header(self):
        meta = make_page(html=doc(head='<meta name="robots" content="noindex,follow">'))
        checks = self.run_checks(snapshot_with(meta))
        self.assertEqual(checks["CR-08"]["status"], "FAIL")
        self.assertIn("noindex", checks["CR-08"]["evidence"])

        header = make_page(html=doc(), headers={"x-robots-tag": "noindex"})
        checks = self.run_checks(snapshot_with(header))
        self.assertEqual(checks["CR-08"]["status"], "FAIL")
        self.assertIn("X-Robots-Tag", checks["CR-08"]["evidence_items"][0]["observed"])

    def test_ai_crawler_block_is_reported_at_site_level(self):
        snapshot = snapshot_with(make_page(html=doc()))
        snapshot["robots"] = {"outcome": "ok", "url": "https://example.test/robots.txt",
                              "status": 200, "sitemaps": [], "group_count": 1,
                              "groups": [{"agents": ["GPTBot"], "blocks_everything": True,
                                          "rules": [{"allow": False, "pattern": "/"}],
                                          "crawl_delay": None}]}
        snapshot["robots_agent_matrix"] = {
            "gptbot": {"kind": "ai", "purpose": "OpenAI", "allowed_at_root": False,
                       "has_explicit_group": True, "group_blocks_everything": True},
            "claudebot": {"kind": "ai", "purpose": "Anthropic", "allowed_at_root": True,
                          "has_explicit_group": False, "group_blocks_everything": False},
            "googlebot": {"kind": "search", "purpose": "Google", "allowed_at_root": True,
                          "has_explicit_group": False, "group_blocks_everything": False},
        }
        checks = self.run_checks(snapshot)
        self.assertEqual(checks["CR-04"]["status"], "FAIL")
        self.assertEqual(checks["CR-04"]["breadth"]["unit"], "sites")
        self.assertEqual(checks["CR-05"]["status"], "PASS")

    def test_no_robots_txt_is_a_pass_not_a_defect(self):
        checks = self.run_checks(snapshot_with(make_page(html=doc())))
        self.assertEqual(checks["CR-03"]["status"], "PASS")
        self.assertIn("valid configuration", checks["CR-03"]["evidence"])

    def test_render_gap_needs_two_signals(self):
        shell = ('<!doctype html><html><head><title>App</title></head><body>'
                 '<div id="root"></div><script>window.__NEXT_DATA__={}</script>'
                 '<script src="/a.js"></script><script src="/b.js"></script>'
                 "</body></html>")
        checks = self.run_checks(snapshot_with(make_page(html=shell)))
        self.assertEqual(checks["CR-11"]["status"], "FAIL")
        self.assertIn("render-gap-root-cause", checks["CR-11"]["tags"])

    def test_server_rendered_framework_site_is_not_flagged(self):
        """A framework marker alone must never produce a render-gap finding."""
        html = doc(head='<script>window.__NEXT_DATA__={"props":{}}</script>')
        html = html.replace("<body>", '<body><div id="root">')
        html = html.replace("</body>", "</div></body>")
        checks = self.run_checks(snapshot_with(make_page(html=html)))
        self.assertEqual(checks["CR-11"]["status"], "PASS",
                         "server-rendered content inside a mount point is not a render gap")

    def test_duplicate_titles_are_detected(self):
        pages = [make_page("https://example.test/a", html=doc(title="Same Title")),
                 make_page("https://example.test/b", depth=1,
                           html=doc(title="Same Title"))]
        checks = self.run_checks(snapshot_with(*pages))
        self.assertEqual(checks["CR-14"]["status"], "FAIL")
        self.assertIn("Same Title".lower(), checks["CR-14"]["evidence"].lower())

    def test_canonical_collapse_is_detected_but_a_single_duplicate_is_not(self):
        collapsed = [make_page("https://example.test/a", html=doc(canonical="https://example.test/")),
                     make_page("https://example.test/b", depth=1,
                               html=doc(canonical="https://example.test/")),
                     make_page("https://example.test/", depth=0, html=doc())]
        checks = self.run_checks(snapshot_with(*collapsed))
        self.assertEqual(checks["CR-21"]["status"], "FAIL")

        single = [make_page("https://example.test/a",
                            html=doc(canonical="https://example.test/")),
                  make_page("https://example.test/", depth=0, html=doc())]
        checks = checks_by_id(run_analyzer("crawl-render-audit", snapshot_with(*single)))
        self.assertNotIn("CR-21", checks,
                         "one page canonicalising to another is a legitimate duplicate")

    def test_cross_domain_canonical_is_detected(self):
        checks = self.run_checks(snapshot_with(
            make_page(html=doc(canonical="https://other.test/x"))))
        self.assertEqual(checks["CR-22"]["status"], "FAIL")

    def test_broken_links_are_reported_with_reference_counts(self):
        snapshot = snapshot_with(make_page(html=doc()))
        snapshot["link_checks"] = [
            {"url": "https://example.test/gone", "status": 404, "ok": False,
             "method": "HEAD", "redirect_hops": 0, "final_url": "", "error": "",
             "error_kind": "", "reference_count": 4,
             "referenced_from": [{"from": "https://example.test/", "text": "Pricing",
                                  "region": "nav"}]}]
        checks = self.run_checks(snapshot)
        self.assertEqual(checks["CR-25"]["status"], "FAIL")
        self.assertEqual(checks["CR-25"]["dedupe_key"], "broken-internal-links")

    def test_soft_404_and_correct_404(self):
        for status, expected in ((200, "FAIL"), (404, "PASS")):
            snapshot = snapshot_with(make_page(html=doc()))
            snapshot["not_found_probe"] = {"evaluated": True, "status": status,
                                           "url": "https://example.test/nope",
                                           "soft_404": status == 200, "word_count": 12,
                                           "internal_link_count": 0, "title": "Oops",
                                           "redirected_to": "", "redirect_hops": 0}
            with self.subTest(status=status):
                self.assertEqual(self.run_checks(snapshot)["CR-26"]["status"], expected)

    def test_missing_sitemap_is_reported_when_candidates_really_404(self):
        checks = self.run_checks(snapshot_with(
            make_page(html=doc()),
            sitemaps=[{"url": "https://example.test/sitemap.xml", "status": 404,
                       "ok": False, "responded": True, "error_kind": "",
                       "url_count": 0, "referenced_in_robots": False}]))
        self.assertEqual(checks["CR-27"]["status"], "FAIL")
        self.assertEqual(checks["CR-27"]["dedupe_key"], "sitemap-missing")

    def test_missing_sitemap_needs_evidence_that_it_is_missing(self):
        """No candidate was recorded, so absence cannot be asserted."""
        checks = self.run_checks(snapshot_with(make_page(html=doc())))
        self.assertEqual(checks["CR-27"]["status"], "NOT_EVALUATED")

    def test_no_html_pages_yields_not_evaluated_not_failures(self):
        snapshot = minimal_snapshot(pages=[make_page(status=503, html=None)])
        checks = self.run_checks(snapshot)
        self.assertEqual(checks["CR-01"]["status"], "FAIL")     # the entry really did fail
        for check_id in ("CR-08", "CR-11", "CR-13", "CR-16", "CR-20"):
            with self.subTest(check=check_id):
                self.assertEqual(checks[check_id]["status"], "NOT_EVALUATED")

    def test_private_host_is_not_flagged_for_plain_http(self):
        snapshot = snapshot_with(make_page("http://127.0.0.1:8000/", html=doc()),
                                 origin="http://127.0.0.1:8000")
        self.assertNotIn("CR-30", self.run_checks(snapshot))


class TestCitabilityDetection(unittest.TestCase):
    def run_checks(self, snapshot):
        return checks_by_id(run_analyzer("ai-citability-audit", snapshot))

    def test_valid_jsonld_passes_and_malformed_fails(self):
        good = self.run_checks(snapshot_with(make_page(html=doc(head=ORG_JSONLD))))
        self.assertEqual(good["AC-03"]["status"], "PASS")
        self.assertEqual(good["AC-01"]["status"], "PASS")
        self.assertEqual(good["AC-04"]["status"], "PASS")

        bad_json = ('<script type="application/ld+json">'
                    '{"@type":"Organization","name":"Northwind",}</script>')
        bad = self.run_checks(snapshot_with(make_page(html=doc(head=bad_json))))
        self.assertEqual(bad["AC-03"]["status"], "FAIL")
        self.assertTrue(bad["AC-03"]["evidence_items"][0]["excerpt"])

    def test_no_structured_data_is_reported_with_a_denominator(self):
        pages = [make_page("https://example.test/%d" % i, depth=1, html=doc())
                 for i in range(3)]
        checks = self.run_checks(snapshot_with(*pages))
        self.assertEqual(checks["AC-01"]["status"], "FAIL")
        self.assertIn("0 of 3", checks["AC-01"]["evidence"])

    def test_microdata_counts_as_structured_data(self):
        html = doc().replace("<main>", '<main itemscope itemtype="https://schema.org/Organization">')
        checks = self.run_checks(snapshot_with(make_page(html=html)))
        self.assertEqual(checks["AC-01"]["status"], "PASS")

    def test_product_schema_only_expected_on_product_pages(self):
        editorial = make_page("https://example.test/blog/x", depth=1, html=doc())
        checks = self.run_checks(snapshot_with(make_page(html=doc()), editorial))
        self.assertEqual(checks["AC-06"]["status"], "NOT_APPLICABLE")
        self.assertIn("precondition", checks["AC-06"]["applicability"])

        product = make_page(
            "https://example.test/products/planner", depth=1,
            html=doc(title="Ward Planner",
                     body="<p>The Ward Planner costs $1,290 per year. Add to cart to "
                          "start. SKU NW-01. In stock.</p>" + CLEAN_BODY))
        checks = self.run_checks(snapshot_with(make_page(html=doc()), product))
        self.assertEqual(checks["AC-06"]["status"], "FAIL")
        self.assertIn("1 of 1", checks["AC-06"]["evidence"])

    def test_local_business_schema_requires_a_real_address(self):
        remote = make_page("https://example.test/contact", depth=1,
                           html=doc(body="<p>Email hello@example.test.</p>" + CLEAN_BODY))
        self.assertEqual(self.run_checks(snapshot_with(remote))["AC-08"]["status"],
                         "NOT_APPLICABLE")

        physical = make_page(
            "https://example.test/contact", depth=1,
            html=doc(body="<p>Visit us at 12 Harbour Street, Leeds LS1 4AB. "
                          "Call 0113 496 0000. Opening hours 9-5.</p>" + CLEAN_BODY))
        self.assertEqual(self.run_checks(snapshot_with(physical))["AC-08"]["status"], "FAIL")

    def test_article_schema_requires_length_and_an_editorial_context(self):
        short_post = make_page("https://example.test/blog/x", depth=1,
                               html=doc(body="<p>Short note.</p>"))
        self.assertEqual(self.run_checks(snapshot_with(short_post))["AC-07"]["status"],
                         "NOT_APPLICABLE")

        long_post = make_page(
            "https://example.test/blog/x", depth=1,
            html=doc(body="<p>Published 12 January 2026 by Dr Ana Ruiz.</p>"
                          + CLEAN_BODY * 6))
        self.assertEqual(self.run_checks(snapshot_with(long_post))["AC-07"]["status"], "FAIL")

    def test_markup_contradicting_the_page_is_detected(self):
        lying = ('<script type="application/ld+json">'
                 '{"@context":"https://schema.org","@type":"Product",'
                 '"name":"Zephyr Turbine Assembly",'
                 '"offers":{"@type":"Offer","price":"9999","priceCurrency":"USD"},'
                 '"aggregateRating":{"@type":"AggregateRating","ratingValue":"4.9",'
                 '"reviewCount":"210"}}</script>')
        checks = self.run_checks(snapshot_with(make_page(html=doc(head=lying))))
        self.assertEqual(checks["AC-09"]["status"], "FAIL")
        observed = " ".join(i["observed"] for i in checks["AC-09"]["evidence_items"])
        self.assertIn("aggregateRating", observed)

    def test_honest_markup_passes_the_fidelity_check(self):
        honest = ('<script type="application/ld+json">'
                  '{"@context":"https://schema.org","@type":"Product",'
                  '"name":"Northwind Analytics","offers":{"@type":"Offer",'
                  '"price":"490","priceCurrency":"USD"}}</script>')
        checks = self.run_checks(snapshot_with(make_page(html=doc(head=honest))))
        self.assertEqual(checks["AC-09"]["status"], "PASS")

    def test_brand_name_variants_are_not_a_conflict(self):
        pages = [make_page("https://example.test/", html=doc(
                     title="Home | Northwind Analytics Ltd",
                     head=ORG_JSONLD + '<meta property="og:site_name" content="Northwind Analytics">')),
                 make_page("https://example.test/about", depth=1,
                           html=doc(title="About | Northwind Analytics Ltd"))]
        checks = self.run_checks(snapshot_with(*pages))
        self.assertEqual(checks["AC-05"]["status"], "PASS",
                         "'Northwind Analytics' vs 'Northwind Analytics Ltd' is not a conflict")

    def test_genuinely_different_names_are_reported(self):
        conflicting = doc(
            title="Home | Northwind Analytics",
            head=('<script type="application/ld+json">{"@context":"https://schema.org",'
                  '"@type":"Organization","name":"Vantage Health Systems",'
                  '"url":"https://example.test/","description":"x","logo":"y",'
                  '"sameAs":["https://linkedin.com/x"]}</script>'))
        pages = [make_page("https://example.test/", html=conflicting),
                 make_page("https://example.test/about", depth=1,
                           html=doc(title="About | Northwind Analytics"))]
        self.assertEqual(self.run_checks(snapshot_with(*pages))["AC-05"]["status"], "FAIL")

    def test_single_name_signal_is_inconclusive_not_a_finding(self):
        bare = ('<!doctype html><html lang="en"><head><title>Northwind</title></head>'
                "<body><main><h1>Northwind</h1><p>%s</p></main></body></html>" % CLEAN_BODY)
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=bare)))["AC-05"]["status"],
                         "INCONCLUSIVE")

    def test_decorative_images_are_not_reported_as_missing_alt(self):
        decorated = doc(body=CLEAN_BODY + '<img src="/x.png" alt=""><img src="/y.png" alt="A chart">')
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=decorated)))["AC-13"]["status"],
                         "PASS")
        undescribed = doc(body=CLEAN_BODY + '<img src="/x.png"><img src="/y.png">')
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=undescribed)))["AC-13"]["status"],
                         "FAIL")

    def test_placeholder_h1_is_reported_but_a_real_one_is_not(self):
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=doc(h1="Welcome"))))["AC-11"]["status"],
                         "FAIL")
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=doc())))["AC-11"]["status"],
                         "PASS")

    def test_no_html_pages_yields_not_evaluated(self):
        checks = self.run_checks(minimal_snapshot(pages=[make_page(status=403, html=None)]))
        self.assertEqual(checks["AC-01"]["status"], "NOT_EVALUATED")


class TestFreshnessDetection(unittest.TestCase):
    def run_checks(self, snapshot, extra=None):
        return checks_by_id(run_analyzer("freshness-corroboration", snapshot, extra))

    def test_editorial_pages_without_dates_are_reported(self):
        post = make_page("https://example.test/blog/x", depth=1,
                         html=doc(body=CLEAN_BODY * 3))
        self.assertEqual(self.run_checks(snapshot_with(post))["FC-01"]["status"], "FAIL")

    def test_non_editorial_pages_are_not_expected_to_carry_dates(self):
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=doc())))["FC-01"]["status"],
                         "NOT_APPLICABLE")

    def test_a_dated_editorial_page_passes(self):
        dated = doc(body='<time datetime="2026-08-01">1 August 2026</time>' + CLEAN_BODY * 3)
        post = make_page("https://example.test/blog/x", depth=1, html=dated)
        self.assertEqual(self.run_checks(snapshot_with(post))["FC-01"]["status"], "PASS")

    def test_contradictory_dates_are_detected(self):
        conflicting = doc(head=(
            '<script type="application/ld+json">{"@context":"https://schema.org",'
            '"@type":"BlogPosting","datePublished":"2026-08-01",'
            '"dateModified":"2025-01-01"}</script>'))
        post = make_page("https://example.test/blog/x", depth=1,
                         html=conflicting.replace("</main>", "</main>"))
        self.assertEqual(self.run_checks(snapshot_with(post))["FC-04"]["status"], "FAIL")

    def test_placeholder_content_is_detected(self):
        page = make_page(html=doc(body="<p>Lorem ipsum dolor sit amet, consectetur.</p>"))
        checks = self.run_checks(snapshot_with(page))
        self.assertEqual(checks["FC-06"]["status"], "FAIL")
        self.assertTrue(checks["FC-06"]["evidence_items"][0]["excerpt"])

    def test_entity_disambiguation_counts_attributes(self):
        thin = make_page(html=doc())
        checks = self.run_checks(snapshot_with(thin))
        self.assertEqual(checks["FC-09"]["status"], "FAIL")

        rich = make_page(html=doc(
            head=ORG_JSONLD,
            body="<p>Northwind Analytics is a healthcare software company based at "
                 "12 Harbour Street, Leeds LS1 4AB, founded in 2014. Registered in "
                 "England, company number 09112233.</p>" + CLEAN_BODY))
        self.assertEqual(self.run_checks(snapshot_with(rich))["FC-09"]["status"], "PASS")

    def test_offsite_corroboration_is_not_evaluated_without_research(self):
        checks = self.run_checks(snapshot_with(make_page(html=doc())))
        self.assertEqual(checks["FC-10"]["status"], "NOT_EVALUATED")
        self.assertIn("no conclusion is drawn", checks["FC-10"]["applicability"])

    def test_stale_copyright_only_at_two_years(self):
        recent = doc().replace("&copy; 2026", "&copy; 2025")
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=recent)))["FC-05"]["status"],
                         "PASS")
        old = doc().replace("&copy; 2026", "&copy; 2019")
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=old)))["FC-05"]["status"],
                         "FAIL")

    def test_contact_consistency_requires_both_sides(self):
        checks = self.run_checks(snapshot_with(make_page(html=doc())))
        self.assertEqual(checks["FC-08"]["status"], "NOT_APPLICABLE")


class TestEngagementDetection(unittest.TestCase):
    def run_checks(self, snapshot):
        return checks_by_id(run_analyzer("engagement-audit", snapshot))

    def test_healthy_page_passes_orientation_checks(self):
        checks = self.run_checks(snapshot_with(make_page(html=doc())))
        for check_id in ("EN-01", "EN-02", "EN-11"):
            with self.subTest(check=check_id):
                self.assertEqual(checks[check_id]["status"], "PASS",
                                 checks[check_id].get("evidence"))
        self.assertEqual(checks["EN-13"]["status"], "NOT_APPLICABLE",
                         "a page with no images has no alt text to check")

    def test_missing_primary_action_is_detected(self):
        no_cta = doc(body=CLEAN_BODY).replace(
            '<a class="btn" href="/demo">Book a demo</a>'
            '<a href="/about">Read about our forecasting method</a>', "")
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=no_cta)))["EN-01"]["status"],
                         "FAIL")

    def test_generic_link_text_is_reported_only_above_the_threshold(self):
        generic = doc(body=CLEAN_BODY + "".join(
            '<a href="/p%d">Read more</a>' % i for i in range(10)))
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=generic)))["EN-02"]["status"],
                         "FAIL")
        occasional = doc(body=CLEAN_BODY + '<a href="/p">Read more</a>' + "".join(
            '<a href="/q%d">Detailed page about topic %d</a>' % (i, i) for i in range(12)))
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=occasional)))["EN-02"]["status"],
                         "PASS")

    def test_dead_end_pages_are_detected(self):
        dead = doc(body=CLEAN_BODY * 3).replace(
            '<a class="btn" href="/demo">Book a demo</a>'
            '<a href="/about">Read about our forecasting method</a>', "")
        page = make_page("https://example.test/deep", depth=1, html=dead)
        self.assertEqual(self.run_checks(snapshot_with(page))["EN-04"]["status"], "FAIL")

    def test_pricing_check_does_not_apply_to_informational_sites(self):
        informational = doc(body="<p>This encyclopaedia documents the history of "
                                 "cartography in northern Europe. Entries are edited "
                                 "by volunteers and reviewed annually.</p>")
        checks = self.run_checks(snapshot_with(make_page(html=informational)))
        self.assertEqual(checks["EN-09"]["status"], "NOT_APPLICABLE")

    def test_pricing_check_applies_to_commercial_sites(self):
        commercial = doc(body="<p>Buy our plans. Request a quote for enterprise "
                              "purchase. Our sales team will discuss packages and "
                              "subscription rates for your order.</p>", nav=True)
        commercial = commercial.replace('<a class="btn" href="/demo">Book a demo</a>', "")
        checks = self.run_checks(snapshot_with(make_page(html=commercial)))
        self.assertEqual(checks["EN-09"]["status"], "FAIL")

    def test_error_page_recovery(self):
        snapshot = snapshot_with(make_page(html=doc()))
        snapshot["not_found_probe"] = {"evaluated": True, "status": 404, "word_count": 3,
                                       "internal_link_count": 0, "url": "https://example.test/x",
                                       "title": "404", "soft_404": False,
                                       "redirected_to": "", "redirect_hops": 0}
        self.assertEqual(self.run_checks(snapshot)["EN-07"]["status"], "FAIL")
        snapshot["not_found_probe"]["internal_link_count"] = 8
        self.assertEqual(self.run_checks(snapshot)["EN-07"]["status"], "PASS")

    def test_labelled_form_passes_and_unlabelled_fails(self):
        good = doc(body=CLEAN_BODY + '<form action="/x"><label for="e">Email</label>'
                                     '<input id="e" name="e"><button>Send</button></form>')
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=good)))["EN-12"]["status"],
                         "PASS")
        bad = doc(body=CLEAN_BODY + '<form action="/x"><input name="e" placeholder="Email">'
                                    '<input name="n" placeholder="Name"></form>')
        self.assertEqual(self.run_checks(snapshot_with(make_page(html=bad)))["EN-12"]["status"],
                         "FAIL")

    def test_breadcrumbs_only_expected_on_deep_sites(self):
        shallow = self.run_checks(snapshot_with(make_page(html=doc())))
        self.assertEqual(shallow["EN-14"]["status"], "NOT_APPLICABLE")
        deep = [make_page("https://example.test/a/b", depth=2, html=doc()),
                make_page("https://example.test/c/d", depth=2, html=doc())]
        self.assertEqual(self.run_checks(snapshot_with(*deep))["EN-14"]["status"], "FAIL")

    def test_navigation_consistency_needs_three_pages(self):
        checks = self.run_checks(snapshot_with(make_page(html=doc())))
        self.assertEqual(checks["EN-03"]["status"], "NOT_APPLICABLE")

    def test_orphan_pages_need_a_sitemap(self):
        checks = self.run_checks(snapshot_with(make_page(html=doc())))
        self.assertEqual(checks["EN-05"]["status"], "NOT_EVALUATED")


class TestAnalyzerRobustness(unittest.TestCase):
    """A malformed or hostile snapshot must not stop an analyzer producing output."""

    def test_empty_snapshot_produces_a_valid_document(self):
        for skill in ("crawl-render-audit", "ai-citability-audit",
                      "freshness-corroboration", "engagement-audit"):
            with self.subTest(skill=skill):
                document = run_analyzer(skill, minimal_snapshot())
                self.assertIn("checks", document)
                self.assertEqual(document["errors"], [])
                self.assertTrue(document["checks"])

    def test_garbage_snapshot_does_not_crash_any_analyzer(self):
        garbage = {"pages": [{"url": None, "extraction": "not a dict"}, 42, None],
                   "link_checks": "nope", "robots": [], "sitemaps": {},
                   "coverage": None, "link_graph": 7}
        for skill in ("crawl-render-audit", "ai-citability-audit",
                      "freshness-corroboration", "engagement-audit"):
            with self.subTest(skill=skill):
                document = run_analyzer(skill, garbage)
                self.assertIn("checks", document)   # errors are recorded, not raised

    def test_every_check_declares_the_contract_fields(self):
        snapshot = snapshot_with(make_page(html=doc(head=ORG_JSONLD)))
        for skill in ("crawl-render-audit", "ai-citability-audit",
                      "freshness-corroboration", "engagement-audit"):
            document = run_analyzer(skill, snapshot)
            for check in document["checks"]:
                with self.subTest(skill=skill, check=check["check_id"]):
                    self.assertIn(check["status"],
                                  ("PASS", "FAIL", "NOT_APPLICABLE",
                                   "NOT_EVALUATED", "INCONCLUSIVE"))
                    self.assertIn(check["pillar"],
                                  ("ai_discoverability", "on_site_engagement"))
                    self.assertIn(check["confidence"], ("high", "medium", "low"))
                    self.assertIsInstance(check["breadth"], dict)
                    if check["status"] == "FAIL":
                        self.assertTrue(check["evidence"], "a finding needs evidence")
                        self.assertTrue(check["mechanism"], "a finding needs a mechanism")
                        self.assertTrue(check["remediation"].get("summary"))
                        self.assertTrue(check["remediation"].get("steps"))
                        self.assertGreater(check["impact_base"], 0)
                    if check["status"] in ("NOT_APPLICABLE", "NOT_EVALUATED",
                                           "INCONCLUSIVE"):
                        self.assertTrue(check["applicability"],
                                        "a skipped check must say why")

    def test_check_ids_are_unique_within_a_skill(self):
        snapshot = snapshot_with(make_page(html=doc(head=ORG_JSONLD)))
        for skill in ("crawl-render-audit", "ai-citability-audit",
                      "freshness-corroboration", "engagement-audit"):
            document = run_analyzer(skill, snapshot)
            ids = [c["check_id"] for c in document["checks"]]
            with self.subTest(skill=skill):
                self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
