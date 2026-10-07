"""Checks built from signals the parser already collected but nothing read.

The HTML parser extracted a set of fields into every snapshot that no check
consumed. Each was a real defect the audit was paying to collect evidence for
and then discarding:

* **`CR-32`** meta-refresh redirects, which browsers follow and fetchers often
  do not, so the stored document is the empty waypoint.
* **`CR-33`** duplicated `<title>` and decisive `<meta name>`, where the value
  that survives is chosen by the parser rather than the author.
* **`CR-34`** locale alternates, so the variants of a localised page stop
  competing with each other.
* **`AC-19`** tables without header cells, where the numbers survive extraction
  but their meaning does not.

The final test in this module is the guard against the whole class recurring: it
fails when a newly extracted field is added and left unread.
"""

from __future__ import annotations

import re
import unittest

from fixture_server import FixtureSite, RICH_PARAGRAPH, Route, page
from helpers import ROOT, checks_by_id, make_page, minimal_snapshot, run_analyzer, \
    snapshot_of, transport_block

BODY = "<p>%s</p>" % RICH_PARAGRAPH


def crawl_checks(snapshot):
    return checks_by_id(run_analyzer("crawl-render-audit", snapshot))


def cite_checks(snapshot):
    return checks_by_id(run_analyzer("ai-citability-audit", snapshot))


def snap(pages):
    return minimal_snapshot(pages=pages)


class TestMetaRefresh(unittest.TestCase):
    def test_a_refresh_with_a_url_is_a_finding(self):
        html = page("Moved", head='<meta http-equiv="refresh" content="0; url=/new-home">',
                    body="<p>Redirecting.</p>")
        check = crawl_checks(snap([make_page(html=html)]))["CR-32"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["impact_base"], 3)
        self.assertEqual(check["dedupe_key"], "meta-refresh-redirect")
        self.assertIn("/new-home", check["evidence"])

    def test_a_refresh_without_a_url_is_not_a_redirect(self):
        """`content="300"` reloads the same page. It hides nothing."""
        html = page("Live scores", head='<meta http-equiv="refresh" content="300">',
                    body=BODY)
        self.assertEqual(crawl_checks(snap([make_page(html=html)]))["CR-32"]["status"],
                         "PASS")

    def test_an_ordinary_page_passes(self):
        self.assertEqual(
            crawl_checks(snap([make_page(html=page("Home", body=BODY))]))["CR-32"]["status"],
            "PASS")

    def test_quoted_and_delayed_targets_are_still_caught(self):
        html = page("Old", head="<meta http-equiv=\"refresh\" content=\"5; URL='/elsewhere'\">")
        check = crawl_checks(snap([make_page(html=html)]))["CR-32"]
        self.assertEqual(check["status"], "FAIL")
        self.assertIn("/elsewhere", check["evidence"])
        self.assertIn("5s", check["evidence"])

    def test_no_response_means_no_finding(self):
        blocked = minimal_snapshot(pages=[],
                                   transport=transport_block([], failures={"dns": 4}))
        self.assertEqual(crawl_checks(blocked)["CR-32"]["status"], "NOT_EVALUATED")

    def test_live_fixture_site(self):
        routes = {"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                       content_type="text/plain"),
                  "/": Route(page("Gateway",
                                  head='<meta http-equiv="refresh" content="0; url=/app">'))}
        with FixtureSite(routes) as site:
            self.assertEqual(crawl_checks(snapshot_of(site.url))["CR-32"]["status"], "FAIL")


