#!/usr/bin/env python3
"""Compose one audit report from a snapshot and one or more check documents.

    python compose_report.py --snapshot snapshot.json \
        --checks crawl.json citability.json freshness.json engagement.json \
        --out report.json

`run_audit.py` does this as its final phase. This script exposes the same
composition on its own, which is what makes the pipeline inspectable: you can
re-compose from saved check documents and see exactly how de-duplication,
rollup, grading and ordering changed the output, without touching the network.

A check document that is missing or unreadable becomes a coverage limitation.
The report is still produced.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auditlib import compose, schema                                # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--checks", nargs="*", default=[])
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)

    limitations = []
    try:
        with open(args.snapshot, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except Exception as exc:
        sys.stderr.write("error: could not read the snapshot (%s)\n" % exc)
        return 2

    documents = []
    for path in args.checks:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
            if not isinstance(document, dict) or "checks" not in document:
                raise ValueError("no `checks` array")
            documents.append(document)
        except Exception as exc:
            limitations.append({
                "scope": Path(path).stem, "status": "NOT_EVALUATED",
                "title": "Check document %s could not be used" % path,
                "reason": "%s: %s" % (type(exc).__name__, str(exc)[:200])})

    metrics = {
        "duration_ms": 0,
        "pages_attempted": snapshot.get("coverage", {}).get("pages_attempted", 0),
        "pages_fetched": snapshot.get("coverage", {}).get("pages_fetched", 0),
        "rendered_pages": 0,
        "checks_completed": sum(len(d.get("checks", [])) for d in documents),
        "checks_skipped_for_time": 0,
        "composed_offline": True,
    }
    report = compose.build_report(snapshot, documents, metrics=metrics,
                                  limitations_extra=limitations)
    report = schema.validate_and_repair(report)

    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out in ("-", "", None):
        sys.stdout.write(payload + "\n")
    else:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
