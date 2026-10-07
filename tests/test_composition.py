"""Composition: selection, de-duplication, rollup, ordering, correlation.

These are the orchestrator's own job. They are tested on synthetic check
documents rather than real crawls so each rule can be isolated.
"""

from __future__ import annotations

import unittest

from helpers import make_check, make_document, make_page, minimal_snapshot

from auditlib import compose, schema


def build(*documents, snapshot=None, **kwargs):
    return compose.build_report(snapshot or minimal_snapshot(), list(documents),
                                metrics={"duration_ms": 0}, **kwargs)


class TestSelection(unittest.TestCase):
    def test_only_failures_become_findings(self):
        document = make_document([
            make_check("A-01", status="FAIL"),
            make_check("A-02", status="PASS"),
            make_check("A-03", status="NOT_APPLICABLE", applicability="no products"),
            make_check("A-04", status="NOT_EVALUATED", applicability="no search tool"),
            make_check("A-05", status="INCONCLUSIVE", applicability="one signal only"),
        ])
        report = build(document)
        self.assertEqual([f["check_id"] for f in report["findings"]], ["A-01"])
        self.assertEqual(report["coverage"]["checks_total"], 5)
        self.assertEqual(report["coverage"]["checks_by_status"]["NOT_APPLICABLE"], 1)

    def test_not_evaluated_and_inconclusive_become_limitations(self):
        report = build(make_document([
            make_check("A-04", status="NOT_EVALUATED", applicability="no search tool"),
            make_check("A-05", status="INCONCLUSIVE", applicability="one signal only"),
        ]))
        scopes = {limitation["scope"] for limitation in report["limitations"]}
        self.assertIn("A-04", scopes)
        self.assertIn("A-05", scopes)
        self.assertEqual(report["findings"], [])

    def test_passes_are_visible_in_coverage(self):
        report = build(make_document([make_check("A-02", status="PASS",
                                                 evidence="6 of 6 pages fine.")]))
        entry = next(c for c in report["coverage"]["checks"] if c["check_id"] == "A-02")
        self.assertEqual(entry["status"], "PASS")
        self.assertIn("6 of 6", entry["note"])


class TestDeduplication(unittest.TestCase):
    def test_shared_dedupe_key_merges_into_one_finding(self):
        crawl = make_document([make_check(
            "CR-25", dedupe_key="broken-internal-links", impact_base=3,
            pillar="ai_discoverability", title="Crawl paths dead-end",
            evidence="1 of 5 probed links returned 404.",
            affected_urls=["https://example.test/gone"])], producer="crawl-render-audit")
        engagement = make_document([make_check(
            "EN-06", dedupe_key="broken-internal-links", impact_base=2,
            pillar="on_site_engagement", title="Visitors reach error pages",
            evidence="1 of 5 links errors for visitors too.",
            mechanism="A visitor lands on an error and leaves.",
            affected_urls=["https://example.test/gone2"])], producer="engagement-audit")
        report = build(crawl, engagement)
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        self.assertEqual(finding["check_id"], "CR-25")
        self.assertEqual([m["check_id"] for m in finding["merged_from"]], ["EN-06"])
        self.assertIn("On-site engagement", finding["also_affects"])
        self.assertIn("Independently confirmed by EN-06", finding["evidence"])
        self.assertIn("second consequence", finding["mechanism"])
        self.assertEqual(len(finding["affected_urls"]), 2)

    def test_distinct_keys_stay_distinct(self):
        report = build(make_document([
            make_check("A-01", dedupe_key="one"),
            make_check("A-02", dedupe_key="two")]))
        self.assertEqual(len(report["findings"]), 2)

    def test_highest_severity_member_wins(self):
        low = make_check("A-01", dedupe_key="shared", impact_base=1)
        high = make_check("B-01", dedupe_key="shared", impact_base=4)
        report = build(make_document([low]), make_document([high], producer="other"))
        self.assertEqual(report["findings"][0]["check_id"], "B-01")