class TestDuplicateHeadMetadata(unittest.TestCase):
    def test_two_title_elements_are_a_finding(self):
        html = page("First", head="<title>Second</title>", body=BODY)
        check = crawl_checks(snap([make_page(html=html)]))["CR-33"]
        self.assertEqual(check["status"], "FAIL")
        self.assertIn("2 <title>", check["evidence"])

    def test_repeated_description_is_a_finding(self):
        html = page("Home", body=BODY, head='<meta name="description" content="One">'
                                            '<meta name="description" content="Two">')
        check = crawl_checks(snap([make_page(html=html)]))["CR-33"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["impact_base"], 2)
        self.assertIn("description", check["evidence"])

    def test_repeated_robots_raises_the_impact(self):
        """Conflicting robots directives resolve to the most restrictive."""
        html = page("Home", body=BODY, head='<meta name="robots" content="index">'
                                            '<meta name="robots" content="noindex">')
        check = crawl_checks(snap([make_page(html=html)]))["CR-33"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["impact_base"], 3)
        self.assertIn("noindex", check["mechanism"])

    def test_a_repeated_non_decisive_meta_is_ignored(self):
        """A second viewport is untidy, not a citation defect."""
        html = page("Home", body=BODY,
                    head='<meta name="viewport" content="width=device-width">')
        self.assertEqual(crawl_checks(snap([make_page(html=html)]))["CR-33"]["status"],
                         "PASS")

    def test_a_clean_head_passes(self):
        self.assertEqual(
            crawl_checks(snap([make_page(html=page("Home", body=BODY))]))["CR-33"]["status"],
            "PASS")

    def test_svg_icon_titles_are_not_document_titles(self):
        """Found live: an icon system put 16 `<title>` elements on one page.

        `<svg><title>` is the accessible name of a glyph, not the document
        title. Counting it reported a `high` defect against a site whose head
        was correct, and concatenated every icon label onto the title an
        assistant would cite.
        """
        icons = "".join('<svg viewBox="0 0 1 1"><title>Icon %d</title><path/></svg>' % i
                        for i in range(16))
        html = page("The Real Title", body=BODY + icons)
        self.assertEqual(crawl_checks(snap([make_page(html=html)]))["CR-33"]["status"],
                         "PASS")

    def test_an_svg_title_never_reaches_the_document_title(self):
        from auditlib import htmlparse
        extracted = htmlparse.extract(
            "<!doctype html><html><head><title>Real Title</title></head><body>"
            "<svg><title>Icon label</title></svg>"
            "<template><title>Never rendered</title></template>"
            "<p>Body copy.</p></body></html>")
        self.assertEqual(extracted.title, "Real Title")
        self.assertEqual(extracted.title_count, 1)

    def test_a_genuine_second_head_title_is_still_counted(self):
        from auditlib import htmlparse
        extracted = htmlparse.extract(
            "<!doctype html><html><head><title>One</title><title>Two</title></head>"
            "<body><p>Body copy.</p></body></html>")
        self.assertEqual(extracted.title_count, 2)

    def test_no_response_means_no_finding(self):
        blocked = minimal_snapshot(pages=[],
                                   transport=transport_block([], failures={"timeout": 3}))
        self.assertEqual(crawl_checks(blocked)["CR-33"]["status"], "NOT_EVALUATED")


class TestLocaleAlternates(unittest.TestCase):
    def test_a_monolingual_site_is_not_asked_for_hreflang(self):
        pages = [make_page(html=page("Home", body=BODY)),
                 make_page("https://example.test/about", depth=1,
                           html=page("About", body=BODY))]
        check = crawl_checks(snap(pages))["CR-34"]
        self.assertEqual(check["status"], "NOT_APPLICABLE")
        self.assertIn("monolingual", check["applicability"])

    def test_locale_path_roots_without_hreflang_are_a_finding(self):
        pages = [make_page(html=page("Home", body=BODY)),
                 make_page("https://example.test/en/pricing", depth=1,
                           html=page("Pricing", body=BODY)),
                 make_page("https://example.test/de/preise", depth=1,
                           html=page("Preise", body=BODY, lang="de"))]
        check = crawl_checks(snap(pages))["CR-34"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["dedupe_key"], "hreflang-missing")
        self.assertIn("locale path roots", check["evidence"])

    def test_two_declared_languages_without_hreflang_are_a_finding(self):
        pages = [make_page(html=page("Home", body=BODY, lang="en")),
                 make_page("https://example.test/seite", depth=1,
                           html=page("Seite", body=BODY, lang="de"))]
        check = crawl_checks(snap(pages))["CR-34"]
        self.assertEqual(check["status"], "FAIL")
        self.assertIn("<html lang>", check["evidence"])

    def test_non_language_path_segments_do_not_trigger_it(self):
        """`/us/` and `/go/` are not languages. This is the false-positive guard."""
        pages = [make_page(html=page("Home", body=BODY)),
                 make_page("https://example.test/us/team", depth=1,
                           html=page("Team", body=BODY)),
                 make_page("https://example.test/go/demo", depth=1,
                           html=page("Demo", body=BODY))]
        self.assertEqual(crawl_checks(snap(pages))["CR-34"]["status"], "NOT_APPLICABLE")

    def test_a_well_formed_reciprocal_set_passes(self):
        alts = ('<link rel="alternate" hreflang="en" href="https://example.test/en/">'
                '<link rel="alternate" hreflang="de" href="https://example.test/de/">'
                '<link rel="alternate" hreflang="x-default" href="https://example.test/">')
        pages = [make_page("https://example.test/en/", html=page("Home", head=alts, body=BODY)),
                 make_page("https://example.test/de/", depth=1, lang_note=None,
                           html=page("Start", head=alts, body=BODY, lang="de"))]
        check = crawl_checks(snap(pages))["CR-34"]
        self.assertEqual(check["status"], "PASS")

    def test_an_invalid_language_code_is_a_finding(self):
        """`en-uk` is invalid; the region subtag for the United Kingdom is `gb`."""
        alts = ('<link rel="alternate" hreflang="en-uk" href="https://example.test/uk/">'
                '<link rel="alternate" hreflang="en" href="https://example.test/en/">')
        pages = [make_page("https://example.test/en/", html=page("Home", head=alts, body=BODY))]
        check = crawl_checks(snap(pages))["CR-34"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["dedupe_key"], "hreflang-invalid")
        self.assertIn("en-uk", check["evidence"])

    def test_a_set_that_omits_itself_is_a_finding(self):
        alts = '<link rel="alternate" hreflang="de" href="https://example.test/de/">'
        pages = [make_page("https://example.test/en/", html=page("Home", head=alts, body=BODY))]
        check = crawl_checks(snap(pages))["CR-34"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["dedupe_key"], "hreflang-no-self-reference")

    def test_no_response_means_no_finding(self):
        blocked = minimal_snapshot(pages=[],
                                   transport=transport_block([], failures={"tls": 2}))
        self.assertEqual(crawl_checks(blocked)["CR-34"]["status"], "NOT_EVALUATED")


