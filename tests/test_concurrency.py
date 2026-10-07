"""Concurrent audits must not interfere with each other.

An earlier revision staged the crawl snapshot at a fixed path,
`<output-directory>/.audit-snapshot.json`, and deleted it when the run finished.
Two audits sharing an output directory therefore raced: one could overwrite the
other's snapshot (cross-site evidence contamination) or delete it mid-read
(FileNotFoundError). It was reproduced during a multi-site batch run.

The snapshot is now held in memory and handed to each analyzer on stdin, so no
shared file exists at all. These tests hold that guarantee: five audits of five
*different* sites, started together, writing into one directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from fixture_server import FixtureSite, RICH_PARAGRAPH, Route, page
from helpers import SCRIPTS

from auditlib import schema

RUN_AUDIT = SCRIPTS / "run_audit.py"
SITE_COUNT = 5


def distinct_site(marker: str) -> dict:
    """A site whose every page is traceable to exactly one fixture.

    Each site's content carries its own marker, so contamination between
    concurrent runs is detectable rather than merely improbable.
    """
    body = ("<p>%s operates the %s programme. %s</p>"
            % (marker, marker.lower(), RICH_PARAGRAPH))
    routes = {
        "/robots.txt": Route("User-agent: *\nAllow: /\n", content_type="text/plain"),
        "/": Route(page("%s home" % marker, h1="%s home" % marker,
                        body=body + '<a href="/about-%s">About %s</a>'
                                    '<a href="/gone-%s">Missing %s</a>'
                             % (marker, marker, marker, marker))),
        "/about-%s" % marker: Route(page("About %s" % marker, body=body)),
        "/gone-%s" % marker: Route("gone", status=404),
    }
    return routes


def audit_into(url: str, directory: Path, name: str):
    """Run one audit, writing its report into a shared directory."""
    out = directory / ("report-%s.json" % name)
    completed = subprocess.run(
        [sys.executable, str(RUN_AUDIT), "--url", url, "--out", str(out),
         "--rate", "50", "--quiet"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=240)
    return {
        "name": name,
        "url": url,
        "returncode": completed.returncode,
        "stderr": completed.stderr,
        "path": out,
        "report": json.loads(out.read_text(encoding="utf-8")) if out.exists() else None,
    }


class TestConcurrentAudits(unittest.TestCase):
    """Five simultaneous audits, one output directory."""

    @classmethod
    def setUpClass(cls):
        cls.markers = ["alpha", "bravo", "charlie", "delta", "echo"][:SITE_COUNT]
        cls.sites = [FixtureSite(distinct_site(m)) for m in cls.markers]
        for site in cls.sites:
            site.__enter__()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.tmp.name)

        # Start every audit at once and let them contend for the directory.
        barrier = threading.Barrier(SITE_COUNT)

        def run(index):
            barrier.wait(timeout=60)
            return audit_into(cls.sites[index].url, cls.directory, cls.markers[index])

        with ThreadPoolExecutor(max_workers=SITE_COUNT) as pool:
            cls.results = list(pool.map(run, range(SITE_COUNT)))

    @classmethod
    def tearDownClass(cls):
        for site in cls.sites:
            site.__exit__(None, None, None)
        cls.tmp.cleanup()

    # -- the failure modes the old design produced --------------------------

    def test_no_run_raised_a_file_error(self):
        for result in self.results:
            with self.subTest(site=result["name"]):
                self.assertNotIn("FileNotFoundError", result["stderr"])
                self.assertNotIn("No such file", result["stderr"])
                self.assertNotIn("Traceback", result["stderr"])

    def test_every_run_exited_cleanly(self):
        for result in self.results:
            with self.subTest(site=result["name"]):
                self.assertEqual(result["returncode"], 0, result["stderr"][-500:])

    def test_every_report_exists_and_is_valid(self):
        self.assertEqual(len(self.results), SITE_COUNT)
        for result in self.results:
            with self.subTest(site=result["name"]):
                self.assertTrue(result["path"].exists())
                self.assertIsNotNone(result["report"])
                self.assertEqual(schema.validate(result["report"]), [])
                self.assertTrue(result["report"]["report_validation"]["valid"])

    def test_each_report_describes_its_own_site(self):
        for result in self.results:
            with self.subTest(site=result["name"]):
                expected_port = urlsplit(result["url"]).port
                audited = result["report"]["audited_url"]
                self.assertEqual(urlsplit(audited).port, expected_port,
                                 "%s reported %s" % (result["url"], audited))

    def test_no_cross_site_evidence_contamination(self):
        """Every URL in every report must belong to that report's own origin."""
        for result in self.results:
            report = result["report"]
            own_port = urlsplit(result["url"]).port
            other_markers = [m for m in self.markers if m != result["name"]]
            urls = []
            for finding in report["findings"]:
                urls.extend(finding.get("affected_urls") or [])
                urls.extend(item.get("url") or ""
                            for item in finding.get("evidence_items") or [])
            urls.extend(check.get("note", "") for check in report["coverage"]["checks"])

            with self.subTest(site=result["name"]):
                for url in urls:
                    if url.startswith("http"):
                        self.assertEqual(urlsplit(url).port, own_port,
                                         "%s leaked %s" % (result["name"], url))
                blob = json.dumps(report)
                for foreign in other_markers:
                    self.assertNotIn(foreign, blob,
                                     "%s report mentions %s"
                                     % (result["name"], foreign))
                self.assertIn(result["name"], blob,
                              "%s report contains none of its own content"
                              % result["name"])

    def test_reports_are_not_identical_to_each_other(self):
        """A guard against every run silently analysing the same snapshot."""
        audited = {r["report"]["audited_url"] for r in self.results}
        self.assertEqual(len(audited), SITE_COUNT)

    def test_findings_were_actually_produced(self):
        for result in self.results:
            with self.subTest(site=result["name"]):
                self.assertGreater(result["report"]["summary"]["total_findings"], 0,
                                   "the run produced no analysis at all")
                self.assertEqual(len(result["report"]["audit_metrics"]["skills_reporting"]),
                                 4, "every analyzer skill should have reported")

    # -- temporary state ----------------------------------------------------

    def test_no_shared_temporary_state_is_left_behind(self):
        leftovers = sorted(p.name for p in self.directory.iterdir()
                           if p.name not in {"report-%s.json" % m for m in self.markers})
        self.assertEqual(leftovers, [],
                         "the output directory should contain only the reports")

    def test_no_snapshot_file_is_created_at_all(self):
        """The fixed `.audit-snapshot.json` must never reappear."""
        self.assertFalse((self.directory / ".audit-snapshot.json").exists())
        self.assertEqual([p.name for p in self.directory.glob("*snapshot*")], [])