class TestRollup(unittest.TestCase):
    def documents(self, root_ratio=(5, 6)):
        affected, applicable = root_ratio
        root = make_check("CR-11", dedupe_key="render-gap", impact_base=4,
                          tags=["render-gap-root-cause"], title="Render gap",
                          breadth={"affected": affected, "applicable": applicable})
        symptoms = [
            make_check("CR-12", dedupe_key="thin-content", impact_base=3,
                       tags=["render-gap-symptom"], title="Thin content",
                       breadth={"affected": affected, "applicable": applicable}),
            make_check("AC-01", dedupe_key="structured-data-missing", impact_base=3,
                       tags=["render-gap-symptom"], title="No structured data",
                       breadth={"affected": applicable, "applicable": applicable}),
        ]
        return make_document([root] + symptoms)

    def test_symptoms_fold_into_the_root_cause(self):
        report = build(self.documents())
        self.assertEqual(len(report["findings"]), 1)
        root = report["findings"][0]
        self.assertEqual(root["check_id"], "CR-11")
        folded = {s["check_id"] for s in root["contributing_symptoms"]}
        self.assertEqual(folded, {"CR-12", "AC-01"})
        self.assertIn("Downstream effects observed", root["evidence"])
        self.assertTrue(root["rollup_note"])
        self.assertEqual(report["finding_rollups"][0]["root_check_id"], "CR-11")

    def test_narrow_root_cause_does_not_absorb_symptoms(self):
        report = build(self.documents(root_ratio=(1, 6)))
        self.assertEqual(len(report["findings"]), 3,
                         "below 50% breadth the symptoms are probably independent")
        self.assertEqual(report["finding_rollups"], [])

    def test_rollup_is_recorded_not_hidden(self):
        report = build(self.documents())
        self.assertTrue(report["finding_rollups"])
        self.assertIn("folded_check_ids", report["finding_rollups"][0])


class TestOrderingAndIdentity(unittest.TestCase):
    def test_findings_are_sorted_by_severity_then_deterministically(self):
        checks = [
            make_check("Z-01", impact_base=1, dedupe_key="z"),
            make_check("A-01", impact_base=4, dedupe_key="a",
                       breadth={"affected": 9, "applicable": 10}),
            make_check("M-01", impact_base=2, dedupe_key="m"),
        ]
        report = build(make_document(checks))
        self.assertEqual([f["check_id"] for f in report["findings"]],
                         ["A-01", "M-01", "Z-01"])
        self.assertEqual([f["id"] for f in report["findings"]],
                         ["F-001", "F-002", "F-003"])
        self.assertEqual([f["priority_rank"] for f in report["findings"]], [1, 2, 3])

    def test_ids_are_stable_across_identical_runs(self):
        checks = [make_check("C-0%d" % i, impact_base=(i % 4) + 1, dedupe_key="k%d" % i)
                  for i in range(8)]
        first = build(make_document(list(checks)))
        second = build(make_document(list(reversed(checks))))
        self.assertEqual([(f["id"], f["check_id"]) for f in first["findings"]],
                         [(f["id"], f["check_id"]) for f in second["findings"]],
                         "input order must not change the report")

    def test_summary_matches_the_findings(self):
        report = build(make_document([
            make_check("A-01", impact_base=4, dedupe_key="a",
                       breadth={"affected": 9, "applicable": 10}),
            make_check("B-01", impact_base=3, dedupe_key="b"),
            make_check("C-01", impact_base=1, dedupe_key="c"),
        ]))
        summary = report["summary"]
        self.assertEqual(summary["total_findings"], 3)
        self.assertEqual(summary["critical"] + summary["high"]
                         + summary["medium"] + summary["low"], 3)
        self.assertEqual(schema.validate(report), [])

    def test_pillar_counts_are_reported(self):
        report = build(make_document([
            make_check("A-01", dedupe_key="a", pillar="ai_discoverability"),
            make_check("B-01", dedupe_key="b", pillar="on_site_engagement"),
        ]))
        self.assertEqual(report["summary"]["by_category"]["AI discoverability"], 1)
        self.assertEqual(report["summary"]["by_category"]["On-site engagement"], 1)


