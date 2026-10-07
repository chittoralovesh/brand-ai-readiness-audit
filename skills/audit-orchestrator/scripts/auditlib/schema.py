"""Validation and self-repair for the final audit report.

The entrypoint must return a schema-valid report even when something upstream
misbehaved. So the last step of an audit is: validate, repair what can be
repaired deterministically, validate again, and publish the outcome inside the
report itself rather than silently emitting something malformed.

The required shape is the one the brief fixes as a floor:

    site, audited_at, summary{total_findings, counts by severity},
    findings[]{id, title, severity, evidence, suggested_action{summary, priority}}

Everything else the report carries is an extension and is validated only for
type sanity.
"""

from __future__ import annotations

import json
import re

SEVERITIES = ("critical", "high", "medium", "low")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:\d{2})$")
_ID_RE = re.compile(r"^F-\d{3,}$")

REQUIRED_TOP_LEVEL = ("site", "audited_at", "summary", "findings")
REQUIRED_FINDING = ("id", "title", "severity", "evidence", "suggested_action")


def validate(report: dict) -> list:
    """Return a list of human-readable schema errors ([] means valid)."""
    errors = []
    if not isinstance(report, dict):
        return ["report is not a JSON object"]

    for field in REQUIRED_TOP_LEVEL:
        if field not in report:
            errors.append("missing required top-level field: %s" % field)

    site = report.get("site")
    if not isinstance(site, str) or not site.strip():
        errors.append("`site` must be a non-empty string")

    audited_at = report.get("audited_at")
    if not isinstance(audited_at, str) or not _ISO_RE.match(audited_at or ""):
        errors.append("`audited_at` must be an ISO-8601 timestamp, got %r" % (audited_at,))

    summary = report.get("summary")
    findings = report.get("findings")
    if not isinstance(findings, list):
        errors.append("`findings` must be a list")
        findings = []
    if not isinstance(summary, dict):
        errors.append("`summary` must be an object")
        summary = {}

    counted = {level: 0 for level in SEVERITIES}
    seen_ids = set()
    for index, finding in enumerate(findings):
        where = "findings[%d]" % index
        if not isinstance(finding, dict):
            errors.append("%s is not an object" % where)
            continue
        for field in REQUIRED_FINDING:
            if field not in finding:
                errors.append("%s missing required field: %s" % (where, field))
        fid = finding.get("id")
        if not isinstance(fid, str) or not _ID_RE.match(fid or ""):
            errors.append("%s.id must match F-NNN, got %r" % (where, fid))
        elif fid in seen_ids:
            errors.append("%s.id %r is duplicated" % (where, fid))
        else:
            seen_ids.add(fid)
        for field in ("title", "evidence"):
            value = finding.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append("%s.%s must be a non-empty string" % (where, field))
        level = finding.get("severity")
        if level not in SEVERITIES:
            errors.append("%s.severity must be one of %s, got %r"
                          % (where, "/".join(SEVERITIES), level))
        else:
            counted[level] += 1
        action = finding.get("suggested_action")
        if not isinstance(action, dict):
            errors.append("%s.suggested_action must be an object" % where)
        else:
            if not isinstance(action.get("summary"), str) or not action["summary"].strip():
                errors.append("%s.suggested_action.summary must be a non-empty string" % where)
            if action.get("priority") not in SEVERITIES:
                errors.append("%s.suggested_action.priority must be one of %s, got %r"
                              % (where, "/".join(SEVERITIES), action.get("priority")))

    if summary.get("total_findings") != len(findings):
        errors.append("summary.total_findings (%r) does not match len(findings) (%d)"
                      % (summary.get("total_findings"), len(findings)))
    for level in SEVERITIES:
        if level not in summary:
            errors.append("summary is missing the %s count" % level)
        elif summary.get(level) != counted[level]:
            errors.append("summary.%s (%r) does not match the findings (%d)"
                          % (level, summary.get(level), counted[level]))

    try:
        json.dumps(report)
    except (TypeError, ValueError) as exc:
        errors.append("report is not JSON-serialisable: %s" % exc)
    return errors


def repair(report: dict) -> tuple:
    """Deterministically fix what can be fixed. Returns (report, repairs)."""
    repairs = []
    if not isinstance(report, dict):
        return report, repairs

    findings = report.get("findings")
    if not isinstance(findings, list):
        report["findings"] = findings = []
        repairs.append("replaced a non-list `findings` with an empty list")

    cleaned = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            repairs.append("dropped findings[%d]: not an object" % index)
            continue
        if finding.get("severity") not in SEVERITIES:
            repairs.append("findings[%d]: unknown severity %r downgraded to low"
                           % (index, finding.get("severity")))
            finding["severity"] = "low"
        action = finding.get("suggested_action")
        if not isinstance(action, dict):
            action = {}
        if not (action.get("summary") or "").strip():
            action["summary"] = ("Review this finding's evidence and address the mechanism "
                                 "it describes.")
            repairs.append("findings[%d]: supplied a placeholder suggested_action.summary"
                           % index)
        if action.get("priority") not in SEVERITIES:
            action["priority"] = finding["severity"]
            repairs.append("findings[%d]: set suggested_action.priority from severity" % index)
        finding["suggested_action"] = action
        if not (finding.get("title") or "").strip():
            finding["title"] = "Unnamed finding (%s)" % finding.get("check_id", "unknown")
            repairs.append("findings[%d]: supplied a placeholder title" % index)
        if not (finding.get("evidence") or "").strip():
            finding["evidence"] = ("No evidence text was recorded for this finding; treat it "
                                   "as unverified.")
            repairs.append("findings[%d]: supplied placeholder evidence" % index)
        cleaned.append(finding)

    for index, finding in enumerate(cleaned, start=1):
        expected = "F-%03d" % index
        if finding.get("id") != expected:
            finding["id"] = expected
            repairs.append("renumbered finding %d to %s" % (index, expected))
    report["findings"] = cleaned

    summary = report.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        repairs.append("rebuilt a missing or invalid `summary`")
    counts = {level: sum(1 for f in cleaned if f["severity"] == level) for level in SEVERITIES}
    if (summary.get("total_findings") != len(cleaned)
            or any(summary.get(level) != counts[level] for level in SEVERITIES)):
        repairs.append("recomputed the severity summary from the findings")
    summary.update(counts)
    summary["total_findings"] = len(cleaned)
    report["summary"] = summary

    if not isinstance(report.get("site"), str) or not (report.get("site") or "").strip():
        report["site"] = report.get("audited_url") or "unknown-site"
        repairs.append("filled `site` from the audited URL")

    audited_at = report.get("audited_at")
    if not isinstance(audited_at, str) or not _ISO_RE.match(audited_at or ""):
        from datetime import datetime, timezone
        report["audited_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        repairs.append("replaced an invalid `audited_at` with the current UTC time")

    return report, repairs


def validate_and_repair(report: dict) -> dict:
    """Validate, repair if needed, re-validate, and record the outcome."""
    errors = validate(report)
    repairs = []
    if errors:
        report, repairs = repair(report)
        errors = validate(report)
    report["report_validation"] = {
        "valid": not errors,
        "errors": errors,
        "repairs_applied": repairs,
        "validated_against": "required fields: %s; per finding: %s"
                             % (", ".join(REQUIRED_TOP_LEVEL), ", ".join(REQUIRED_FINDING)),
    }
    return report
