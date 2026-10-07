"""Crawl behaviour against synthetic sites: safety, limits, determinism, failures.

Every test runs against an in-process fixture server, so nothing here depends on
a live third-party website. That is a correctness requirement (results must not
drift) and an ethical one (we should not rate-abuse someone else's server to
test our own politeness).
"""

from __future__ import annotations

import unittest

from fixture_server import FixtureSite, Route, RICH_PARAGRAPH, page
from helpers import crawl, snapshot_of

from auditlib.budget import Budget
from auditlib.crawler import score_candidate
from auditlib.fetcher import Fetcher


def sitemap(base, paths, lastmod=None):
    entries = "".join(
        "<url><loc>%s%s</loc>%s</url>"
        % (base, p, "<lastmod>%s</lastmod>" % lastmod if lastmod else "")
        for p in paths)
    return Route('<?xml version="1.0"?><urlset '
                 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % entries,
                 content_type="application/xml")


BASIC = {
    "/": Route(page("Home", body=RICH_PARAGRAPH +
                    '<a href="/about">About</a><a href="/pricing">Pricing</a>')),
    "/about": Route(page("About", body=RICH_PARAGRAPH)),
    "/pricing": Route(page("Pricing", body=RICH_PARAGRAPH)),
}


class TestSafety(unittest.TestCase):
    def test_only_get_and_head_are_ever_issued(self):
        with FixtureSite(BASIC) as site:
            snapshot_of(site.url)
            self.assertTrue(site.methods_used() <= {"GET", "HEAD"},
                            "the auditor issued %s" % site.methods_used())

    def test_read_only_fetcher_refuses_other_methods(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                with self.assertRaises(ValueError):
                    Fetcher().fetch("https://example.test/", method=method)

    def test_state_changing_urls_are_never_requested(self):
        routes = dict(BASIC)
        routes["/"] = Route(page("Home", body=RICH_PARAGRAPH +
                                 '<a href="/cart">Cart</a><a href="/logout">Log out</a>'
                                 '<a href="/account">Account</a><a href="/about">About</a>'))
        with FixtureSite(routes) as site:
            session = crawl(site.url)
            requested = " ".join(site.paths_requested())
            for forbidden in ("/cart", "/logout", "/account"):
                self.assertNotIn(forbidden, requested)
            self.assertGreaterEqual(len(session.skipped_unsafe), 3)
            self.assertGreaterEqual(
                session.snapshot()["coverage"]["pages_skipped_unsafe"], 3)

    def test_robots_disallow_is_obeyed_and_reported(self):
        routes = dict(BASIC)
        routes["/robots.txt"] = Route("User-agent: *\nDisallow: /pricing\n",
                                      content_type="text/plain")
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            self.assertNotIn("/pricing", " ".join(site.paths_requested()))
            self.assertEqual(snapshot["coverage"]["pages_skipped_by_robots"], 1)
            self.assertTrue(snapshot["coverage"]["robots_blocked_sample"])

    def test_robots_server_error_stops_the_crawl_entirely(self):
        routes = dict(BASIC)
        routes["/robots.txt"] = Route("boom", status=503, content_type="text/plain")
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            self.assertEqual(snapshot["robots"]["outcome"], "server_error")
            self.assertEqual(snapshot["coverage"]["pages_fetched"], 0,
                             "a 5xx robots.txt must be treated as disallow-all")

    def test_crawl_delay_is_honoured(self):
        routes = dict(BASIC)
        routes["/robots.txt"] = Route("User-agent: *\nCrawl-delay: 2\n",
                                      content_type="text/plain")
        with FixtureSite(routes) as site:
            session = crawl(site.url, budget=Budget(total=60.0))
            self.assertTrue(any("Crawl-delay" in n for n in session.notes))


class TestLimits(unittest.TestCase):
    def build_wide_site(self, count=40):
        links = "".join('<a href="/p%d">Page %d</a>' % (i, i) for i in range(count))
        routes = {"/": Route(page("Home", body=RICH_PARAGRAPH + links))}
        for i in range(count):
            routes["/p%d" % i] = Route(page("Page %d" % i, body=RICH_PARAGRAPH))
        return routes

    def test_page_limit_is_respected(self):
        with FixtureSite(self.build_wide_site()) as site:
            snapshot = snapshot_of(site.url, max_pages=7)
            self.assertLessEqual(len(snapshot["pages"]), 7)

    def test_depth_limit_is_respected(self):
        routes = {
            "/": Route(page("Home", body='<a href="/a">A</a>')),
            "/a": Route(page("A", body='<a href="/a/b">B</a>')),
            "/a/b": Route(page("B", body='<a href="/a/b/c">C</a>')),
            "/a/b/c": Route(page("C")),
        }
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url, max_depth=1)
            self.assertLessEqual(snapshot["coverage"]["max_depth_reached"], 1)
            self.assertNotIn("/a/b/c", " ".join(p["url"] for p in snapshot["pages"]))

    def test_body_byte_cap_truncates_rather_than_failing(self):
        huge = page("Huge", body="<p>%s</p>" % ("word " * 200000))
        with FixtureSite({"/": Route(huge)}) as site:
            fetcher = Fetcher(rate_per_sec=50, max_body_bytes=4096)
            snapshot = snapshot_of(site.url, fetcher=fetcher)
            self.assertTrue(snapshot["pages"][0]["truncated"])
            self.assertLessEqual(snapshot["pages"][0]["bytes"], 4096)

    def test_total_byte_budget_stops_further_fetching(self):
        with FixtureSite(self.build_wide_site(12)) as site:
            fetcher = Fetcher(rate_per_sec=50, max_total_bytes=3000)
            snapshot = snapshot_of(site.url, fetcher=fetcher)
            self.assertLessEqual(snapshot["http_stats"]["bytes"], 60000)
            self.assertTrue(snapshot["pages"])

    def test_link_probes_are_bounded_and_prioritised(self):
        links = "".join('<a href="/missing%d">Gone %d</a>' % (i, i) for i in range(50))
        routes = {"/": Route(page("Home", body=RICH_PARAGRAPH + links))}
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url, max_pages=1, max_link_probes=5)
            self.assertLessEqual(len(snapshot["link_checks"]), 5)
            self.assertTrue(all(not c["ok"] for c in snapshot["link_checks"]))

    def test_deadline_stops_new_fetches_but_still_yields_a_snapshot(self):
        with FixtureSite(self.build_wide_site(30)) as site:
            snapshot = snapshot_of(site.url, budget=Budget(total=8.0))
            self.assertIsInstance(snapshot["pages"], list)
            self.assertIn("coverage", snapshot)