class TestFindingContent(unittest.TestCase):
    def test_every_finding_carries_actionable_detail(self):
        report = build(make_document([make_check("A-01")]))
        finding = report["findings"][0]
        for field in ("mechanism", "severity_rationale", "applicability",
                      "reproducibility", "confidence", "breadth", "check_id",
                      "dedupe_key", "produced_by"):
            self.assertIn(field, finding)
        action = finding["suggested_action"]
        self.assertTrue(action["summary"])
        self.assertTrue(action["steps"])
        self.assertIn(action["priority"], ("critical", "high", "medium", "low"))
        self.assertIn(action["effort"], ("low", "medium", "high"))

    def test_severity_rationale_shows_the_arithmetic(self):
        report = build(make_document([make_check("A-01", impact_base=3,
                                                 confidence="medium")]))
        self.assertIn("confidence medium", report["findings"][0]["severity_rationale"])


class TestCorrelation(unittest.TestCase):
    def test_crawlable_but_unquotable_is_diagnosed(self):
        checks = [
            make_check("AC-01", dedupe_key="structured-data-missing", impact_base=3),
            make_check("AC-12", dedupe_key="fact-density", impact_base=2),
            make_check("CR-01", status="PASS"), make_check("CR-02", status="PASS"),
            make_check("CR-05", status="PASS"), make_check("CR-08", status="PASS"),
        ]
        report = build(make_document(checks))
        ids = [rc["id"] for rc in report["root_cause_analysis"]]
        self.assertIn("RC-01", ids)
        rc = next(r for r in report["root_cause_analysis"] if r["id"] == "RC-01")
        self.assertTrue(rc["related_findings"])
        self.assertTrue(rc["fix_order"])

    def test_blocked_ai_crawlers_suppress_the_crawlable_diagnosis(self):
        checks = [
            make_check("AC-01", dedupe_key="structured-data-missing", impact_base=3),
            make_check("AC-12", dedupe_key="fact-density", impact_base=2),
            make_check("CR-04", dedupe_key="robots-blocks-ai", impact_base=3),
            make_check("CR-01", status="PASS"), make_check("CR-02", status="PASS"),
            make_check("CR-05", status="PASS"), make_check("CR-08", status="PASS"),
        ]
        report = build(make_document(checks))
        self.assertNotIn("RC-01", [rc["id"] for rc in report["root_cause_analysis"]],
                         "a site that blocks AI crawlers is not 'fully crawlable'")

    def test_contradictory_indexation_signals_are_diagnosed(self):
        snapshot = minimal_snapshot(
            sitemap_urls=[{"loc": "https://example.test/a", "lastmod": ""}])
        report = build(make_document([
            make_check("CR-08", dedupe_key="noindex", impact_base=4,
                       affected_urls=["https://example.test/a"])]), snapshot=snapshot)
        rc = [r for r in report["root_cause_analysis"] if r["id"] == "RC-02"]
        self.assertTrue(rc)
        self.assertEqual(rc[0]["evidence_urls"], ["https://example.test/a"])

    def test_correlations_never_change_the_finding_count(self):
        checks = [make_check("AC-01", dedupe_key="structured-data-missing"),
                  make_check("AC-12", dedupe_key="fact-density")]
        report = build(make_document(checks))
        self.assertEqual(report["summary"]["total_findings"], len(report["findings"]))
        self.assertEqual(len(report["findings"]), 2)