class TestTabularFacts(unittest.TestCase):
    TABLE_NO_TH = ("<table><tr><td>Tier</td><td>Price</td></tr>"
                   "<tr><td>Pro</td><td>490</td></tr></table>")
    TABLE_WITH_TH = ("<table><caption>Plans</caption><thead><tr><th>Tier</th>"
                     "<th>Price</th></tr></thead><tbody><tr><td>Pro</td>"
                     "<td>490</td></tr></tbody></table>")

    def test_a_page_with_no_table_is_not_applicable(self):
        check = cite_checks(snap([make_page(html=page("Home", body=BODY))]))["AC-19"]
        self.assertEqual(check["status"], "NOT_APPLICABLE")
        self.assertIn("no tabular data", check["applicability"].lower().replace(
            "there is no tabular data", "no tabular data"))

    def test_a_table_without_header_cells_is_a_finding(self):
        html = page("Pricing", body=BODY + self.TABLE_NO_TH)
        check = cite_checks(snap([make_page(html=html)]))["AC-19"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["dedupe_key"], "tables-unlabelled")
        self.assertEqual(check["confidence"], "medium")
        self.assertIn("<th>", check["evidence"])

    def test_a_labelled_table_passes(self):
        html = page("Pricing", body=BODY + self.TABLE_WITH_TH)
        self.assertEqual(cite_checks(snap([make_page(html=html)]))["AC-19"]["status"],
                         "PASS")

    def test_the_denominator_is_pages_that_have_a_table(self):
        pages = [make_page(html=page("Home", body=BODY)),
                 make_page("https://example.test/pricing", depth=1,
                           html=page("Pricing", body=BODY + self.TABLE_NO_TH))]
        check = cite_checks(snap(pages))["AC-19"]
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["breadth"], {"affected": 1, "applicable": 1,
                                            "unit": "pages"})

    def test_caption_count_appears_in_the_evidence_items(self):
        html = page("Pricing", body=BODY + self.TABLE_NO_TH)
        check = cite_checks(snap([make_page(html=html)]))["AC-19"]
        self.assertIn("<caption>", check["evidence_items"][0]["observed"])


class TestNoSignalIsCollectedAndDiscarded(unittest.TestCase):
    """The guard against the defect class this module exists to close.

    Every field the parser writes into a snapshot costs bytes in the artefact and
    implies to a reader that something acts on it. A field that nothing reads is
    either a missing check or dead weight, and both should be a deliberate
    decision rather than an accident.
    """

    # Fields that are deliberately carried without being asserted on. Each is
    # context a human or an agent reads off the snapshot when interpreting a
    # finding, not a signal with a threshold.
    ALLOWED_UNREAD = {
        "has_skip_link",        # accessibility context; EN-* judges orientation by links
        "inline_script_bytes",  # weight detail behind CR-30's byte total
        "inline_style_bytes",   # ditto
        "list_items",           # scanability context behind EN-11
        "media_counts",         # narrative context for AC-13's media findings
        "paragraph_count",      # denominator context behind long_paragraphs
        "parse_error",          # surfaced by the crawler as a note, not a check
        "scripts",              # the src list behind script_count
    }

    def extraction_fields(self):
        source = (ROOT / "skills/audit-orchestrator/scripts/auditlib/htmlparse.py").read_text(
            encoding="utf-8")
        block = re.search(r"    def to_dict\(self\) -> dict:\n        return \{(.*?)\n        \}",
                          source, re.S)
        self.assertIsNotNone(block, "could not locate Extraction.to_dict()")
        return set(re.findall(r'"([a-z_0-9]+)":', block.group(1)))

    def consumers(self):
        blob = ""
        for path in sorted(ROOT.glob("skills/*/scripts/check_*.py")):
            blob += path.read_text(encoding="utf-8")
        blob += (ROOT / "skills/audit-orchestrator/scripts/auditlib/compose.py").read_text(
            encoding="utf-8")
        return blob

    def test_every_extracted_field_is_read_or_explicitly_allowed(self):
        blob = self.consumers()
        unread = {f for f in self.extraction_fields()
                  if '"%s"' % f not in blob and "'%s'" % f not in blob}
        self.assertEqual(
            unread, self.ALLOWED_UNREAD,
            "extraction fields changed. Add a check that reads the new field, or add it "
            "to ALLOWED_UNREAD with the reason it is carried unread.")

    def test_the_four_revived_fields_are_genuinely_consumed(self):
        blob = self.consumers()
        for field in ("hreflang", "meta_http_equiv", "title_count", "meta_duplicates",
                      "tables", "table_headers"):
            with self.subTest(field=field):
                self.assertIn('"%s"' % field, blob)


if __name__ == "__main__":
    unittest.main()