class TestDeterminism(unittest.TestCase):
    def test_identical_site_yields_identical_page_selection(self):
        routes = {"/": Route(page("Home", body=RICH_PARAGRAPH + "".join(
            '<a href="/%s">%s</a>' % (s, s)
            for s in ("about", "pricing", "contact", "blog", "legal", "careers",
                      "docs", "shop", "team", "news"))))}
        for slug in ("about", "pricing", "contact", "blog", "legal", "careers",
                     "docs", "shop", "team", "news"):
            routes["/" + slug] = Route(page(slug.title(), body=RICH_PARAGRAPH))
        with FixtureSite(routes) as site:
            first = [p["url"] for p in snapshot_of(site.url, max_pages=6)["pages"]]
            second = [p["url"] for p in snapshot_of(site.url, max_pages=6)["pages"]]
            self.assertEqual(first, second)

    def test_selection_is_not_simply_document_order(self):
        """High-information pages must beat earlier low-value links."""
        body = ('<a href="/legal/cookie-policy">Cookies</a>'
                '<a href="/legal/terms">Terms</a>'
                '<a href="/legal/privacy">Privacy</a>'
                '<a href="/pricing">Pricing</a>')
        routes = {"/": Route(page("Home", body=RICH_PARAGRAPH + body))}
        for path in ("/legal/cookie-policy", "/legal/terms", "/legal/privacy", "/pricing"):
            routes[path] = Route(page(path, body=RICH_PARAGRAPH))
        with FixtureSite(routes) as site:
            # The fixture template also injects /about and /contact into <nav>,
            # which legitimately outrank both /pricing and the legal pages.
            snapshot = snapshot_of(site.url, max_pages=4)
            selected = {p["url"].split(site.url.rstrip("/"))[-1] for p in snapshot["pages"]}
            self.assertIn("/pricing", selected,
                          "priority scoring should pick /pricing over legal boilerplate")
            self.assertFalse([s for s in selected if s.startswith("/legal/")],
                             "legal boilerplate should not displace higher-value pages")

    def test_score_prefers_roles_shallow_depth_and_clean_urls(self):
        root = "https://a.test/"
        self.assertEqual(score_candidate(root, "", 0, in_nav=False, root=root), 100)
        about = score_candidate("https://a.test/about", "About", 1, in_nav=True, root=root)
        deep = score_candidate("https://a.test/a/b/c/d/e/f", "", 2, in_nav=False, root=root)
        trap = score_candidate("https://a.test/x?sort_by=1&a=2&b=3", "", 1,
                               in_nav=False, root=root)
        self.assertGreater(about, deep)
        self.assertGreater(about, trap)

    def test_unknown_vocabulary_is_not_penalised(self):
        """A site whose words we do not recognise is ranked by structure alone."""
        root = "https://a.test/"
        unknown = score_candidate("https://a.test/zzyzx", "Zzyzx", 1,
                                  in_nav=True, root=root)
        self.assertGreater(unknown, 0)
        self.assertGreater(unknown, score_candidate("https://a.test/a/b/c/d/e/zzyzx", "",
                                                    2, in_nav=False, root=root))


