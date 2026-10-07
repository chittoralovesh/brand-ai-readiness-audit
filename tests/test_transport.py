"""Transport-failure classification: a broken network is not a broken website.

The governing rule, exercised from three levels here:

    NO TARGET RESPONSE  =>  NO TARGET-SPECIFIC FINDING.

A DNS failure, a refused connection, an unreachable network, a TLS environment
fault or a bare timeout tells us about the machine running the audit. It cannot
support a claim that a site is uncrawlable, that it publishes no sitemap, or
that its robots.txt is broken. Those conclusions require evidence *from the
target origin*: a real HTTP status, a retrieved robots.txt, a real 404.

Covered:
  * classification unit tests for every failure mode, per platform wording
  * analyzer-level gating on synthetic snapshots
  * end-to-end runs against unroutable hosts and fixture servers
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixture_server import FixtureSite, RICH_PARAGRAPH, Route, page
from helpers import SCRIPTS, checks_by_id, make_page, minimal_snapshot, run_analyzer, \
    snapshot_of, transport_block

from auditlib import schema
from auditlib.fetcher import TRANSPORT_ERROR_KINDS, classify_transport_error

RUN_AUDIT = SCRIPTS / "run_audit.py"

# RFC 2606 reserves `.invalid`, so this name can never resolve anywhere.
UNRESOLVABLE = "http://audit-probe-does-not-resolve-9c1f.invalid/"
# RFC 5737 TEST-NET-1: reserved for documentation, guaranteed unroutable.
UNROUTABLE = "http://192.0.2.1/"

BODY = "<p>%s</p>" % RICH_PARAGRAPH


def closed_port() -> int:
    """A local port with nothing listening on it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def run_audit(url, *extra, expect=0, timeout=180):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        completed = subprocess.run(
            [sys.executable, str(RUN_AUDIT), "--url", url, "--out", str(out),
             "--rate", "50", "--quiet"] + list(extra),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
        assert completed.returncode == expect, \
            "exit %d (expected %d): %s" % (completed.returncode, expect,
                                           completed.stderr[-600:])
        return json.loads(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Classification
# ---------------------------------------------------------------------------

class TestClassification(unittest.TestCase):
    """Each platform words these differently; all must land in the right bucket."""

    CASES = [
        ("dns", "[Errno 11001] getaddrinfo failed"),                       # Windows
        ("dns", "[Errno -2] Name or service not known"),                   # Linux
        ("dns", "[Errno 8] nodename nor servname provided"),               # macOS
        ("dns", "[Errno -3] Temporary failure in name resolution"),
        ("dns", "[Errno 11004] getaddrinfo failed"),
        ("connection_refused", "[Errno 111] Connection refused"),
        ("connection_refused",
         "[WinError 10061] No connection could be made because the target machine "
         "actively refused it"),
        ("network_unreachable", "[Errno 101] Network is unreachable"),
        ("network_unreachable", "[Errno 113] No route to host"),
        ("tls", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
        ("tls", "EOF occurred in violation of protocol (_ssl.c:1006)"),
        ("timeout", "The read operation timed out"),
        ("timeout", "timed out"),
        ("connection", "something entirely unfamiliar"),
    ]

    def test_every_failure_mode_is_named(self):
        for expected, reason in self.CASES:
            with self.subTest(reason=reason[:48]):
                self.assertEqual(classify_transport_error(reason), expected)

    def test_every_kind_is_a_recognised_transport_failure(self):
        for expected, _ in self.CASES:
            self.assertIn(expected, TRANSPORT_ERROR_KINDS)

    def test_tls_object_is_classified_even_without_matching_text(self):
        import ssl
        self.assertEqual(classify_transport_error(ssl.SSLError("odd")), "tls")

    def test_socket_timeout_object_is_classified(self):
        self.assertEqual(classify_transport_error(socket.timeout()), "timeout")


class TestLiveTransportFailures(unittest.TestCase):
    """Real sockets, no server. These must fail fast and be labelled correctly."""

    def test_dns_failure_is_recognised(self):
        session = snapshot_of(UNRESOLVABLE, total=30.0, timeout=4.0)
        transport = session["transport"]
        self.assertFalse(transport["target_responded"])
        self.assertEqual(transport["dominant_failure"], "dns")
        self.assertIn("dns", transport["transport_failures"])

    def test_connection_failure_is_a_transport_failure(self):
        session = snapshot_of("http://127.0.0.1:%d/" % closed_port(),
                              total=30.0, timeout=3.0)
        transport = session["transport"]
        self.assertFalse(transport["target_responded"])
        # Windows silently drops rather than sending RST, so the precise kind is
        # platform-dependent; what matters is that it is a transport failure.
        self.assertIn(transport["dominant_failure"], TRANSPORT_ERROR_KINDS)

    def test_unroutable_address_is_a_transport_failure(self):
        session = snapshot_of(UNROUTABLE, total=30.0, timeout=3.0)
        self.assertFalse(session["transport"]["target_responded"])
        self.assertIn(session["transport"]["dominant_failure"], TRANSPORT_ERROR_KINDS)

    def test_a_responding_server_reports_a_healthy_transport(self):
        with FixtureSite({"/": Route(page("Home", body=BODY))}) as site:
            transport = snapshot_of(site.url)["transport"]
        self.assertTrue(transport["target_responded"])
        self.assertGreater(transport["responses_received"], 0)
        self.assertEqual(transport["state"], "ok")


# ---------------------------------------------------------------------------
# 2. Analyzer gating on synthetic snapshots
# ---------------------------------------------------------------------------

def unreachable_snapshot(kind="dns", attempts=6):
    """A crawl in which nothing on the origin ever answered."""
    failures = {kind: attempts}
    return minimal_snapshot(
        pages=[],
        robots={"outcome": "unreachable", "url": "https://example.test/robots.txt",
                "status": None, "error_kind": kind, "error": "getaddrinfo failed",
                "sitemaps": [], "groups": [], "group_count": 0},
        sitemaps=[{"url": "https://example.test/sitemap.xml", "status": None,
                   "ok": False, "responded": False, "error_kind": kind,
                   "url_count": 0, "referenced_in_robots": False},
                  {"url": "https://example.test/sitemap_index.xml", "status": None,
                   "ok": False, "responded": False, "error_kind": kind,
                   "url_count": 0, "referenced_in_robots": False}],
        not_found_probe={"evaluated": True, "status": None, "responded": False,
                         "error_kind": kind, "word_count": 0,
                         "internal_link_count": 0, "url": "https://example.test/x",
                         "title": None, "soft_404": False, "redirected_to": "",
                         "redirect_hops": 0},
        transport=transport_block([], failures=failures,
                                  detail="No HTTP response was received from the "
                                         "target origin: DNS failure."))


class TestNoResponseYieldsNoFinding(unittest.TestCase):
    """The regression this patch exists for."""

    def setUp(self):
        self.checks = checks_by_id(
            run_analyzer("crawl-render-audit", unreachable_snapshot()))

    def test_entry_reachability_is_inconclusive_not_critical(self):
        self.assertEqual(self.checks["CR-01"]["status"], "INCONCLUSIVE")
        self.assertIn("No HTTP response", self.checks["CR-01"]["applicability"])

    def test_robots_is_not_evaluated_not_broken(self):
        self.assertEqual(self.checks["CR-03"]["status"], "NOT_EVALUATED")

    def test_sitemap_is_not_evaluated_not_missing(self):
        self.assertEqual(self.checks["CR-27"]["status"], "NOT_EVALUATED")

    def test_bot_wall_is_not_evaluated(self):
        self.assertEqual(self.checks["CR-02"]["status"], "NOT_EVALUATED")

    def test_no_check_fails_at_all(self):
        failed = [c["check_id"] for c in self.checks.values() if c["status"] == "FAIL"]
        self.assertEqual(failed, [],
                         "a transport failure produced site findings: %s" % failed)

    def test_holds_for_every_transport_failure_mode(self):
        for kind in ("dns", "connection_refused", "network_unreachable", "tls",
                     "timeout", "connection"):
            with self.subTest(kind=kind):
                checks = checks_by_id(run_analyzer(
                    "crawl-render-audit", unreachable_snapshot(kind)))
                failed = [c["check_id"] for c in checks.values()
                          if c["status"] == "FAIL"]
                self.assertEqual(failed, [])

    def test_every_analyzer_stays_silent(self):
        snapshot = unreachable_snapshot()
        for skill in ("crawl-render-audit", "ai-citability-audit",
                      "freshness-corroboration", "engagement-audit"):
            with self.subTest(skill=skill):
                document = run_analyzer(skill, snapshot)
                failed = [c["check_id"] for c in document["checks"]
                          if c["status"] == "FAIL"]
                self.assertEqual(failed, [])
                self.assertEqual(document["errors"], [])


class TestRealResponsesStillProduceFindings(unittest.TestCase):
    """The other half: genuine target evidence must still be reported."""

    def checks_for(self, snapshot):
        return checks_by_id(run_analyzer("crawl-render-audit", snapshot))

    def test_http_403_is_a_blocking_finding(self):
        checks = self.checks_for(minimal_snapshot(
            pages=[make_page(status=403, html=None)]))
        self.assertEqual(checks["CR-01"]["status"], "FAIL")
        self.assertEqual(checks["CR-02"]["status"], "FAIL")
        self.assertIn("403", checks["CR-01"]["evidence"])

    def test_http_429_is_a_blocking_finding(self):
        checks = self.checks_for(minimal_snapshot(
            pages=[make_page(status=429, html=None)]))
        self.assertEqual(checks["CR-02"]["status"], "FAIL")

    def test_captcha_interstitial_is_a_blocking_finding(self):
        challenge = ("<!doctype html><html><head><title>Just a moment...</title></head>"
                     "<body><h1>Checking your browser before accessing</h1>"
                     "<p>Please verify you are human to continue.</p></body></html>")
        checks = self.checks_for(minimal_snapshot(
            pages=[make_page(status=200, html=challenge)]))
        self.assertEqual(checks["CR-02"]["status"], "FAIL")
        self.assertIn("interstitial", checks["CR-02"]["evidence"])

    def test_retrieved_robots_with_restrictive_directives_is_a_finding(self):
        snapshot = minimal_snapshot(pages=[make_page(html=page("Home", body=BODY))])
        snapshot["robots"] = {"outcome": "ok", "status": 200, "error_kind": "",
                              "url": "https://example.test/robots.txt", "sitemaps": [],
                              "group_count": 1,
                              "groups": [{"agents": ["*"], "blocks_everything": True,
                                          "crawl_delay": None,
                                          "rules": [{"allow": False, "pattern": "/"}]}]}
        snapshot["robots_agent_matrix"] = {
            "gptbot": {"kind": "ai", "purpose": "OpenAI", "allowed_at_root": False,
                       "has_explicit_group": False, "group_blocks_everything": True},
            "googlebot": {"kind": "search", "purpose": "Google", "allowed_at_root": False,
                          "has_explicit_group": False, "group_blocks_everything": True},
        }
        checks = self.checks_for(snapshot)
        self.assertEqual(checks["CR-04"]["status"], "FAIL")
        self.assertEqual(checks["CR-05"]["status"], "FAIL")

    def test_robots_5xx_from_the_server_is_still_a_finding(self):
        snapshot = minimal_snapshot(pages=[make_page(html=page("Home", body=BODY))])
        snapshot["robots"] = {"outcome": "server_error", "status": 503,
                              "error_kind": "http", "error": "server error",
                              "url": "https://example.test/robots.txt",
                              "sitemaps": [], "groups": [], "group_count": 0}
        checks = self.checks_for(snapshot)
        self.assertEqual(checks["CR-03"]["status"], "FAIL")
        self.assertEqual(checks["CR-03"]["impact_base"], 4)

    def test_sitemap_404_from_the_server_is_a_missing_sitemap_finding(self):
        snapshot = minimal_snapshot(
            pages=[make_page(html=page("Home", body=BODY))],
            sitemaps=[{"url": "https://example.test/sitemap.xml", "status": 404,
                       "ok": False, "responded": True, "error_kind": "",
                       "url_count": 0, "referenced_in_robots": False}])
        checks = self.checks_for(snapshot)
        self.assertEqual(checks["CR-27"]["status"], "FAIL")
        self.assertIn("404", checks["CR-27"]["evidence"])

    def test_sitemap_without_a_response_is_not_a_missing_sitemap_finding(self):
        snapshot = minimal_snapshot(
            pages=[make_page(html=page("Home", body=BODY))],
            sitemaps=[{"url": "https://example.test/sitemap.xml", "status": None,
                       "ok": False, "responded": False, "error_kind": "timeout",
                       "url_count": 0, "referenced_in_robots": False}])
        checks = self.checks_for(snapshot)
        self.assertEqual(checks["CR-27"]["status"], "NOT_EVALUATED")
        self.assertIn("timeout", checks["CR-27"]["applicability"])

    def test_link_probes_without_a_response_are_not_broken_links(self):
        snapshot = minimal_snapshot(
            pages=[make_page(html=page("Home", body=BODY))],
            link_checks=[{"url": "https://example.test/a", "status": None, "ok": False,
                          "method": "HEAD", "redirect_hops": 0, "final_url": "",
                          "error": "timed out", "error_kind": "timeout",
                          "reference_count": 1, "referenced_from": []}])
        self.assertEqual(self.checks_for(snapshot)["CR-25"]["status"], "NOT_EVALUATED")

    def test_link_probes_with_a_404_are_broken_links(self):
        snapshot = minimal_snapshot(
            pages=[make_page(html=page("Home", body=BODY))],
            link_checks=[{"url": "https://example.test/a", "status": 404, "ok": False,
                          "method": "HEAD", "redirect_hops": 0, "final_url": "",
                          "error": "", "error_kind": "", "reference_count": 3,
                          "referenced_from": [{"from": "https://example.test/",
                                               "text": "A", "region": "nav"}]}])
        self.assertEqual(self.checks_for(snapshot)["CR-25"]["status"], "FAIL")


# ---------------------------------------------------------------------------
# 3. End to end
# ---------------------------------------------------------------------------

class TestEndToEndTransport(unittest.TestCase):
    def assert_no_site_findings(self, report, label):
        self.assertEqual(schema.validate(report), [])
        self.assertEqual(report["summary"]["total_findings"], 0,
                         "%s produced findings: %s"
                         % (label, [(f["check_id"], f["title"])
                                    for f in report["findings"]]))
        scopes = {l["scope"] for l in report["limitations"]}
        self.assertIn("transport", scopes,
                      "%s must record a transport coverage limitation" % label)
        self.assertFalse(report["audit_metrics"]["target_responded"])

    def test_dns_failure(self):
        self.assert_no_site_findings(
            run_audit(UNRESOLVABLE, "--budget", "40", "--timeout", "4"), "DNS failure")

    def test_connection_refused(self):
        self.assert_no_site_findings(
            run_audit("http://127.0.0.1:%d/" % closed_port(),
                      "--budget", "40", "--timeout", "3"), "connection failure")

    def test_generic_timeout_against_an_unroutable_host(self):
        self.assert_no_site_findings(
            run_audit(UNROUTABLE, "--budget", "40", "--timeout", "3"), "timeout")

    def test_complete_local_network_outage(self):
        """Every request fails at the transport layer, for the whole run."""
        report = run_audit(UNRESOLVABLE, "--budget", "40", "--timeout", "3")
        self.assert_no_site_findings(report, "network outage")
        self.assertEqual(report["audit_metrics"]["transport_state"], "dns")
        limitation = next(l for l in report["limitations"] if l["scope"] == "transport")
        self.assertIn("cannot be derived", limitation["reason"])

    def test_http_403_still_produces_findings(self):
        with FixtureSite({"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                               content_type="text/plain")},
                         default_status=403,
                         not_found_body="<html><body>Access denied</body></html>") as site:
            report = run_audit(site.url)
        self.assertEqual(schema.validate(report), [])
        self.assertTrue(report["audit_metrics"]["target_responded"])
        found = {f["check_id"] for f in report["findings"]}
        self.assertTrue({"CR-01", "CR-02"} & found,
                        "a real 403 from the origin must still be reported")

    def test_http_429_still_produces_findings(self):
        with FixtureSite({"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                               content_type="text/plain")},
                         default_status=429,
                         not_found_body="<html><body>Too many requests</body></html>") as site:
            report = run_audit(site.url)
        self.assertEqual(schema.validate(report), [])
        self.assertTrue({"CR-01", "CR-02"} &
                        {f["check_id"] for f in report["findings"]})

    def test_captcha_response_still_produces_findings(self):
        challenge = ("<!doctype html><html><head><title>Just a moment...</title></head>"
                     "<body><p>Checking your browser before accessing the site. "
                     "Please verify you are human.</p></body></html>")
        with FixtureSite({"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                               content_type="text/plain"),
                          "/": Route(challenge)}) as site:
            report = run_audit(site.url)
        self.assertEqual(schema.validate(report), [])
        self.assertIn("CR-02", {f["check_id"] for f in report["findings"]})

    def test_robots_disallow_still_produces_findings(self):
        with FixtureSite({"/robots.txt": Route("User-agent: *\nDisallow: /\n",
                                               content_type="text/plain"),
                          "/": Route(page("Home", body=BODY))}) as site:
            report = run_audit(site.url)
        self.assertEqual(schema.validate(report), [])
        found = {f["check_id"] for f in report["findings"]}
        self.assertIn("CR-04", found)
        self.assertIn("CR-05", found)

    def test_sitemap_404_still_produces_a_missing_sitemap_finding(self):
        with FixtureSite({"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                               content_type="text/plain"),
                          "/": Route(page("Home", body=BODY))}) as site:
            report = run_audit(site.url)
        self.assertEqual(schema.validate(report), [])
        sitemap = [f for f in report["findings"] if f["check_id"] == "CR-27"]
        self.assertTrue(sitemap, "a real 404 for every sitemap candidate is a finding")
        self.assertIn("404", sitemap[0]["evidence"])


class TestABotWallsPageIsNotTheSitesPage(unittest.TestCase):
    """A challenge page answers every path, including the not-found probe.

    Found live: a healthcare site and a restaurant chain both returned an
    "Access Denied" interstitial for the missing-page probe, and `EN-07` read it
    as *their* 404 page, reporting that their error page left visitors with no
    way back. No human following a stale link would ever see that page. The
    blocking is real and is reported by `CR-02`; what cannot be concluded from a
    WAF's output is anything about the site's own error handling.
    """

    def probe_status(self, **probe):
        base = {"evaluated": True, "status": 404, "blocked": False,
                "title": "Page not found", "word_count": 30,
                "internal_link_count": 0, "url": "https://example.test/missing"}
        base.update(probe)
        document = run_analyzer("engagement-audit",
                                minimal_snapshot(not_found_probe=base))
        return checks_by_id(document)["EN-07"]

    def test_a_refusal_status_is_inconclusive(self):
        for status in (401, 403, 407, 429, 451):
            with self.subTest(status=status):
                check = self.probe_status(status=status, blocked=True,
                                          title="Access Denied")
                self.assertEqual(check["status"], "INCONCLUSIVE")
                self.assertIn("CR-02", check["applicability"])

    def test_a_challenge_title_behind_a_200_is_inconclusive(self):
        check = self.probe_status(status=200, title="Just a moment...")
        self.assertEqual(check["status"], "INCONCLUSIVE")

    def test_the_crawler_flags_a_blocked_probe(self):
        check = self.probe_status(status=200, blocked=True, title="Home")
        self.assertEqual(check["status"], "INCONCLUSIVE")

    def test_a_genuinely_bare_404_is_still_a_finding(self):
        """The guard must not swallow the real defect it sits next to."""
        check = self.probe_status(status=404, title="Page not found")
        self.assertEqual(check["status"], "FAIL")

    def test_a_helpful_404_still_passes(self):
        check = self.probe_status(status=404, internal_link_count=9, word_count=80)
        self.assertEqual(check["status"], "PASS")

    def test_a_search_box_counts_as_a_route_back(self):
        """Found live: a major marketplace's 404 carries a search box and a link
        home, and was reported as leaving visitors with no way back."""
        check = self.probe_status(status=404, internal_link_count=2, has_search=True)
        self.assertEqual(check["status"], "PASS")
        self.assertIn("search box", check["evidence"])

    def test_a_search_box_alone_with_no_links_is_still_a_finding(self):
        check = self.probe_status(status=404, internal_link_count=0, has_search=True)
        self.assertEqual(check["status"], "FAIL")

    def test_a_live_bot_walled_site_reports_the_block_not_the_error_page(self):
        blocked = "<html><head><title>Access Denied</title></head><body>" \
                  "<p>Request blocked.</p></body></html>"
        with FixtureSite({"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                               content_type="text/plain")},
                         default_status=403, not_found_body=blocked) as site:
            report = run_audit(site.url)
        self.assertEqual(schema.validate(report), [])
        found = {f["check_id"] for f in report["findings"]}
        self.assertIn("CR-02", found)
        self.assertNotIn("EN-07", found,
                         "a WAF interstitial is not the site's own error page")


if __name__ == "__main__":
    unittest.main()
