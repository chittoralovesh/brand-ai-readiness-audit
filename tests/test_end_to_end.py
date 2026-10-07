"""Full-pipeline behaviour through the entrypoint script.

These exercise the whole marketplace the way a grading harness would: point the
entrypoint at a site and read the report it emits. They cover the happy path,
determinism, degradation when parts of the system fail, and the requirement that
a valid report is always produced.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fixture_server import FixtureSite, Route, RICH_PARAGRAPH, page
from helpers import ROOT, SCRIPTS

from auditlib import schema

RUN_AUDIT = SCRIPTS / "run_audit.py"
FINALIZE = SCRIPTS / "finalize_report.py"
CRAWL_SITE = SCRIPTS / "crawl_site.py"
COMPOSE = SCRIPTS / "compose_report.py"

ARTICLE = ("<p>Published 12 January 2026 by Dr Ana Ruiz.</p>" + RICH_PARAGRAPH * 4)
PRODUCT = ("<p>The Ward Planner is licensed per ward. $1,290 per ward per year. "
           "Add to cart to start a 30-day evaluation. SKU NW-WP-01. In stock.</p>"
           + RICH_PARAGRAPH)

DEFECTIVE_SITE = {
    "/robots.txt": Route("User-agent: *\nDisallow: /internal/\n\n"
                         "User-agent: GPTBot\nDisallow: /\n\n"
                         "User-agent: CCBot\nDisallow: /\n", content_type="text/plain"),
    "/": Route(page("Northwind Analytics", h1="Northwind Analytics",
                    body="<p>Empowering next-generation outcomes.</p>"
                         '<a href="/about">Read more</a>'
                         '<a href="/products/planner">Click here</a>'
                         '<a href="/blog/forecasting">Learn more</a>'
                         '<a href="/broken">More</a>'
                         '<a href="/internal/secret">Internal</a>'
                         '<img src="/a.png"><img src="/b.png"><img src="/c.png">',
                    head='<script type="application/ld+json">'
                         '{"@type":"Organization","name":"Northwind",}</script>')),
    "/about": Route(page("About | Northwind Analytics", body=RICH_PARAGRAPH)),
    "/products/planner": Route(page("Ward Planner | Northwind Analytics", body=PRODUCT)),
    "/blog/forecasting": Route(page("Forecasting | Northwind Analytics", body=ARTICLE)),
    "/contact": Route(page("Contact | Northwind Analytics",
                           body="<p>Call 020 7946 0000. 12 Harbour Street, Leeds LS1 4AB. "
                                "Opening hours 9-5.</p>")),
    "/broken": Route("gone", status=404),
    "/internal/secret": Route(page("Secret")),
}

HEALTHY_SITE = {
    "/robots.txt": Route("User-agent: *\nAllow: /\n", content_type="text/plain"),
    "/llms.txt": Route("# Northwind\n> Workforce planning.\n", content_type="text/plain"),
    "/": Route(page("Workforce planning for hospitals | Northwind Analytics",
                    h1="Workforce planning for hospitals",
                    body=RICH_PARAGRAPH * 2 +
                         '<a class="btn" href="/pricing">See pricing</a>'
                         '<a href="/about">How our forecasting works</a>'
                         '<a href="/contact">Contact the team</a>'
                         '<a href="/privacy">Privacy policy</a>'
                         '<img src="/a.png" alt="A staffing plan">',
                    head='<meta property="og:title" content="Northwind Analytics">'
                         '<meta property="og:description" content="Workforce planning.">'
                         '<meta property="og:site_name" content="Northwind Analytics">'
                         '<script type="application/ld+json">'
                         '{"@context":"https://schema.org","@type":"Organization",'
                         '"@id":"http://x/#org","name":"Northwind Analytics",'
                         '"url":"http://x/","logo":"http://x/l.png",'
                         '"description":"Workforce planning for hospitals",'
                         '"sameAs":["https://www.linkedin.com/company/northwind"]}'
                         "</script>")),
    "/about": Route(page("About | Northwind Analytics",
                         body=RICH_PARAGRAPH * 2 +
                              "<p>Northwind Analytics is a healthcare software company "
                              "based at 12 Harbour Street, Leeds LS1 4AB, founded in 2014. "
                              "Registered in England, company number 09112233.</p>"
                              '<a href="/pricing">See pricing</a>')),
    "/pricing": Route(page("Pricing | Northwind Analytics",
                           body=RICH_PARAGRAPH * 2 + '<a href="/contact">Get a quote</a>')),
    "/contact": Route(page("Contact | Northwind Analytics",
                           body=RICH_PARAGRAPH + "<p>Call 020 7946 0000.</p>"
                                '<a href="/about">About us</a>')),
    "/privacy": Route(page("Privacy | Northwind Analytics",
                           body=RICH_PARAGRAPH + '<a href="/">Home</a>')),
}


# A well-built site also returns a helpful 404 inside the normal layout.
GOOD_404 = page("Page not found | Northwind Analytics",
                h1="We could not find that page",
                body="<p>The page you asked for has moved or never existed. "
                     "Try one of these instead.</p>"
                     '<a href="/">Home</a><a href="/pricing">Pricing</a>'
                     '<a href="/contact">Contact us</a>')


def run_audit(url, *extra, expect=0, timeout=180):
    """Invoke the entrypoint the way a harness would, returning the report."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        command = [sys.executable, str(RUN_AUDIT), "--url", url, "--out", str(out),
                   "--rate", "50", "--quiet"] + list(extra)
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout)
        assert completed.returncode == expect, \
            "exit %d (expected %d): %s" % (completed.returncode, expect,
                                           completed.stderr[-800:])
        return json.loads(out.read_text(encoding="utf-8"))