class TestFailureHandling(unittest.TestCase):
    def test_redirect_chain_is_recorded_not_followed_blindly(self):
        routes = {
            "/": Route("", status=301, location="/step2"),
            "/step2": Route("", status=302, location="/final"),
            "/final": Route(page("Final", body=RICH_PARAGRAPH)),
        }
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            # Pages are stored sorted by URL, so select the entry by depth.
            entry = next(p for p in snapshot["pages"] if p["depth"] == 0)
            self.assertEqual(entry["status"], 200)
            self.assertEqual(len(entry["redirects"]), 2)
            self.assertTrue(entry["url"].endswith("/final"))

    def test_trailing_slash_redirect_is_followed_not_treated_as_a_loop(self):
        """Very many sites 301 /about to /about/. That must resolve, not fail."""
        routes = {
            "/": Route(page("Home", body=RICH_PARAGRAPH + '<a href="/about">About</a>')),
            "/about": Route("", status=301, location="/about/"),
            "/about/": Route(page("About", body=RICH_PARAGRAPH)),
        }
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            about = [p for p in snapshot["pages"] if "/about" in p["url"]]
            self.assertTrue(about, "the /about page should have been crawled")
            self.assertEqual(about[0]["status"], 200)
            self.assertTrue(about[0]["url"].endswith("/about/"))
            self.assertEqual(snapshot["coverage"]["pages_fetched"], 2)

    def test_slash_variants_do_not_consume_two_crawl_slots(self):
        routes = {
            "/": Route(page("Home", body=RICH_PARAGRAPH +
                            '<a href="/about">About</a><a href="/about/">About again</a>')),
            "/about": Route(page("About", body=RICH_PARAGRAPH)),
            "/about/": Route(page("About", body=RICH_PARAGRAPH)),
        }
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            about = [p for p in snapshot["pages"] if "about" in p["url"]]
            self.assertEqual(len(about), 1, "slash variants must share one crawl slot")

    def test_redirect_loop_terminates(self):
        routes = {"/": Route("", status=302, location="/a"),
                  "/a": Route("", status=302, location="/")}
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            self.assertTrue(snapshot["pages"])

    def test_http_errors_are_recorded_not_raised(self):
        routes = {"/": Route(page("Home", body='<a href="/gone">Gone</a>'
                                               '<a href="/boom">Boom</a>' + RICH_PARAGRAPH)),
                  "/gone": Route("nope", status=404),
                  "/boom": Route("kaboom", status=500)}
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            statuses = {p["url"].rsplit("/", 1)[-1]: p["status"] for p in snapshot["pages"]}
            self.assertEqual(statuses.get("gone"), 404)
            self.assertIn(statuses.get("boom"), (500, None))

    def test_unreachable_host_produces_a_snapshot_with_errors(self):
        snapshot = snapshot_of("http://127.0.0.1:9/", total=12.0)
        self.assertEqual(snapshot["coverage"]["pages_fetched"], 0)
        self.assertIsInstance(snapshot["pages"], list)

    def test_non_html_response_is_not_parsed_as_html(self):
        routes = {"/": Route('{"hello": "world"}', content_type="application/json")}
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            self.assertTrue(snapshot["pages"][0]["extraction"].get("non_html"))

    def test_sitemap_seeds_pages_and_records_lastmod(self):
        with FixtureSite(BASIC) as site:
            base = site.url.rstrip("/")
            site.routes["/sitemap.xml"] = sitemap(
                base, ["/", "/about", "/pricing", "/deep"], lastmod="2026-01-05")
            site.routes["/deep"] = Route(page("Deep", body=RICH_PARAGRAPH))
            site.routes["/robots.txt"] = Route(
                "User-agent: *\nSitemap: %s/sitemap.xml\n" % base, content_type="text/plain")
            snapshot = snapshot_of(site.url)
            self.assertEqual(snapshot["sitemaps"][0]["url_count"], 4)
            self.assertEqual(snapshot["sitemaps"][0]["newest_lastmod"], "2026-01-05")
            self.assertIn("/deep", " ".join(p["url"] for p in snapshot["pages"]))

    def test_malformed_sitemap_still_yields_urls(self):
        with FixtureSite(BASIC) as site:
            base = site.url.rstrip("/")
            site.routes["/sitemap.xml"] = Route(
                "<urlset><url><loc>%s/about</loc></url>" % base,   # unclosed
                content_type="application/xml")
            snapshot = snapshot_of(site.url)
            self.assertGreaterEqual(snapshot["sitemaps"][0]["url_count"], 1)

    def test_soft_404_probe_detects_a_200_for_a_missing_page(self):
        with FixtureSite(BASIC, default_status=200,
                         not_found_body=page("Oops", body="<p>Nothing here.</p>")) as site:
            snapshot = snapshot_of(site.url)
            self.assertTrue(snapshot["not_found_probe"]["soft_404"])

    def test_llms_txt_is_probed(self):
        routes = dict(BASIC)
        routes["/llms.txt"] = Route("# Example\n> A site.\n", content_type="text/plain")
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
            self.assertTrue(snapshot["well_known"]["llms_txt"]["ok"])


if __name__ == "__main__":
    unittest.main()
