"""Shared test helpers: path setup and small builders.

Every test module imports from here so the marketplace's own modules are
importable without installing anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCH = ROOT / "skills" / "audit-orchestrator"
SCRIPTS = ORCH / "scripts"

for path in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

ANALYZER_SCRIPTS = {
    "crawl-render-audit": ROOT / "skills/crawl-render-audit/scripts/check_crawl_render.py",
    "ai-citability-audit": ROOT / "skills/ai-citability-audit/scripts/check_citability.py",
    "freshness-corroboration": ROOT / "skills/freshness-corroboration/scripts/check_freshness.py",
    "engagement-audit": ROOT / "skills/engagement-audit/scripts/check_engagement.py",
}


def run_analyzer(skill_id: str, snapshot: dict, extra_args=None) -> dict:
    """Run one analyzer skill as a subprocess over an in-memory snapshot."""
    script = ANALYZER_SCRIPTS[skill_id]
    completed = subprocess.run(
        [sys.executable, str(script), "--snapshot", "-", "--out", "-"] + list(extra_args or []),
        input=json.dumps(snapshot), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90)
    assert completed.returncode == 0, "%s exited %d: %s" % (
        skill_id, completed.returncode, completed.stderr[:500])
    return json.loads(completed.stdout)


def checks_by_id(document: dict) -> dict:
    return {c["check_id"]: c for c in document.get("checks", [])}


def crawl(url: str, **kwargs):
    """Crawl a fixture site with rate limiting relaxed (localhost, our own server)."""
    from auditlib.budget import Budget
    from auditlib.crawler import Crawl
    from auditlib.fetcher import Fetcher

    budget = kwargs.pop("budget", None) or Budget(total=kwargs.pop("total", 120.0))
    fetcher = kwargs.pop("fetcher", None) or Fetcher(rate_per_sec=kwargs.pop("rate", 50.0),
                                                     timeout=kwargs.pop("timeout", 5.0))
    session = Crawl(url, budget=budget, fetcher=fetcher, **kwargs)
    session.discover()
    session.run()
    return session


def snapshot_of(url: str, **kwargs) -> dict:
    return crawl(url, **kwargs).snapshot()


def make_check(check_id="X-01", **overrides) -> dict:
    """A minimal valid CheckResult, for severity/composition tests."""
    check = {
        "check_id": check_id,
        "title": "A defect",
        "pillar": "ai_discoverability",
        "category": "test",
        "status": "FAIL",
        "impact_base": 3,
        "confidence": "high",
        "breadth": {"affected": 1, "applicable": 1, "unit": "pages"},
        "evidence": "1 of 1 sampled pages exhibits the defect.",
        "evidence_items": [],
        "mechanism": "It breaks the thing.",
        "affected_urls": ["https://example.test/"],
        "remediation": {"summary": "Fix it.", "steps": ["Do the thing."],
                        "expected_benefit": "It works.", "effort": "low"},
        "dedupe_key": check_id,
        "tags": [],
        "applicability": "",
        "supersedes": [],
        "producer": "test-skill",
    }
    breadth = overrides.pop("breadth", None)
    if breadth:
        check["breadth"].update(breadth)
    check.update(overrides)
    return check


def make_document(checks, producer="test-skill") -> dict:
    return {"schema_version": "1.0", "producer": producer,
            "produced_at": "2026-09-04T00:00:00Z", "checks": checks,
            "notes": [], "errors": []}


def minimal_snapshot(**overrides) -> dict:
    """A snapshot with the shape analyzers expect but no pages."""
    snapshot = {
        "schema_version": "1.0",
        "site": "example.test",
        "target_url": "https://example.test/",
        "final_url": "https://example.test/",
        "origin": "https://example.test",
        "captured_at": "2026-09-04T00:00:00Z",
        "config": {"max_pages": 20, "max_depth": 2},
        "robots": {"outcome": "missing", "url": "https://example.test/robots.txt",
                   "status": 404, "sitemaps": [], "groups": [], "group_count": 0},
        "robots_agent_matrix": {},
        "sitemaps": [], "sitemap_urls": [], "sitemap_url_total": 0,
        "well_known": {}, "not_found_probe": {"evaluated": False},
        "pages": [], "link_checks": [],
        "link_graph": {"internal_edges": [], "external_hosts": {},
                       "orphan_sitemap_urls": []},
        "coverage": {"pages_attempted": 0, "pages_fetched": 0},
        "http_stats": {"requests": 0, "bytes": 0},
        "notes": [], "errors": [],
    }
    snapshot.update(overrides)
    if "transport" not in overrides:
        snapshot["transport"] = transport_block(snapshot["pages"])
    return snapshot


def transport_block(pages, *, failures=None, detail="") -> dict:
    """Build the crawl's transport diagnosis the way `Crawl` would.

    `target_responded` is what every analyzer gates on: findings about a site
    require at least one HTTP response from that site.
    """
    responded = sum(1 for p in pages
                    if isinstance(p, dict) and p.get("status") is not None)
    failures = dict(failures or {})
    dominant = next(iter(failures), "")
    if responded and not failures:
        state = "ok"
    elif responded:
        state = "partial"
    elif failures:
        state = dominant
    else:
        state = "not_attempted"
    return {
        "target_responded": responded > 0,
        "responses_received": responded,
        "transport_failures": failures,
        "failed_requests": sum(failures.values()),
        "dominant_failure": dominant,
        "state": state,
        "detail": detail or ("The target origin responded to every request."
                             if state == "ok" else
                             "No HTTP response was received from the target origin."),
    }


def make_page(url="https://example.test/", *, depth=0, status=200, html=None,
              extraction=None, **overrides) -> dict:
    """Build a snapshot page record, optionally by parsing real HTML."""
    from auditlib import htmlparse
    from auditlib.urls import resolve, same_site

    page = {
        "url": url, "requested_url": url, "depth": depth, "discovery": "seed",
        "discovered_from": "", "anchor_text": "", "priority_score": 100,
        "status": status, "ok": 200 <= status < 300, "content_type": "text/html",
        "bytes": len(html or ""), "truncated": False, "elapsed_ms": 10,
        "redirects": [], "headers": {}, "error": "", "error_kind": "",
        "internal_links": [], "external_links": [],
    }
    if html is not None:
        ex = htmlparse.extract(html)
        page["extraction"] = ex.to_dict()
        for anchor in ex.anchors:
            target = resolve(url, anchor["href"])
            if not target:
                continue
            entry = {"url": target, "text": anchor["text"],
                     "region": anchor["region"], "rel": anchor["rel"]}
            key = "internal_links" if same_site(target, url) else "external_links"
            page[key].append(entry)
    if extraction is not None:
        page["extraction"] = extraction
    page.update(overrides)
    return page
