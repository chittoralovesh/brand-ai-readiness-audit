#!/usr/bin/env python3
"""End-to-end audit: crawl once, run every analyzer skill, emit one report.

    python run_audit.py --url https://example.com --out report.json

This is the deterministic core of the entrypoint skill. It performs the single
bounded crawl, resolves the analyzer skills through `marketplace.json`, runs
each one in its own process over the shared snapshot, then de-duplicates,
grades, prioritises, validates and emits one audit report.

Failure containment is a design requirement, not an afterthought: an analyzer
that crashes, hangs or is missing becomes a recorded coverage limitation, and
the report is still produced. The only thing that can stop a report being
written is the process being killed.

Runtime: designed for well under 180 s, with a hard internal deadline
(default 240 s) that reserves time for analysis and serialisation.

Exit codes: 0 report written; 2 the input URL could not be used (a valid,
empty report is still written).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auditlib import compose, registry, schema                      # noqa: E402
from auditlib.budget import Budget                                  # noqa: E402
from auditlib.crawler import Crawl, MAX_DEPTH, MAX_PAGES            # noqa: E402
from auditlib.fetcher import DEFAULT_USER_AGENT, Fetcher            # noqa: E402
from auditlib.urls import coerce_input_url                          # noqa: E402

# Which script inside each analyzer skill produces check results. The manifest
# says *which skills exist*; this table says how to run the ones that analyse a
# snapshot. A skill listed in the manifest but absent here is simply not an
# analyzer (the orchestrator itself, for instance).
ANALYZERS = {
    "crawl-render-audit": "scripts/check_crawl_render.py",
    "ai-citability-audit": "scripts/check_citability.py",
    "freshness-corroboration": "scripts/check_freshness.py",
    "engagement-audit": "scripts/check_engagement.py",
}
DEFAULT_DEADLINE = 240.0


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_analyzer(skill_id: str, script: Path, snapshot_json: str, timeout: float,
                 research: str = "") -> tuple:
    """Run one analyzer skill in its own process. Never raises.

    The snapshot is handed over on stdin rather than through a file. Process
    isolation between skills is deliberate - it is what makes one analyzer
    crashing survivable - but a shared file is not: two audits running in the
    same directory would race on it. Piping keeps the isolation and removes the
    shared resource entirely, so concurrent audits cannot interfere.
    """
    if not script.is_file():
        return skill_id, None, "script not found at %s" % script
    command = [sys.executable, str(script.resolve()), "--snapshot", "-", "--out", "-"]
    if research and skill_id == "freshness-corroboration":
        command += ["--research", str(Path(research).resolve())]
    try:
        completed = subprocess.run(
            command, input=snapshot_json, capture_output=True, text=True,
            timeout=max(5.0, timeout), encoding="utf-8", errors="replace",
            check=False)
    except subprocess.TimeoutExpired:
        return skill_id, None, "exceeded its %.0fs share of the time budget" % timeout
    except Exception as exc:                                        # pragma: no cover
        return skill_id, None, "%s: %s" % (type(exc).__name__, str(exc)[:200])
    if completed.returncode != 0:
        return skill_id, None, "exited with code %d: %s" % (
            completed.returncode, (completed.stderr or "").strip()[:300])
    try:
        document = json.loads(completed.stdout)
    except Exception as exc:
        return skill_id, None, "returned unparseable output (%s)" % str(exc)[:160]
    if not isinstance(document, dict) or "checks" not in document:
        return skill_id, None, "returned a document without a `checks` array"
    document.setdefault("producer", skill_id)
    return skill_id, document, ""


def build_agent_context(snapshot: dict, check_documents: list, report: dict) -> dict:
    """A small packet for the optional agent-side semantic pass.

    Deliberately compact: the point of the deterministic core is that the model
    never has to read the crawl. It receives only the excerpts a human reviewer
    would need to judge clarity, plus the checks that asked for judgement.
    """
    pages = [p for p in snapshot.get("pages", [])
             if p.get("ok") and isinstance(p.get("extraction"), dict)
             and not p["extraction"].get("non_html")]
    entry = next((p for p in pages if p.get("depth") == 0), None)

    def digest(page, limit=420):
        e = page.get("extraction", {})
        return {
            "url": page["url"],
            "title": e.get("title"),
            "h1": next((h["text"] for h in (e.get("headings") or []) if h["level"] == 1), ""),
            "meta_description": (e.get("meta_name") or {}).get("description", ""),
            "words": e.get("main_word_count", 0),
            "opening_text": (e.get("first_screen_text") or "")[:limit],
        }

    review_requests = []
    for document in check_documents:
        for check in document.get("checks", []):
            if "semantic-review" in (check.get("tags") or []):
                review_requests.append({
                    "check_id": check["check_id"],
                    "produced_by": document.get("producer"),
                    "status": check["status"],
                    "title": check["title"],
                    "evidence": check.get("evidence", "")[:300],
                    "question": check.get("applicability") or "",
                })
    identity = []
    for page in pages[:4]:
        for raw in (page["extraction"].get("jsonld_blocks") or [])[:2]:
            identity.append(raw[:600])
    return {
        "schema_version": "1.0",
        "site": snapshot.get("site"),
        "audited_url": snapshot.get("final_url"),
        "entry_page": digest(entry, 700) if entry else None,
        "sampled_pages": [digest(p) for p in pages[:6]],
        "navigation_labels": sorted({
            (a.get("text") or "").strip()
            for p in pages for a in (p["extraction"].get("anchors") or [])
            if a.get("region") == "nav" and (a.get("text") or "").strip()})[:25],
        "action_labels": sorted({
            (a.get("text") or "").strip()
            for p in pages for a in (p["extraction"].get("anchors") or [])
            if a.get("button_like") and (a.get("text") or "").strip()})[:15],
        "declared_identity_jsonld": identity[:4],
        "checks_requesting_semantic_review": review_requests,
        "deterministic_findings": [
            {"id": f["id"], "check_id": f["check_id"], "severity": f["severity"],
             "title": f["title"]} for f in report.get("findings", [])],
        "instructions_ref": "skills/audit-orchestrator/references/semantic-review.md",
    }


def empty_report(target: str, reason: str, budget: Budget) -> dict:
    report = {
        "schema_version": "1.0",
        "report_type": "brand-ai-readiness-audit",
        "site": target or "unknown-site",
        "audited_url": target,
        "audited_at": _now(),
        "summary": {"total_findings": 0, "critical": 0, "high": 0, "medium": 0, "low": 0},
        "findings": [],
        "root_cause_analysis": [],
        "proactive_recommendations": [],
        "coverage": {"checks_by_status": {}, "checks_total": 0, "skills_reporting": [],
                     "checks": [], "crawl": {}},
        "limitations": [{"scope": "input", "status": "NOT_EVALUATED",
                         "title": "The audit could not start", "reason": reason}],
        "finding_rollups": [],
        "audit_metrics": {"duration_ms": budget.duration_ms(), "pages_attempted": 0,
                          "pages_fetched": 0, "checks_completed": 0},
    }
    return schema.validate_and_repair(report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="the site to audit")
    parser.add_argument("--out", default="report.json", help="report path, or - for stdout")
    parser.add_argument("--snapshot-out", default="", help="also write the raw snapshot here")
    parser.add_argument("--agent-context-out", default="",
                        help="write the compact packet for the optional semantic pass")
    parser.add_argument("--research", default="",
                        help="optional off-site research JSON for freshness-corroboration")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    parser.add_argument("--budget", type=float, default=DEFAULT_DEADLINE,
                        help="hard internal deadline in seconds (default 240)")
    parser.add_argument("--timeout", type=float, default=8.0, help="per-request timeout")
    parser.add_argument("--rate", type=float, default=2.0, help="requests/second/host")
    parser.add_argument("--user-agent", default=os.environ.get("AUDIT_USER_AGENT",
                                                               DEFAULT_USER_AGENT))
    parser.add_argument("--marketplace-root", default="",
                        help="override marketplace root discovery (used by the test suite)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    budget = Budget(total=max(30.0, args.budget))
    limitations: list = []

    def log(message):
        if not args.quiet:
            sys.stderr.write("[%6.1fs] %s\n" % (budget.elapsed(), message))

    target = coerce_input_url(args.url)
    if not target:
        report = empty_report(args.url, "%r is not a usable absolute HTTP(S) URL. Provide a "
                                        "URL such as https://example.com." % args.url, budget)
        _write(args.out, report)
        sys.stderr.write("error: %r is not a usable URL\n" % args.url)
        return 2

    # ---------------------------------------------------------- phase 1: crawl
    log("crawling %s" % target)
    fetcher = Fetcher(user_agent=args.user_agent, timeout=args.timeout,
                      rate_per_sec=args.rate)
    crawl = Crawl(target, budget=budget, fetcher=fetcher,
                  max_pages=max(1, args.max_pages), max_depth=max(0, args.max_depth))
    try:
        crawl.discover()
        budget.mark("discovery_ms")
        crawl.run()
    except Exception as exc:                                        # pragma: no cover
        limitations.append({"scope": "crawl", "status": "PARTIAL",
                            "title": "The crawl ended early after an internal error",
                            "reason": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
    budget.mark("crawl_ms")
    snapshot = crawl.snapshot()
    log("crawl complete: %d page(s), %d link probe(s), %d request(s)"
        % (len(snapshot.get("pages", [])), len(snapshot.get("link_checks", [])),
           snapshot.get("http_stats", {}).get("requests", 0)))

    # The snapshot lives in memory for the whole run. It only touches disk when
    # the operator explicitly asks for it with --snapshot-out, at a path they
    # chose; the audit itself never creates a shared temporary file, so any
    # number of audits can run concurrently in the same directory.
    try:
        snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    except Exception as exc:
        sys.stderr.write("error: the crawl snapshot could not be serialised (%s)\n" % exc)
        report = empty_report(target,
                              "the crawl snapshot could not be serialised: %s" % exc,
                              budget)
        _write(args.out, report)
        return 2
    if args.snapshot_out:
        try:
            destination = Path(args.snapshot_out)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(snapshot_json, encoding="utf-8")
        except Exception as exc:
            sys.stderr.write("warning: could not write --snapshot-out (%s)\n" % exc)

    # ------------------------------------------------------- phase 2: analyze
    try:
        root = Path(args.marketplace_root) if args.marketplace_root else None
        manifest = registry.load_manifest(root)
        manifest_errors = registry.validate_manifest(manifest)
        paths = registry.skill_paths(manifest)
    except Exception as exc:
        manifest, manifest_errors, paths = {}, ["%s" % exc], {}
    if manifest_errors:
        limitations.append({"scope": "marketplace", "status": "PARTIAL",
                            "title": "The marketplace manifest reported problems",
                            "reason": "; ".join(manifest_errors)[:400]})

    jobs = []
    for skill_id, relative in ANALYZERS.items():
        skill_dir = paths.get(skill_id)
        if skill_dir is None:
            limitations.append({
                "scope": skill_id, "status": "NOT_EVALUATED",
                "title": "Analyzer skill %s was not run" % skill_id,
                "reason": "the skill is not listed in marketplace.json, so its checks are "
                          "absent from this report"})
            continue
        jobs.append((skill_id, skill_dir / relative))

    share = max(10.0, (budget.remaining() - 20.0) / max(1, len(jobs)))
    documents = []
    if jobs and budget.allow_analysis():
        log("running %d analyzer skill(s) (%.0fs each at most)" % (len(jobs), share))
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            futures = [pool.submit(run_analyzer, sid, path, snapshot_json, share,
                                   args.research) for sid, path in jobs]
            for future in futures:
                skill_id, document, error = future.result()
                if document is None:
                    limitations.append({
                        "scope": skill_id, "status": "NOT_EVALUATED",
                        "title": "Analyzer skill %s did not return results" % skill_id,
                        "reason": error})
                    log("  %s FAILED: %s" % (skill_id, error))
                    continue
                documents.append(document)
                inner = document.get("errors") or []
                if inner:
                    limitations.append({
                        "scope": skill_id, "status": "PARTIAL",
                        "title": "Some checks inside %s did not complete" % skill_id,
                        "reason": "; ".join("%s: %s" % (e.get("check"), e.get("error"))
                                            for e in inner)[:400]})
                log("  %s: %d check(s)" % (skill_id, len(document.get("checks", []))))
    else:
        limitations.append({
            "scope": "analysis", "status": "NOT_EVALUATED",
            "title": "Analysis was skipped to protect the time budget",
            "reason": "only %.0fs of the %.0fs budget remained after crawling."
                      % (budget.remaining(), budget.total)})
    budget.mark("analysis_ms")

    # ------------------------------------------------------- phase 3: compose
    transport = snapshot.get("transport", {}) or {}
    if not transport.get("target_responded"):
        limitations.append({
            "scope": "transport", "status": "NOT_EVALUATED",
            "title": "No HTTP response was received from the target origin",
            "reason": "%s Findings about the site cannot be derived from a failure "
                      "that never reached it, so the site-specific checks are reported "
                      "as not evaluated rather than as defects."
                      % transport.get("detail", "")})
    elif transport.get("failed_requests"):
        limitations.append({
            "scope": "transport", "status": "PARTIAL",
            "title": "Some requests failed before a response was received",
            "reason": "%s Checks that depend on those URLs are reported from the "
                      "responses that did arrive." % transport.get("detail", "")})

    metrics = {
        "duration_ms": budget.duration_ms(),
        "budget_ms": int(budget.total * 1000),
        "phase_ms": budget.marks(),
        "pages_attempted": snapshot.get("coverage", {}).get("pages_attempted", 0),
        "pages_fetched": snapshot.get("coverage", {}).get("pages_fetched", 0),
        "pages_skipped_by_robots": snapshot.get("coverage", {}).get("pages_skipped_by_robots", 0),
        "pages_skipped_unsafe": snapshot.get("coverage", {}).get("pages_skipped_unsafe", 0),
        "pages_skipped_for_time": snapshot.get("coverage", {}).get("pages_skipped_for_time", 0),
        "links_probed": snapshot.get("coverage", {}).get("links_probed", 0),
        "rendered_pages": 0,
        "transport_state": transport.get("state", ""),
        "target_responded": bool(transport.get("target_responded")),
        "transport_failures": transport.get("transport_failures", {}),
        "http_requests": snapshot.get("http_stats", {}).get("requests", 0),
        "bytes_downloaded": snapshot.get("http_stats", {}).get("bytes", 0),
        "skills_invoked": sorted(sid for sid, _ in jobs),
        "skills_reporting": sorted(d.get("producer", "?") for d in documents),
        "checks_completed": sum(len(d.get("checks", [])) for d in documents),
        "checks_skipped_for_time": 0,
    }
    try:
        report = compose.build_report(snapshot, documents, metrics=metrics,
                                      limitations_extra=limitations)
    except Exception as exc:                                        # pragma: no cover
        sys.stderr.write("error: report composition failed (%s)\n" % exc)
        report = empty_report(target, "report composition failed: %s: %s"
                              % (type(exc).__name__, exc), budget)
        report["audit_metrics"] = metrics
    report["audit_metrics"]["duration_ms"] = budget.duration_ms()
    report["engine"] = {
        "marketplace": manifest.get("name", "brand-ai-readiness-audit"),
        "version": manifest.get("version", "1.0.0"),
        "entrypoint": registry.entrypoint_id(manifest) or "audit-orchestrator",
        "user_agent": args.user_agent,
        "read_only": True,
        "robots_respected": True,
    }
    report = schema.validate_and_repair(report)
    budget.mark("compose_ms")
    report["audit_metrics"]["phase_ms"] = budget.marks()
    report["audit_metrics"]["duration_ms"] = budget.duration_ms()

    if args.agent_context_out:
        try:
            Path(args.agent_context_out).write_text(
                json.dumps(build_agent_context(snapshot, documents, report), indent=2,
                           ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            sys.stderr.write("warning: could not write the agent context (%s)\n" % exc)

    _write(args.out, report)
    log("done: %d finding(s) in %.1fs (valid=%s)"
        % (report["summary"]["total_findings"], budget.elapsed(),
           report["report_validation"]["valid"]))
    return 0


def _write(destination: str, report: dict) -> None:
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if destination in ("-", "", None):
        sys.stdout.write(payload + "\n")
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