class TestSnapshotOutIsOptOnly(unittest.TestCase):
    """--snapshot-out writes exactly where asked, and nowhere else."""

    def test_snapshot_is_written_only_when_requested(self):
        with FixtureSite(distinct_site("solo")) as site, \
                tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            subprocess.run(
                [sys.executable, str(RUN_AUDIT), "--url", site.url,
                 "--out", str(directory / "report.json"), "--rate", "50", "--quiet"],
                check=True, capture_output=True, timeout=240)
            self.assertEqual(sorted(p.name for p in directory.iterdir()),
                             ["report.json"])

            wanted = directory / "nested" / "snap.json"
            subprocess.run(
                [sys.executable, str(RUN_AUDIT), "--url", site.url,
                 "--out", str(directory / "report.json"),
                 "--snapshot-out", str(wanted), "--rate", "50", "--quiet"],
                check=True, capture_output=True, timeout=240)
            self.assertTrue(wanted.exists())
            snapshot = json.loads(wanted.read_text(encoding="utf-8"))
            self.assertIn("transport", snapshot)
            self.assertTrue(snapshot["transport"]["target_responded"])

    def test_two_audits_may_share_an_output_path_prefix(self):
        """Same directory, same basename stem, different sites - no interference."""
        with FixtureSite(distinct_site("first")) as a, \
                FixtureSite(distinct_site("second")) as b, \
                tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda pair: audit_into(pair[0], directory, pair[1]),
                    [(a.url, "one"), (b.url, "two")]))
        for result, marker in zip(results, ("first", "second")):
            self.assertEqual(result["returncode"], 0, result["stderr"][-400:])
            self.assertEqual(schema.validate(result["report"]), [])
            self.assertIn(marker, json.dumps(result["report"]))


if __name__ == "__main__":
    unittest.main()
