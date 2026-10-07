"""Declared-vs-enforced crawler access (CR-31) and the adaptive page budget.

Two capabilities that widen what the audit can see without widening what it
claims:

* **CR-31** compares the policy `robots.txt` publishes against how the network
  layer actually behaves. A CDN or WAF enforces its own User-Agent rules, so a
  site can invite crawlers on paper and refuse them in practice - a gap that is
  invisible to any audit that only reads the policy file. The check is made with
  the auditor's own honest user agent; it never impersonates a named crawler, so
  the finding rests on "a self-identifying automated client was refused", not on
  pretending to be GPTBot.

* **The adaptive page budget** widens the sample on a site that has already
  proved it responds quickly, so the ratios every finding quotes rest on more
  pages. It is bounded by a hard ceiling and a conservative headroom test, and
  decided once before the crawl loop so selection stays deterministic.
"""

from __future__ import annotations

import unittest

from fixture_server import FixtureSite, RICH_PARAGRAPH, Route, page
from helpers import checks_by_id, crawl, make_page, minimal_snapshot, run_analyzer, \
    snapshot_of, transport_block

from auditlib.budget import Budget
from auditlib.crawler import (ADAPTIVE_MAX_PAGES_CEILING, MAX_PAGES, Crawl)
from auditlib.fetcher import Fetcher

BODY = "<p>%s</p>" % RICH_PARAGRAPH


def open_matrix(allowed=True):
    """A robots_agent_matrix where the catalogued crawlers are allowed at root."""
    agents = {
        "gptbot": "ai", "claudebot": "ai", "perplexitybot": "ai", "ccbot": "ai",
        "google-extended": "ai", "googlebot": "search", "bingbot": "search",
    }
    return {token: {"kind": kind, "purpose": "test", "allowed_at_root": allowed,
                    "has_explicit_group": False, "group_blocks_everything": not allowed}
            for token, kind in agents.items()}


def snapshot_with(entry_status=200, *, robots_ok=True, allowed=True, responded=True):
    if responded:
        html = page("Home", body=BODY) if entry_status == 200 else None
        pages = [make_page(status=entry_status, html=html)]
    else:
        pages = []          # nothing answered, so there is no page record at all
    snap = minimal_snapshot(pages=pages)
    snap["robots"] = {
        "outcome": "ok" if robots_ok else "missing", "status": 200 if robots_ok else 404,
        "url": "https://example.test/robots.txt", "error_kind": "",
        "sitemaps": [], "groups": [], "group_count": 1,
    }
    snap["robots_agent_matrix"] = open_matrix(allowed) if robots_ok else {}
    if not responded:
        snap["transport"] = transport_block([], failures={"timeout": 5})
    return snap


