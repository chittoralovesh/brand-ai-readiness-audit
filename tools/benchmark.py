#!/usr/bin/env python3
"""Measure audit runtime and where it goes.

    python tools/benchmark.py                         # synthetic sites, no network
    python tools/benchmark.py --runs 3
    python tools/benchmark.py --url https://example.com --url https://example.org
    python tools/benchmark.py --json bench.json

Two modes.

**Fixture mode (default).** Runs against locally-generated synthetic sites of
increasing size and shape - small, wide, deep, JavaScript-only, slow-responding.
It needs no network, is deterministic, and isolates the parts of the runtime we
control (parsing, analysis, composition) from network latency, which we do not.

**Live mode (`--url`).** Runs the real pipeline against sites you name. No site
is ever hard-coded here: the whole point of the exercise is that the marketplace
generalises, so the targets are yours to choose. Live runs obey `robots.txt`,
the rate limits and the read-only guarantee exactly as a graded run would.

Reported per run: wall-clock time, the orchestrator's own phase breakdown
(discovery / crawl / analysis / compose), pages attempted and fetched, requests
issued, bytes processed, findings produced, and any work skipped for time.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))

RUN_AUDIT = ROOT / "skills" / "audit-orchestrator" / "scripts" / "run_audit.py"
ADOBE_LIMIT_S = 300.0
DESIGN_TARGET_S = 180.0


# --------------------------------------------------------------------------
# Synthetic sites. Each isolates a different cost profile.
# --------------------------------------------------------------------------

def build_fixtures():
    from fixture_server import CSR_SHELL, Route, RICH_PARAGRAPH, page

    def linked(paths):
        return "".join('<a href="%s">%s</a>' % (p, p.strip("/") or "home") for p in paths)

    fixtures = {}

    # 1. Small brochure site: the common case.
    small = {"/robots.txt": Route("User-agent: *\nAllow: /\n", content_type="text/plain"),
             "/": Route(page("Home", body=RICH_PARAGRAPH * 2 +
                             linked(["/about", "/pricing", "/contact"])))}
    for path in ("/about", "/pricing", "/contact"):
        small[path] = Route(page(path.strip("/").title(), body=RICH_PARAGRAPH * 2))
    fixtures["small (4 pages)"] = small

    # 2. Wide site: exercises page selection and the page cap.
    wide = {"/robots.txt": Route("User-agent: *\nAllow: /\n", content_type="text/plain"),
            "/": Route(page("Home", body=RICH_PARAGRAPH +
                            linked(["/p%d" % i for i in range(60)])))}
    for i in range(60):
        wide["/p%d" % i] = Route(page("Page %d" % i, body=RICH_PARAGRAPH * 3 +
                                      linked(["/p%d" % ((i + 1) % 60)])))
    fixtures["wide (60 pages offered, 20 crawled)"] = wide

    # 3. Deep site: exercises the depth limit.
    deep = {"/robots.txt": Route("User-agent: *\nAllow: /\n", content_type="text/plain")}
    deep["/"] = Route(page("Home", body=RICH_PARAGRAPH + linked(["/a"])))
    path = ""
    for level in range(8):
        path += "/a"
        deep[path] = Route(page("Level %d" % level,
                                body=RICH_PARAGRAPH + linked([path + "/a"])))
    fixtures["deep (8 levels, depth capped at 2)"] = deep

    # 4. Heavy pages: exercises parsing cost rather than network cost.
    heavy_body = (RICH_PARAGRAPH * 40
                  + "".join('<img src="/i%d.png" alt="Figure %d">' % (i, i)
                            for i in range(120))
                  + "".join('<a href="/h%d">Detailed section %d</a>' % (i, i)
                            for i in range(200)))
    heavy = {"/robots.txt": Route("User-agent: *\nAllow: /\n", content_type="text/plain"),
             "/": Route(page("Heavy", body=heavy_body))}
    for i in range(25):
        heavy["/h%d" % i] = Route(page("Section %d" % i, body=heavy_body))
    fixtures["heavy (large documents)"] = heavy

    # 5. JavaScript-only: the render-gap path.
    csr = {"/robots.txt": Route("User-agent: *\nAllow: /\n", content_type="text/plain"),
           "/": Route(CSR_SHELL)}
    fixtures["client-rendered shell"] = csr

    # 6. Slow server: exercises timeouts and the budget, not our own code.
    slow = {"/robots.txt": Route("User-agent: *\nAllow: /\n",
                                 content_type="text/plain", delay=0.4),
            "/": Route(page("Slow", body=RICH_PARAGRAPH * 2 +
                            linked(["/a", "/b", "/c"])), delay=0.8)}
    for path in ("/a", "/b", "/c"):
        slow[path] = Route(page(path, body=RICH_PARAGRAPH), delay=0.8)
    fixtures["slow server (0.8s per response)"] = slow

    return fixtures


# --------------------------------------------------------------------------

def run_once(url, *, rate, budget, timeout):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, str(RUN_AUDIT), "--url", url, "--out", str(out),
             "--rate", str(rate), "--budget", str(budget), "--timeout", str(timeout),
             "--quiet"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=budget + 120)
        wall = time.monotonic() - started
        if completed.returncode not in (0, 2) or not out.exists():
            return {"error": (completed.stderr or "").strip()[-300:] or "no report",
                    "wall_s": wall}
        report = json.loads(out.read_text(encoding="utf-8"))

    metrics = report.get("audit_metrics", {})
    phases = metrics.get("phase_ms", {})
    crawl_ms = phases.get("crawl_ms", 0)
    return {
        "wall_s": round(wall, 2),
        "audit_s": round(metrics.get("duration_ms", 0) / 1000.0, 2),
        "discovery_s": round(phases.get("discovery_ms", 0) / 1000.0, 2),
        "crawl_s": round(crawl_ms / 1000.0, 2),
        "analysis_s": round(max(0, phases.get("analysis_ms", 0) - crawl_ms) / 1000.0, 2),
        "compose_s": round(max(0, phases.get("compose_ms", 0)
                               - phases.get("analysis_ms", 0)) / 1000.0, 2),
        "pages_attempted": metrics.get("pages_attempted", 0),
        "pages_fetched": metrics.get("pages_fetched", 0),
        "links_probed": metrics.get("links_probed", 0),
        "requests": metrics.get("http_requests", 0),
        "kb": round(metrics.get("bytes_downloaded", 0) / 1024.0, 1),
        "checks": metrics.get("checks_completed", 0),
        "findings": report.get("summary", {}).get("total_findings", 0),
        "skipped_for_time": metrics.get("pages_skipped_for_time", 0),
        "skills_reporting": len(metrics.get("skills_reporting", [])),
        "valid": report.get("report_validation", {}).get("valid", False),
    }


def summarise(runs):
    numeric = [r for r in runs if "error" not in r]
    if not numeric:
        return {"error": runs[0].get("error", "all runs failed")}
    out = {}
    for key in numeric[0]:
        values = [r[key] for r in numeric if isinstance(r[key], (int, float))]
        if values:
            out[key] = round(statistics.median(values), 2)
    out["runs"] = len(numeric)
    out["max_wall_s"] = max(r["wall_s"] for r in numeric)
    out["valid"] = all(r.get("valid") for r in numeric)
    return out


HEADER = ("%-38s %8s %8s %8s %8s %8s %6s %6s %8s %6s"
          % ("target", "wall s", "crawl s", "analys s", "comp s", "reqs",
             "pages", "checks", "KB", "finds"))


def render(label, stats):
    if "error" in stats:
        return "%-38s  FAILED: %s" % (label[:38], stats["error"][:60])
    return ("%-38s %8.2f %8.2f %8.2f %8.2f %8d %6d %6d %8.1f %6d"
            % (label[:38], stats["wall_s"], stats["crawl_s"], stats["analysis_s"],
               stats["compose_s"], stats["requests"], stats["pages_fetched"],
               stats["checks"], stats["kb"], stats["findings"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", action="append", default=[],
                        help="audit a live site (repeatable); omit for fixture mode")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--budget", type=float, default=240.0)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--rate", type=float, default=0.0,
                        help="requests/second/host; defaults to 2 live, 50 on fixtures")
    parser.add_argument("--json", default="", help="also write the raw results here")
    args = parser.parse_args(argv)

    results = {}
    print(HEADER)
    print("-" * len(HEADER))

    if args.url:
        rate = args.rate or 2.0
        print("live mode: robots.txt, rate limits and the read-only guarantee all apply\n")
        for url in args.url:
            runs = [run_once(url, rate=rate, budget=args.budget, timeout=args.timeout)
                    for _ in range(args.runs)]
            results[url] = summarise(runs)
            print(render(url, results[url]))
    else:
        from fixture_server import FixtureSite

        rate = args.rate or 50.0
        for label, routes in build_fixtures().items():
            with FixtureSite(routes) as site:
                runs = [run_once(site.url, rate=rate, budget=args.budget,
                                 timeout=args.timeout) for _ in range(args.runs)]
            results[label] = summarise(runs)
            print(render(label, results[label]))

    print("-" * len(HEADER))
    worst = max((s.get("max_wall_s", s.get("wall_s", 0))
                 for s in results.values() if "error" not in s), default=0.0)
    all_valid = all(s.get("valid") for s in results.values() if "error" not in s)
    print("slowest target: %.2f s   design target: %.0f s   Adobe limit: %.0f s"
          % (worst, DESIGN_TARGET_S, ADOBE_LIMIT_S))
    print("every report schema-valid: %s" % ("yes" if all_valid else "NO"))
    if worst >= ADOBE_LIMIT_S:
        print("FAIL: a target exceeded the 5-minute ceiling")
        return 1
    if worst >= DESIGN_TARGET_S:
        print("WARN: above the 180 s design target, still inside the ceiling")
    else:
        print("PASS: comfortably inside the ceiling (%.0f%% headroom)"
              % (100 * (1 - worst / ADOBE_LIMIT_S)))

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print("raw results written to %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
