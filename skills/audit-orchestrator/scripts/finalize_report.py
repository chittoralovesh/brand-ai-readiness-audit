#!/usr/bin/env python3
"""Merge agent-authored semantic findings into an existing report.

    python finalize_report.py --report report.json \
        --agent-findings mine.json --out report.json

The optional semantic pass (see `references/semantic-review.md`) can add or
sharpen a small number of judgement-based findings. Rather than letting a model
edit the report directly - which would risk breaking the schema, the ordering or
the severity counts - it writes findings in the check-result shape and this
script re-applies the deterministic pipeline over them:

  * grade with the one severity model, capped by the declared confidence;
  * merge into an existing finding when the `dedupe_key` matches, instead of
    adding a second entry for the same defect;
  * re-sort deterministically, reassign `F-NNN`, recount the summary;
  * validate, repair if necessary, re-validate.

Anything malformed in the agent input is dropped with a recorded reason. The
report that comes out is always schema-valid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auditlib import compose, schema                                # noqa: E402
from auditlib import severity as sev                                # noqa: E402

REQUIRED = ("check_id", "title", "evidence", "remediation")
# A judgement call is never an emergency: the semantic pass may not exceed
# `high`, whatever it claims about itself.
MAX_IMPACT = 3
MAX_CONFIDENCE = "medium"


def normalise(raw: dict, index: int) -> tuple:
    """Return (check-shaped record, error). Never raises."""
    if not isinstance(raw, dict):
        return None, "agent finding %d is not an object" % index
    missing = [f for f in REQUIRED if not raw.get(f)]
    if missing:
        return None, "agent finding %d is missing %s" % (index, ", ".join(missing))
    remediation = raw.get("remediation") or {}
    if not isinstance(remediation, dict) or not (remediation.get("summary") or "").strip():
        return None, "agent finding %d has no remediation summary" % index
    check_id = str(raw["check_id"])
    if not check_id.startswith("AG-"):
        check_id = "AG-" + check_id.split("-", 1)[-1]
    breadth = raw.get("breadth") or {}
    confidence = str(raw.get("confidence", "medium")).lower()
    if confidence not in ("medium", "low"):
        confidence = MAX_CONFIDENCE
    record = {
        "check_id": check_id,
        "title": str(raw["title"])[:200],
        "pillar": raw.get("pillar") if raw.get("pillar") in
                  ("ai_discoverability", "on_site_engagement") else "ai_discoverability",
        "category": str(raw.get("category", "semantic_review")),
        "status": "FAIL",
        "impact_base": max(0, min(MAX_IMPACT, int(raw.get("impact_base", 2) or 2))),
        "confidence": confidence,
        "breadth": {"affected": int(breadth.get("affected", 1) or 1),
                    "applicable": int(breadth.get("applicable", 1) or 1),
                    "unit": str(breadth.get("unit", "pages"))},
        "evidence": str(raw["evidence"]),
        "evidence_items": raw.get("evidence_items") or [],
        "mechanism": str(raw.get("mechanism", "")),
        "affected_urls": raw.get("affected_urls") or [],
        "remediation": {
            "summary": str(remediation.get("summary")),
            "steps": [str(s) for s in (remediation.get("steps") or [])][:8],
            "expected_benefit": str(remediation.get("expected_benefit", "")),
            "effort": str(remediation.get("effort", "medium")).lower(),
        },
        "dedupe_key": str(raw.get("dedupe_key") or check_id),
        "tags": sorted(set((raw.get("tags") or []) + ["semantic-review"])),
        "applicability": str(raw.get("applicability", "")),
        "producer": "agent-semantic-review",
        "supersedes": [],
    }
    return record, ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--agent-findings", required=True)
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)

    try:
        with open(args.report, "r", encoding="utf-8") as handle:
            report = json.load(handle)
    except Exception as exc:
        sys.stderr.write("error: could not read the report (%s)\n" % exc)
        return 2

    problems = []
    try:
        with open(args.agent_findings, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        raw_findings = payload.get("findings") if isinstance(payload, dict) else payload
        raw_findings = raw_findings if isinstance(raw_findings, list) else []
    except Exception as exc:
        raw_findings = []
        problems.append("agent findings file could not be read: %s" % str(exc)[:160])

    accepted = []
    for index, raw in enumerate(raw_findings):
        record, error = normalise(raw, index)
        if record is None:
            problems.append(error)
            continue
        accepted.append(record)

    findings = [f for f in report.get("findings", []) if isinstance(f, dict)]
    by_key = {f.get("dedupe_key"): f for f in findings}

    added, merged = 0, 0
    for record in accepted:
        built = compose.build_finding(record)
        existing = by_key.get(record["dedupe_key"])
        if existing is not None:
            # Same defect: strengthen the existing finding rather than repeat it.
            existing.setdefault("merged_from", []).append({
                "check_id": record["check_id"], "producer": record["producer"],
                "pillar": record["pillar"], "evidence": record["evidence"]})
            existing["evidence"] = (existing.get("evidence", "").rstrip()
                                    + " Semantic review adds: " + record["evidence"])
            if sev.SEVERITY_RANK.get(built["severity"], 0) > \
                    sev.SEVERITY_RANK.get(existing.get("severity", "low"), 0):
                existing["severity"] = built["severity"]
                existing["severity_score"] = built["severity_score"]
                existing["severity_rationale"] = (
                    built["severity_rationale"] + "; raised by the semantic review pass")
            merged += 1
            continue
        findings.append(built)
        by_key[record["dedupe_key"]] = built
        added += 1

    report["findings"] = compose.assign_ids(findings)
    report["summary"] = {**report.get("summary", {}), **compose.summarise(report["findings"])}
    report["summary"]["by_category"] = {
        compose.PILLAR_LABEL[k]: sum(1 for f in report["findings"] if f.get("category") == k)
        for k in ("ai_discoverability", "on_site_engagement")}
    report["executive_summary"] = compose.executive_summary(
        report["findings"], report.get("root_cause_analysis", []),
        report.get("proactive_recommendations", []), {})
    report.setdefault("limitations", []).append({
        "scope": "semantic-review", "status": "PARTIAL" if problems else "COMPLETE",
        "title": "Agent semantic review pass",
        "reason": "%d finding(s) added, %d merged into existing findings.%s"
                  % (added, merged,
                     " Rejected: " + "; ".join(problems) if problems else "")})
    report = schema.validate_and_repair(report)

    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out in ("-", "", None):
        sys.stdout.write(payload + "\n")
    else:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    sys.stderr.write("semantic review: %d added, %d merged, %d rejected\n"
                     % (added, merged, len(problems)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
