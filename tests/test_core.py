"""URL normalisation, robots.txt semantics, HTML extraction, severity and schema.

These are the deterministic primitives every finding rests on. If any of them
drift, findings drift with them, so they are tested directly rather than only
through the pipeline.
"""

from __future__ import annotations

import unittest

from helpers import make_check

from auditlib import htmlparse, robots, schema
from auditlib import severity as sev
from auditlib import urls
from auditlib.budget import Budget


class TestUrlNormalisation(unittest.TestCase):
    def test_canonical_form(self):
        cases = [
            ("HTTPS://Example.COM:443/Foo//bar/?utm_source=x&b=2&a=1#frag",
             "https://example.com/Foo/bar/?a=1&b=2"),
            ("http://www.Example.com/index.html", "http://www.example.com/"),
            # The trailing slash is preserved: /a and /a/ are different URLs to a
            # server, and folding them together broke every site that 301s
            # between them. De-duplication uses canonical_key() instead.
            ("https://a.com/a/", "https://a.com/a/"),
            ("https://a.com/x?gclid=1&fbclid=2", "https://a.com/x"),
            ("https://a.com:8080/x", "https://a.com:8080/x"),
        ]
        for raw, expected in cases:
            with self.subTest(url=raw):
                self.assertEqual(urls.normalize(raw), expected)

    def test_canonical_key_is_slash_insensitive(self):
        self.assertEqual(urls.canonical_key("https://a.com/a/"),
                         urls.canonical_key("https://a.com/a"))
        self.assertEqual(urls.canonical_key("https://a.com/"), "https://a.com/")
        self.assertNotEqual(urls.canonical_key("https://a.com/a"),
                            urls.canonical_key("https://a.com/b"))

    def test_normalisation_is_idempotent(self):
        for raw in ("https://a.com/a/b/", "http://A.com/x?b=1&a=2", "https://a.com/%7Euser"):
            once = urls.normalize(raw)
            self.assertEqual(once, urls.normalize(once))

    def test_invalid_input_rejected(self):
        for raw in ("", "not a url", "ftp://x.com/a", "javascript:alert(1)", "http://",
                    "https://nodots", "   "):
            with self.subTest(url=raw):
                self.assertEqual(urls.coerce_input_url(raw), "")

    def test_bare_domain_is_accepted(self):
        self.assertEqual(urls.coerce_input_url("example.com"), "https://example.com/")
        self.assertEqual(urls.coerce_input_url("//example.com"), "https://example.com/")

    def test_same_site_is_www_insensitive_but_not_subdomain_wide(self):
        self.assertTrue(urls.same_site("https://www.a.com/x", "https://a.com/y"))
        self.assertFalse(urls.same_site("https://blog.a.com/x", "https://a.com/y"))

    def test_state_changing_urls_are_rejected(self):
        for unsafe in ("https://a.com/cart", "https://a.com/checkout/step-1",
                       "https://a.com/wp-login.php", "https://a.com/p?add-to-cart=5",
                       "https://a.com/account/settings", "https://a.com/logout"):
            with self.subTest(url=unsafe):
                self.assertTrue(urls.is_unsafe(unsafe))
        for safe in ("https://a.com/about", "https://a.com/products/widget",
                     "https://a.com/blog/post"):
            with self.subTest(url=safe):
                self.assertFalse(urls.is_unsafe(safe))

    def test_trap_scoring_prefers_shallow_clean_urls(self):
        self.assertEqual(urls.trap_score("https://a.com/about"), 0)
        self.assertGreater(urls.trap_score("https://a.com/blog/page/9?sort_by=x&a=1&b=2"), 1)
        self.assertGreater(urls.trap_score("https://a.com/2024/03/17/post"), 0)

    def test_non_html_resources_are_recognised(self):
        self.assertFalse(urls.is_probably_html("https://a.com/x.jpg"))
        self.assertFalse(urls.is_probably_html("https://a.com/x.pdf"))
        self.assertTrue(urls.is_document("https://a.com/price-list.pdf"))
        self.assertTrue(urls.is_probably_html("https://a.com/x"))