class TestProactiveRecommendations(unittest.TestCase):
    def snapshot_with_pages(self, n=4, **page_kwargs):
        html = ("<html lang='en'><head><title>T</title></head><body><main>"
                "<h1>H</h1><p>%s</p></main></body></html>" % ("word " * 120))
        pages = [make_page("https://example.test/p%d" % i, depth=1, html=html,
                           **page_kwargs) for i in range(n)]
        return minimal_snapshot(
            pages=pages,
            well_known={"llms_txt": {"evaluated": True, "ok": False, "status": 404,
                                     "url": "https://example.test/llms.txt"}})

    def test_recommendations_are_gated_on_evidence(self):
        report = build(make_document([]), snapshot=self.snapshot_with_pages())
        ids = {r["id"] for r in report["proactive_recommendations"]}
        self.assertIn("PR-01", ids, "no llms.txt was observed")
        self.assertIn("PR-02", ids, "no question-shaped headings were observed")
        for rec in report["proactive_recommendations"]:
            self.assertTrue(rec["rationale"])
            self.assertTrue(rec["mechanism"])
            self.assertTrue(rec["evidence"])
            self.assertTrue(rec["steps"])
            self.assertIn(rec["priority"], ("critical", "high", "medium", "low"))

    def test_llms_txt_recommendation_is_withheld_when_it_exists(self):
        snapshot = self.snapshot_with_pages()
        snapshot["well_known"]["llms_txt"] = {"evaluated": True, "ok": True,
                                              "status": 200,
                                              "url": "https://example.test/llms.txt"}
        report = build(make_document([]), snapshot=snapshot)
        self.assertNotIn("PR-01", {r["id"] for r in report["proactive_recommendations"]})

    def test_recommendations_are_sorted_by_priority(self):
        report = build(make_document([]), snapshot=self.snapshot_with_pages())
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        ranks = [order[r["priority"]] for r in report["proactive_recommendations"]]
        self.assertEqual(ranks, sorted(ranks))


class TestReportShape(unittest.TestCase):
    def test_empty_input_yields_a_valid_empty_report(self):
        report = schema.validate_and_repair(build())
        self.assertTrue(report["report_validation"]["valid"])
        self.assertEqual(report["summary"]["total_findings"], 0)
        self.assertIn("executive_summary", report)
        self.assertTrue(report["executive_summary"]["headline"])

    def test_executive_summary_leads_with_the_worst_finding(self):
        report = build(make_document([
            make_check("A-01", impact_base=4, dedupe_key="a", title="Worst thing",
                       breadth={"affected": 9, "applicable": 10}),
            make_check("B-01", impact_base=1, dedupe_key="b", title="Small thing")]))
        summary = report["executive_summary"]
        self.assertIn("worst thing", summary["headline"].lower())
        self.assertEqual(summary["start_here"][0]["finding_id"], "F-001")
        self.assertTrue(summary["how_to_read_this"])

    def test_low_severity_tail_is_explicitly_triaged(self):
        """A long tail of small findings must not read as a laundry list."""
        checks = [make_check("A-01", impact_base=4, dedupe_key="a",
                             breadth={"affected": 9, "applicable": 10})]
        checks += [make_check("L-%02d" % i, impact_base=1, dedupe_key="l%d" % i)
                   for i in range(6)]
        report = build(make_document(checks))
        note = report["executive_summary"].get("note_on_low_severity")
        self.assertTrue(note, "a report with 5+ low findings should say so plainly")
        self.assertIn("low severity", note)
        self.assertNotIn("note_on_low_severity",
                         build(make_document([make_check("A-01")]))["executive_summary"],
                         "the note is withheld when there is no low-severity tail")

    def test_quick_wins_are_cheap_and_site_wide(self):
        report = build(make_document([
            make_check("A-01", impact_base=1, dedupe_key="a",
                       breadth={"affected": 10, "applicable": 10}),
            make_check("B-01", impact_base=1, dedupe_key="b",
                       breadth={"affected": 1, "applicable": 10},
                       remediation={"summary": "x", "steps": ["y"], "effort": "high"}),
        ]))
        wins = {w["finding_id"] for w in report["executive_summary"]["quick_wins"]}
        ids = {f["check_id"]: f["id"] for f in report["findings"]}
        self.assertIn(ids["A-01"], wins)
        self.assertNotIn(ids["B-01"], wins)

    def test_limitations_explain_crawl_bounds(self):
        snapshot = minimal_snapshot(
            coverage={"pages_attempted": 3, "pages_fetched": 3,
                      "pages_skipped_by_robots": 2, "pages_skipped_unsafe": 4,
                      "pages_skipped_for_time": 1},
            sitemap_url_total=99)
        report = build(make_document([]), snapshot=snapshot)
        titles = " ".join(limitation["title"] for limitation in report["limitations"])
        self.assertIn("robots.txt", titles)
        self.assertIn("State-changing", titles)
        self.assertIn("time budget", titles)
        self.assertIn("bounded sample", titles)


if __name__ == "__main__":
    unittest.main()