class TestHappyPath(unittest.TestCase):
    def test_defective_site_produces_a_valid_prioritised_report(self):
        with FixtureSite(DEFECTIVE_SITE) as site:
            base = site.url.rstrip("/")
            site.routes["/sitemap.xml"] = Route(
                '<?xml version="1.0"?><urlset '
                'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                + "".join("<url><loc>%s%s</loc></url>" % (base, p)
                          for p in ("/", "/about", "/products/planner",
                                    "/blog/forecasting", "/contact", "/hidden"))
                + "</urlset>", content_type="application/xml")
            site.routes["/hidden"] = Route(page("Hidden", body=RICH_PARAGRAPH))
            report = run_audit(site.url)

            self.assertEqual(schema.validate(report), [])
            self.assertTrue(report["report_validation"]["valid"])
            self.assertGreater(report["summary"]["total_findings"], 5)
            self.assertTrue(site.methods_used() <= {"GET", "HEAD"})

            found = {f["check_id"] for f in report["findings"]}
            for expected in ("CR-04", "AC-03", "AC-06", "AC-07", "FC-01"):
                self.assertIn(expected, found,
                              "%s should be detected on this fixture" % expected)

            for finding in report["findings"]:
                self.assertTrue(finding["evidence"])
                self.assertTrue(finding["suggested_action"]["summary"])
                self.assertTrue(finding["mechanism"])
                self.assertIn(finding["severity"],
                              ("critical", "high", "medium", "low"))

            self.assertEqual([f["priority_rank"] for f in report["findings"]],
                             list(range(1, len(report["findings"]) + 1)))
            self.assertEqual(report["engine"]["read_only"], True)
            self.assertGreater(report["audit_metrics"]["http_requests"], 0)

    def test_healthy_site_produces_few_findings_and_no_false_alarms(self):
        with FixtureSite(HEALTHY_SITE, not_found_body=GOOD_404) as site:
            base = site.url.rstrip("/")
            site.routes["/robots.txt"] = Route(
                "User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % base,
                content_type="text/plain")
            site.routes["/sitemap.xml"] = Route(
                '<?xml version="1.0"?><urlset '
                'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                + "".join("<url><loc>%s%s</loc><lastmod>2026-08-0%d</lastmod></url>"
                          % (base, p, i + 1)
                          for i, p in enumerate(("/", "/about", "/pricing",
                                                 "/contact", "/privacy")))
                + "</urlset>", content_type="application/xml")
            report = run_audit(site.url)

            self.assertEqual(schema.validate(report), [])
            critical_or_high = [f for f in report["findings"]
                                if f["severity"] in ("critical", "high")]
            self.assertEqual(critical_or_high, [],
                             "a well-built site should raise no critical or high findings; got %s"
                             % [(f["check_id"], f["title"]) for f in critical_or_high])
            found = {f["check_id"] for f in report["findings"]}
            for absent in ("CR-04", "CR-05", "CR-08", "CR-11", "AC-01", "AC-03",
                           "AC-04", "AC-11", "EN-01", "EN-07"):
                self.assertNotIn(absent, found,
                                 "%s is a false positive on a healthy site" % absent)

    def test_report_is_deterministic_across_runs(self):
        with FixtureSite(DEFECTIVE_SITE) as site:
            first = run_audit(site.url)
            second = run_audit(site.url)

        def fingerprint(report):
            return [(f["id"], f["check_id"], f["severity"], f["priority_rank"])
                    for f in report["findings"]]

        self.assertEqual(fingerprint(first), fingerprint(second))
        self.assertEqual(first["summary"]["total_findings"],
                         second["summary"]["total_findings"])
        self.assertEqual([r["id"] for r in first["root_cause_analysis"]],
                         [r["id"] for r in second["root_cause_analysis"]])

    def test_runtime_is_well_inside_the_five_minute_ceiling(self):
        with FixtureSite(DEFECTIVE_SITE) as site:
            started = time.monotonic()
            report = run_audit(site.url)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 120, "a small site must audit quickly")
        self.assertLess(report["audit_metrics"]["duration_ms"], 120000)
        self.assertIn("phase_ms", report["audit_metrics"])