class TestRobots(unittest.TestCase):
    SAMPLE = (
        "User-agent: *\n"
        "Disallow: /private/\n"
        "Allow: /private/public$\n"
        "Crawl-delay: 1.5\n"
        "Sitemap: https://x.com/sitemap.xml\n"
        "\n"
        "User-agent: GPTBot\n"
        "User-agent: CCBot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: Googlebot\n"
        "Disallow:\n"
    )

    def setUp(self):
        self.policy = robots.parse(self.SAMPLE, url="https://x.com/robots.txt", status=200)

    def test_longest_match_and_end_anchor(self):
        self.assertFalse(self.policy.can_fetch("MyBot", "https://x.com/private/x"))
        self.assertTrue(self.policy.can_fetch("MyBot", "https://x.com/private/public"))
        self.assertFalse(self.policy.can_fetch("MyBot", "https://x.com/private/publicX"))

    def test_per_agent_groups(self):
        self.assertFalse(self.policy.can_fetch("GPTBot", "https://x.com/"))
        self.assertFalse(self.policy.can_fetch("CCBot", "https://x.com/"))
        self.assertTrue(self.policy.can_fetch("Googlebot", "https://x.com/anything"))

    def test_empty_disallow_allows_everything(self):
        policy = robots.parse("User-agent: *\nDisallow:\n")
        self.assertTrue(policy.can_fetch("Any", "https://x.com/deep/path"))

    def test_wildcards_in_patterns(self):
        policy = robots.parse("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(policy.can_fetch("Any", "https://x.com/a/b.pdf"))
        self.assertTrue(policy.can_fetch("Any", "https://x.com/a/b.pdf?x=1"))

    def test_sitemap_and_crawl_delay(self):
        self.assertEqual(self.policy.sitemaps, ["https://x.com/sitemap.xml"])
        self.assertEqual(self.policy.crawl_delay_for("MyBot"), 1.5)

    def test_blocked_agent_catalogue(self):
        blocked = {token for token, _, _ in self.policy.blocked_agents(robots.AI_CRAWLERS)}
        self.assertEqual(blocked, {"gptbot", "ccbot"})
        self.assertEqual(self.policy.blocked_agents(robots.SEARCH_CRAWLERS), [])

    def test_wildcard_block_covers_every_agent(self):
        policy = robots.parse("User-agent: *\nDisallow: /\n")
        blocked = {t for t, _, _ in policy.blocked_agents(robots.AI_CRAWLERS)}
        self.assertEqual(blocked, set(robots.AI_CRAWLERS))

    def test_server_error_is_treated_as_disallow_all(self):
        policy = robots.unreadable("u", 503, "server error", server_error=True)
        self.assertFalse(policy.can_fetch("Any", "https://x.com/"))

    def test_missing_robots_allows_everything(self):
        self.assertTrue(robots.missing("u").can_fetch("Any", "https://x.com/"))

    def test_malformed_lines_do_not_crash(self):
        policy = robots.parse("garbage\nDisallow: /x\nUser-agent\nAllow: /y\n")
        self.assertTrue(policy.parse_warnings)
        self.assertTrue(policy.can_fetch("Any", "https://x.com/anything"))


class TestHtmlExtraction(unittest.TestCase):
    DOC = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>Acme Widgets - Fasteners</title>"
        '<meta name="description" content="Stainless fasteners.">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta property="og:site_name" content="Acme Widgets">'
        '<link rel="canonical" href="https://acme.test/">'
        '<script type="application/ld+json">{"@type":"Organization","name":"Acme"}</script>'
        "</head><body>"
        '<header><nav><a href="/about">About us</a><a href="/pricing">Pricing</a></nav></header>'
        "<main><h1>Marine fasteners</h1><p>Acme makes 316-grade fasteners in Leeds.</p>"
        '<h2>Range</h2><a class="btn" href="/quote">Request a quote</a>'
        '<a href="/x">Click here</a>'
        '<img src="/a.png" alt="A tray of bolts"><img src="/b.png"><img src="/c.png" alt="">'
        '<form action="/search"><label for="q">Search</label>'
        '<input id="q" name="q"><button>Go</button></form>'
        '<time datetime="2026-02-11">11 February 2026</time></main>'
        "<footer><p>&copy; 2026 Acme Widgets Ltd</p></footer></body></html>"
    )

    def setUp(self):
        self.ex = htmlparse.extract(self.DOC)
        self.d = self.ex.to_dict()

    def test_head_metadata(self):
        self.assertEqual(self.d["title"], "Acme Widgets - Fasteners")
        self.assertEqual(self.d["html_lang"], "en")
        self.assertEqual(self.d["canonical"], "https://acme.test/")
        self.assertEqual(self.d["meta_name"]["description"], "Stainless fasteners.")
        self.assertIn("viewport", self.d["meta_name"])
        self.assertEqual(self.d["meta_property"]["og:site_name"], "Acme Widgets")
        self.assertEqual(len(self.d["jsonld_blocks"]), 1)

    def test_regions_distinguish_navigation_from_content(self):
        regions = {a["text"]: a["region"] for a in self.d["anchors"]}
        self.assertEqual(regions["About us"], "nav")
        self.assertEqual(regions["Request a quote"], "main")

    def test_missing_alt_is_distinct_from_empty_alt(self):
        alts = {i["src"]: i["alt"] for i in self.d["images"]}
        self.assertEqual(alts["/a.png"], "A tray of bolts")
        self.assertIsNone(alts["/b.png"], "a missing alt attribute must be None")
        self.assertEqual(alts["/c.png"], "", "an explicit empty alt must be preserved")

    def test_structure_and_forms(self):
        self.assertEqual([h["level"] for h in self.d["headings"]], [1, 2])
        self.assertEqual(self.d["landmark_counts"]["main"], 1)
        self.assertTrue(self.d["forms"][0]["has_submit"])
        self.assertEqual(self.d["forms"][0]["labels"], 1)
        self.assertEqual(self.d["time_elements"][0]["datetime"], "2026-02-11")
        self.assertGreater(self.d["main_word_count"], 5)

    def test_button_like_and_generic_links(self):
        by_text = {a["text"]: a for a in self.d["anchors"]}
        self.assertTrue(by_text["Request a quote"]["button_like"])
        self.assertTrue(htmlparse.is_generic_link_text("Click here"))
        self.assertFalse(htmlparse.is_generic_link_text("Request a quote"))

    def test_malformed_html_degrades_gracefully(self):
        ex = htmlparse.extract("<html><head><title>Broken<body><p>Hello <b>world"
                               "<div>more<img src=x alt=>")
        self.assertEqual(ex.title, "Broken", "an unterminated <title> must close at <body>")
        self.assertIn("Hello world", ex.text)
        self.assertEqual(ex.parse_error, "")

    def test_empty_and_non_html_input(self):
        for text in ("", "   ", "{\"json\": true}", "%PDF-1.4 binary-ish"):
            with self.subTest(text=text[:12]):
                ex = htmlparse.extract(text)
                self.assertIsInstance(ex.to_dict(), dict)

    def test_client_render_shell_is_recognised(self):
        ex = htmlparse.extract(
            '<html><head><title>App</title></head><body><div id="root"></div>'
            '<script>window.__NEXT_DATA__={}</script>'
            '<script src="/a.js"></script></body></html>')
        self.assertIn("__NEXT_DATA__", ex.csr_markers)
        self.assertEqual(ex.empty_root_containers[0]["inner_text_chars"], 0)
        self.assertEqual(ex.main_word_count, 0)

    def test_microdata_and_rdfa_are_detected(self):
        ex = htmlparse.extract(
            '<div itemscope itemtype="https://schema.org/Product">'
            '<span typeof="Offer">x</span></div>')
        self.assertEqual(ex.microdata_types, ["https://schema.org/Product"])
        self.assertEqual(ex.rdfa_types, ["Offer"])