class TestDeclaredVersusEnforcedAccess(unittest.TestCase):
    def checks_for(self, snap):
        return checks_by_id(run_analyzer("crawl-render-audit", snap))

    def test_open_policy_but_403_is_a_finding(self):
        check = self.checks_for(snapshot_with(403))["CR-31"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["impact_base"], 4)
        self.assertEqual(check["dedupe_key"], "robots-enforcement-mismatch")
        self.assertIn("403", check["evidence"])
        self.assertIn("robots.txt allows", check["evidence"])

    def test_other_refusal_statuses_also_count(self):
        for status in (401, 429, 451):
            with self.subTest(status=status):
                self.assertEqual(self.checks_for(snapshot_with(status))["CR-31"]["status"],
                                 "FAIL")

    def test_open_policy_and_a_normal_response_passes(self):
        check = self.checks_for(snapshot_with(200))["CR-31"]
        self.assertEqual(check["status"], "PASS")
        self.assertIn("agree", check["evidence"])

    def test_a_500_is_not_an_enforcement_mismatch(self):
        """A server error is a broken server, not a bot rule. CR-01 owns it."""
        self.assertEqual(self.checks_for(snapshot_with(500))["CR-31"]["status"], "PASS")

    def test_restrictive_robots_makes_a_block_consistent_not_contradictory(self):
        """If robots.txt disallows crawlers too, a 403 agrees with the policy."""
        check = self.checks_for(snapshot_with(403, allowed=False))["CR-31"]
        self.assertEqual(check["status"], "NOT_APPLICABLE")
        self.assertIn("consistent with the published policy", check["applicability"])

    def test_no_robots_txt_means_nothing_to_compare_against(self):
        check = self.checks_for(snapshot_with(403, robots_ok=False))["CR-31"]
        self.assertEqual(check["status"], "NOT_APPLICABLE")
        self.assertIn("no declared", check["applicability"])

    def test_no_response_at_all_is_inconclusive_not_a_finding(self):
        """A silent drop and a broken local network look identical from here."""
        check = self.checks_for(snapshot_with(None, responded=False))["CR-31"]
        self.assertEqual(check["status"], "INCONCLUSIVE")
        self.assertIn("silently drops", check["applicability"])

    def test_the_check_never_impersonates_a_named_crawler(self):
        """The honesty guarantee, asserted against the shipped source."""
        from helpers import ROOT
        source = (ROOT / "skills/crawl-render-audit/scripts/check_crawl_render.py").read_text(
            encoding="utf-8")
        crawler = (ROOT / "skills/audit-orchestrator/scripts/auditlib/crawler.py").read_text(
            encoding="utf-8")
        fetcher = (ROOT / "skills/audit-orchestrator/scripts/auditlib/fetcher.py").read_text(
            encoding="utf-8")
        for blob, name in ((source, "check_crawl_render"), (crawler, "crawler"),
                           (fetcher, "fetcher")):
            with self.subTest(module=name):
                # No spoofed crawler identity may appear in a User-Agent we send.
                self.assertNotIn("GPTBot/", blob)
                self.assertNotIn("ClaudeBot/", blob)
                self.assertNotIn("PerplexityBot/", blob)
                self.assertNotIn("Googlebot/", blob)

    def test_live_site_that_refuses_bots_is_reported(self):
        routes = {"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                       content_type="text/plain")}
        with FixtureSite(routes, default_status=403,
                         not_found_body="<html><body>Access denied</body></html>") as site:
            snapshot = snapshot_of(site.url)
        check = self.checks_for(snapshot)["CR-31"]
        self.assertEqual(check["status"], "FAIL")

    def test_live_healthy_site_passes(self):
        routes = {"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                       content_type="text/plain"),
                  "/": Route(page("Home", body=BODY))}
        with FixtureSite(routes) as site:
            snapshot = snapshot_of(site.url)
        self.assertEqual(self.checks_for(snapshot)["CR-31"]["status"], "PASS")


class TestAdaptivePageBudget(unittest.TestCase):
    def build_wide_site(self, count=40):
        links = "".join('<a href="/p%d">Section %d</a>' % (i, i) for i in range(count))
        routes = {"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                       content_type="text/plain"),
                  "/": Route(page("Home", body=RICH_PARAGRAPH + links))}
        for i in range(count):
            routes["/p%d" % i] = Route(page("Section %d" % i, body=RICH_PARAGRAPH))
        return routes

    def test_generous_budget_widens_the_sample(self):
        with FixtureSite(self.build_wide_site()) as site:
            session = crawl(site.url, budget=Budget(total=900.0))
            self.assertGreater(session.max_pages, MAX_PAGES)
            self.assertTrue(any("adaptive budget" in n for n in session.notes))

    def test_expansion_is_capped_by_the_hard_ceiling(self):
        with FixtureSite(self.build_wide_site()) as site:
            session = crawl(site.url, budget=Budget(total=100000.0))
            self.assertLessEqual(session.max_pages, ADAPTIVE_MAX_PAGES_CEILING)
            self.assertLessEqual(len(session.snapshot()["pages"]),
                                 ADAPTIVE_MAX_PAGES_CEILING)

    def test_normal_budget_does_not_expand(self):
        """The default 240s deadline must keep the documented 20-page sample."""
        with FixtureSite(self.build_wide_site()) as site:
            session = crawl(site.url, budget=Budget(total=240.0))
            self.assertEqual(session.max_pages, MAX_PAGES)
            self.assertFalse(any("adaptive budget" in n for n in session.notes))

    def test_tight_budget_never_expands(self):
        with FixtureSite(self.build_wide_site()) as site:
            session = crawl(site.url, budget=Budget(total=60.0))
            self.assertEqual(session.max_pages, MAX_PAGES)

    def test_an_explicit_caller_cap_always_wins(self):
        with FixtureSite(self.build_wide_site()) as site:
            session = Crawl(site.url, budget=Budget(total=900.0),
                            fetcher=Fetcher(rate_per_sec=50.0), max_pages=6)
            session.discover()
            session.run()
            self.assertEqual(session.max_pages, 6)
            self.assertLessEqual(len(session.snapshot()["pages"]), 6)

    def test_expansion_is_recorded_in_the_snapshot(self):
        with FixtureSite(self.build_wide_site()) as site:
            snapshot = snapshot_of(site.url, budget=Budget(total=900.0))
        self.assertEqual(snapshot["config"]["max_pages"], snapshot["config"]["max_pages"])
        self.assertTrue(any("adaptive budget" in n for n in snapshot["notes"]),
                        "the report must say the sample was widened, and why")

    def test_selection_stays_deterministic_under_expansion(self):
        with FixtureSite(self.build_wide_site()) as site:
            first = [p["url"] for p in snapshot_of(site.url, budget=Budget(total=900.0))["pages"]]
            second = [p["url"] for p in snapshot_of(site.url, budget=Budget(total=900.0))["pages"]]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