class TestDegradation(unittest.TestCase):
    def test_invalid_url_still_writes_a_valid_report(self):
        report = run_audit("not a url", expect=2)
        self.assertEqual(schema.validate(report), [])
        self.assertEqual(report["summary"]["total_findings"], 0)
        self.assertEqual(report["limitations"][0]["scope"], "input")

    def test_unreachable_site_reports_a_limitation_not_a_finding(self):
        """A host we never reached is a coverage gap, not a site defect.

        Detailed coverage of every transport failure mode lives in
        test_transport.py; this asserts the pipeline-level contract.
        """
        report = run_audit("http://127.0.0.1:9/", "--budget", "35", "--timeout", "3")
        self.assertEqual(schema.validate(report), [])
        self.assertEqual(report["summary"]["total_findings"], 0)
        self.assertIn("transport", {l["scope"] for l in report["limitations"]})
        self.assertFalse(report["audit_metrics"]["target_responded"])

    def test_blocked_site_is_reported_as_a_discoverability_defect(self):
        blocked = {"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                        content_type="text/plain")}
        with FixtureSite(blocked, default_status=403,
                         not_found_body="<html><body>Access denied</body></html>") as site:
            report = run_audit(site.url)
        self.assertEqual(schema.validate(report), [])
        found = {f["check_id"] for f in report["findings"]}
        self.assertTrue({"CR-01", "CR-02"} & found)
        self.assertGreater(report["summary"]["critical"] + report["summary"]["high"], 0)

    def test_a_missing_analyzer_becomes_a_limitation_not_a_failure(self):
        """Simulate a broken marketplace: the report must still be produced."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "marketplace"
            (fake_root / "skills").mkdir(parents=True)
            manifest = json.loads((ROOT / "marketplace.json").read_text(encoding="utf-8"))
            manifest["skills"] = [s for s in manifest["skills"]
                                  if s["id"] in ("audit-orchestrator", "engagement-audit")]
            (fake_root / "marketplace.json").write_text(json.dumps(manifest),
                                                        encoding="utf-8")
            # The surviving skills are real copies; the other two are simply
            # absent from the manifest, as if the package had been trimmed.
            for skill in manifest["skills"]:
                shutil.copytree(ROOT / skill["path"], fake_root / skill["path"])
            with FixtureSite(HEALTHY_SITE) as site:
                report = run_audit(site.url, "--marketplace-root", str(fake_root))

        self.assertEqual(schema.validate(report), [])
        scopes = {limitation["scope"] for limitation in report["limitations"]}
        self.assertIn("crawl-render-audit", scopes)
        self.assertIn("ai-citability-audit", scopes)
        self.assertEqual(report["audit_metrics"]["skills_reporting"], ["engagement-audit"])
        self.assertGreater(report["coverage"]["checks_total"], 0,
                           "the surviving skill still contributed coverage")

    def test_tiny_budget_still_produces_a_valid_report(self):
        with FixtureSite(DEFECTIVE_SITE) as site:
            report = run_audit(site.url, "--budget", "30")
        self.assertEqual(schema.validate(report), [])
        self.assertTrue(report["report_validation"]["valid"])

    def test_slow_site_does_not_prevent_a_report(self):
        slow = {"/": Route(page("Slow", body=RICH_PARAGRAPH), delay=1.5),
                "/robots.txt": Route("User-agent: *\nAllow: /\n",
                                     content_type="text/plain", delay=1.0)}
        with FixtureSite(slow) as site:
            report = run_audit(site.url, "--budget", "40", "--timeout", "3")
        self.assertEqual(schema.validate(report), [])


class TestAuxiliaryScripts(unittest.TestCase):
    def test_crawl_then_compose_offline_matches_the_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap.json"
            with FixtureSite(DEFECTIVE_SITE) as site:
                subprocess.run([sys.executable, str(CRAWL_SITE), "--url", site.url,
                                "--out", str(snap), "--rate", "50"],
                               capture_output=True, text=True, check=True, timeout=120)
            self.assertTrue(snap.exists())
            snapshot = json.loads(snap.read_text(encoding="utf-8"))
            self.assertTrue(snapshot["pages"])

            checks = []
            for skill, script in (
                ("crawl-render-audit", "skills/crawl-render-audit/scripts/check_crawl_render.py"),
                ("engagement-audit", "skills/engagement-audit/scripts/check_engagement.py"),
            ):
                out = Path(tmp) / ("%s.json" % skill)
                subprocess.run([sys.executable, str(ROOT / script), "--snapshot", str(snap),
                                "--out", str(out)], check=True, capture_output=True,
                               timeout=120)
                checks.append(str(out))

            report_path = Path(tmp) / "report.json"
            subprocess.run([sys.executable, str(COMPOSE), "--snapshot", str(snap),
                            "--checks"] + checks + ["--out", str(report_path)],
                           check=True, capture_output=True, timeout=120)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(schema.validate(report), [])
            self.assertTrue(report["findings"])

    def test_compose_tolerates_a_missing_check_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap.json"
            snap.write_text(json.dumps({"site": "example.test", "pages": [],
                                        "coverage": {}}), encoding="utf-8")
            out = Path(tmp) / "report.json"
            subprocess.run([sys.executable, str(COMPOSE), "--snapshot", str(snap),
                            "--checks", str(Path(tmp) / "nope.json"), "--out", str(out)],
                           check=True, capture_output=True, timeout=60)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(schema.validate(report), [])
            self.assertTrue(any(limitation["scope"] == "nope"
                                for limitation in report["limitations"]))

    def test_semantic_findings_merge_and_malformed_ones_are_rejected(self):
        with FixtureSite(DEFECTIVE_SITE) as site, tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            subprocess.run([sys.executable, str(RUN_AUDIT), "--url", site.url,
                            "--out", str(report_path), "--rate", "50", "--quiet"],
                           check=True, capture_output=True, timeout=180)
            before = json.loads(report_path.read_text(encoding="utf-8"))

            agent = Path(tmp) / "agent.json"
            agent.write_text(json.dumps({"producer": "agent-semantic-review", "findings": [
                {"check_id": "AG-01", "title": "Menu labels are internal jargon",
                 "pillar": "on_site_engagement", "impact_base": 2, "confidence": "medium",
                 "breadth": {"affected": 2, "applicable": 3, "unit": "pages"},
                 "evidence": "Menu items read 'Platform' and 'Resources'.",
                 "mechanism": "Abstract labels force exploration.",
                 "remediation": {"summary": "Rename menu items.", "steps": ["Use nouns."],
                                 "effort": "low"}},
                {"check_id": "AG-02", "title": "no remediation"},
                "not an object",
                {"check_id": "AG-03", "title": "Overreaching claim", "impact_base": 9,
                 "confidence": "high", "evidence": "x",
                 "remediation": {"summary": "y", "steps": []}},
            ]}), encoding="utf-8")

            out = Path(tmp) / "final.json"
            subprocess.run([sys.executable, str(FINALIZE), "--report", str(report_path),
                            "--agent-findings", str(agent), "--out", str(out)],
                           check=True, capture_output=True, timeout=60)
            after = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(schema.validate(after), [])
        self.assertEqual(after["summary"]["total_findings"],
                         before["summary"]["total_findings"] + 2)
        agent_findings = [f for f in after["findings"] if f["check_id"].startswith("AG-")]
        self.assertEqual({f["check_id"] for f in agent_findings}, {"AG-01", "AG-03"})
        for finding in agent_findings:
            self.assertNotEqual(finding["severity"], "critical",
                                "the semantic pass is capped below critical")
        note = next(l for l in after["limitations"] if l["scope"] == "semantic-review")
        self.assertIn("Rejected", note["reason"])


if __name__ == "__main__":
    unittest.main()