class TestSeverityModel(unittest.TestCase):
    def test_bands(self):
        cases = [
            (dict(impact_base=4, confidence="high",
                  breadth={"affected": 6, "applicable": 6}), "critical"),
            (dict(impact_base=3, confidence="high",
                  breadth={"affected": 12, "applicable": 12}), "critical"),
            (dict(impact_base=3, confidence="high",
                  breadth={"affected": 1, "applicable": 1}), "high"),
            (dict(impact_base=2, confidence="high",
                  breadth={"affected": 6, "applicable": 6}), "high"),
            (dict(impact_base=2, confidence="high",
                  breadth={"affected": 1, "applicable": 5}), "low"),
            (dict(impact_base=1, confidence="high",
                  breadth={"affected": 1, "applicable": 1}), "low"),
        ]
        for overrides, expected in cases:
            with self.subTest(**overrides):
                severity, _, _ = sev.severity_of(make_check(**overrides))
                self.assertEqual(severity, expected)

    def test_low_confidence_can_never_be_critical(self):
        severity, _, why = sev.severity_of(make_check(
            impact_base=4, confidence="low", breadth={"affected": 10, "applicable": 10}))
        self.assertEqual(severity, "medium")
        self.assertIn("capped", why)

    def test_medium_confidence_capped_at_high(self):
        severity, _, why = sev.severity_of(make_check(
            impact_base=4, confidence="medium", breadth={"affected": 10, "applicable": 10}))
        self.assertEqual(severity, "high")
        self.assertIn("capped", why)

    def test_single_unit_gets_no_breadth_bonus(self):
        self.assertEqual(sev.breadth_adjustment({"affected": 1, "applicable": 1}), 0)
        self.assertEqual(sev.breadth_adjustment(
            {"affected": 1, "applicable": 1, "unit": "sites"}), 0)
        self.assertEqual(sev.breadth_adjustment({"affected": 5, "applicable": 5}), 1)
        self.assertEqual(sev.breadth_adjustment({"affected": 1, "applicable": 10}), -1)

    def test_priority_promotion_is_narrow(self):
        wide = {"affected": 10, "applicable": 10, "unit": "pages"}
        narrow = {"affected": 1, "applicable": 10, "unit": "pages"}
        self.assertEqual(sev.remediation_priority("medium", "low", wide), "high")
        self.assertEqual(sev.remediation_priority("medium", "low", narrow), "medium")
        self.assertEqual(sev.remediation_priority("medium", "high", wide), "medium")
        self.assertEqual(sev.remediation_priority("high", "low", wide), "high")

    def test_rationale_shows_the_arithmetic(self):
        _, _, why = sev.severity_of(make_check(
            impact_base=3, confidence="medium", breadth={"affected": 4, "applicable": 5}))
        for fragment in ("impact 3", "breadth", "confidence medium", "score"):
            self.assertIn(fragment, why)


