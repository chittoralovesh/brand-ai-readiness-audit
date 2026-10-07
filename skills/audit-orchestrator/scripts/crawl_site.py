#!/usr/bin/env python3
"""Produce a site snapshot without analysing it.

    python crawl_site.py --url https://example.com --out snapshot.json

Useful for three things: inspecting exactly what the audit saw, re-running the
analyzer skills without touching the site again, and capturing a fixture for the
test suite. The crawl performed here is identical to the one `run_audit.py`
performs - same limits, same robots handling, same read-only guarantee.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auditlib.budget import Budget                                  # noqa: E402
from auditlib.crawler import Crawl, MAX_DEPTH, MAX_PAGES            # noqa: E402
from auditlib.fetcher import DEFAULT_USER_AGENT, Fetcher            # noqa: E402
from auditlib.urls import coerce_input_url                          # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", default="-", help="snapshot path, or - for stdout")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    parser.add_argument("--budget", type=float, default=180.0)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--rate", type=float, default=2.0)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args(argv)

    target = coerce_input_url(args.url)
    if not target:
        sys.stderr.write("error: %r is not a usable absolute HTTP(S) URL\n" % args.url)
        return 2

    budget = Budget(total=max(20.0, args.budget))
    crawl = Crawl(target, budget=budget,
                  fetcher=Fetcher(user_agent=args.user_agent, timeout=args.timeout,
                                  rate_per_sec=args.rate),
                  max_pages=max(1, args.max_pages), max_depth=max(0, args.max_depth))
    crawl.discover()
    crawl.run()
    snapshot = crawl.snapshot()
    snapshot["capture_duration_ms"] = budget.duration_ms()

    payload = json.dumps(snapshot, indent=2, ensure_ascii=False)
    if args.out in ("-", "", None):
        sys.stdout.write(payload + "\n")
    else:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        sys.stderr.write("wrote %s (%d page(s), %.1fs)\n"
                         % (path, len(snapshot.get("pages", [])), budget.elapsed()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