class TestReportSchema(unittest.TestCase):
    def valid_report(self):
        return {
            "site": "example.test",
            "audited_at": "2026-09-04T00:00:00Z",
            "summary": {"total_findings": 1, "critical": 0, "high": 1,
                        "medium": 0, "low": 0},
            "findings": [{
                "id": "F-001", "title": "A defect", "severity": "high",
                "evidence": "1 of 1 pages.",
                "suggested_action": {"summary": "Fix it.", "priority": "high"}}],
        }

    def test_valid_report_passes(self):
        self.assertEqual(schema.validate(self.valid_report()), [])

    def test_each_required_field_is_enforced(self):
        for field in ("site", "audited_at", "summary", "findings"):
            report = self.valid_report()
            del report[field]
            with self.subTest(field=field):
                self.assertTrue(schema.validate(report))

    def test_finding_fields_are_enforced(self):
        for field in ("id", "title", "severity", "evidence", "suggested_action"):
            report = self.valid_report()
            del report["findings"][0][field]
            with self.subTest(field=field):
                self.assertTrue(schema.validate(report))

    def test_counts_must_match(self):
        report = self.valid_report()
        report["summary"]["high"] = 5
        self.assertTrue(any("high" in e for e in schema.validate(report)))

    def test_repair_fixes_counts_ids_and_priority(self):
        report = self.valid_report()
        report["summary"] = {"total_findings": 99}
        report["findings"].append({"id": "F-999", "title": "", "severity": "urgent",
                                   "evidence": "", "suggested_action": {}})
        repaired = schema.validate_and_repair(report)
        self.assertTrue(repaired["report_validation"]["valid"],
                        repaired["report_validation"]["errors"])
        self.assertEqual([f["id"] for f in repaired["findings"]], ["F-001", "F-002"])
        self.assertEqual(repaired["summary"]["total_findings"], 2)
        self.assertEqual(repaired["findings"][1]["severity"], "low")
        self.assertTrue(repaired["findings"][1]["suggested_action"]["summary"])
        self.assertTrue(repaired["report_validation"]["repairs_applied"])

    def test_repair_survives_garbage(self):
        repaired = schema.validate_and_repair(
            {"findings": ["not an object", 42], "summary": "nope"})
        self.assertTrue(repaired["report_validation"]["valid"])
        self.assertEqual(repaired["findings"], [])

    def test_timestamp_must_be_iso8601(self):
        report = self.valid_report()
        report["audited_at"] = "4 September 2026"
        self.assertTrue(schema.validate(report))
        self.assertTrue(schema.validate_and_repair(report)["report_validation"]["valid"])


class TestBudget(unittest.TestCase):
    def test_reserves_gate_optional_work(self):
        generous = Budget(total=240.0)
        self.assertTrue(generous.allow_new_fetches())
        self.assertTrue(generous.allow_optional())
        self.assertTrue(generous.allow_analysis())

        tight = Budget(total=50.0)
        self.assertFalse(tight.allow_new_fetches())
        self.assertFalse(tight.allow_optional())
        self.assertTrue(tight.allow_analysis())

        expired = Budget(total=1.0)
        self.assertFalse(expired.allow_analysis())

    def test_timeouts_are_clamped_to_the_deadline(self):
        self.assertLessEqual(Budget(total=20.0).clamp_timeout(8.0), 8.0)
        self.assertGreaterEqual(Budget(total=2.0).clamp_timeout(8.0), 0.5)

    def test_marks_are_recorded(self):
        budget = Budget(total=10.0)
        budget.mark("crawl_ms")
        self.assertIn("crawl_ms", budget.marks())
        self.assertGreaterEqual(budget.duration_ms(), 0)


if __name__ == "__main__":
    unittest.main()
