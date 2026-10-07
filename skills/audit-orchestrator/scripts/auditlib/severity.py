"""The one severity policy for the whole marketplace.

Analyzer skills never assign severity. They report three orthogonal facts about
a failed check - how much damage the defect can do, how much of the site it
touches, and how strong the evidence is - and the orchestrator turns those into
a single severity here. Keeping the policy in one place is what makes severities
comparable across skills, and what stops a specialist skill grading its own
concern as the most important thing on the site.

The full rationale, the bands and the worked examples are in
`references/severity-model.md`. This module is the executable version of that
document; if the two ever disagree, this file is wrong.
"""

from __future__ import annotations

CRITICAL, HIGH, MEDIUM, LOW = "critical", "high", "medium", "low"
SEVERITY_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}
SEVERITY_RANK = {CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1}

CONFIDENCE_ADJUSTMENT = {"high": 0, "medium": -1, "low": -2}
# A weak-evidence finding may never be presented as an emergency, however large
# its theoretical impact. This is the single most important false-positive
# control in the report: it bounds the cost of being wrong.
CONFIDENCE_CAP = {"high": CRITICAL, "medium": HIGH, "low": MEDIUM}

EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2, "": 1}
PILLAR_ORDER = {"ai_discoverability": 0, "on_site_engagement": 1}


def breadth_ratio(breadth: dict):
    """Affected / applicable, or None when the check is not page-scoped."""
    applicable = int((breadth or {}).get("applicable") or 0)
    affected = int((breadth or {}).get("affected") or 0)
    if applicable <= 0:
        return None
    return min(1.0, affected / applicable)


def breadth_adjustment(breadth: dict) -> int:
    """+1 site-wide, 0 partial, -1 isolated, 0 when breadth is not meaningful.

    Single-unit checks (`unit: sites`, or one applicable page) get no adjustment:
    "1 of 1" is not evidence of site-wide breadth, it is the only observation
    that could have been made.
    """
    unit = (breadth or {}).get("unit") or "pages"
    applicable = int((breadth or {}).get("applicable") or 0)
    if unit in ("sites",) or applicable <= 1:
        return 0
    ratio = breadth_ratio(breadth)
    if ratio is None:
        return 0
    if ratio >= 0.80:
        return 1
    if ratio >= 0.30:
        return 0
    return -1


def score(check: dict) -> int:
    """Deterministic 0..5 severity score for a FAILing check."""
    base = int(check.get("impact_base") or 0)
    confidence = (check.get("confidence") or "high").lower()
    raw = base + breadth_adjustment(check.get("breadth") or {}) \
        + CONFIDENCE_ADJUSTMENT.get(confidence, -1)
    return max(0, min(5, raw))


def band(value: int) -> str:
    if value >= 4:
        return CRITICAL
    if value == 3:
        return HIGH
    if value == 2:
        return MEDIUM
    return LOW


def severity_of(check: dict) -> tuple:
    """Return (severity, score, explanation) for a failed check."""
    value = score(check)
    severity = band(value)
    confidence = (check.get("confidence") or "high").lower()
    cap = CONFIDENCE_CAP.get(confidence, HIGH)
    capped = False
    if SEVERITY_RANK[severity] > SEVERITY_RANK[cap]:
        severity, capped = cap, True
    breadth = check.get("breadth") or {}
    ratio = breadth_ratio(breadth)
    parts = [
        "impact %d" % int(check.get("impact_base") or 0),
        "breadth %+d (%s)" % (
            breadth_adjustment(breadth),
            "%d/%d %s" % (breadth.get("affected", 0), breadth.get("applicable", 0),
                          breadth.get("unit", "pages")) if ratio is not None
            else "not page-scoped"),
        "confidence %s (%+d)" % (confidence, CONFIDENCE_ADJUSTMENT.get(confidence, -1)),
        "score %d" % value,
    ]
    if capped:
        parts.append("capped at %s by %s confidence" % (cap, confidence))
    return severity, value, "; ".join(parts)


def remediation_priority(severity: str, effort: str, breadth: dict = None) -> str:
    """Priority attached to the suggested action.

    Priority tracks severity, with one narrow, deliberate exception: a
    *site-wide* medium-severity defect with a *low-effort* fix is promoted to
    high, because "cheap and everywhere" genuinely belongs at the top of a
    remediation queue.

    The exception is kept narrow on purpose. An earlier revision promoted every
    low-effort medium finding and ended up marking most of the report `high`,
    which destroys the signal the field exists to carry.
    """
    if severity == MEDIUM and (effort or "").lower() == "low":
        ratio = breadth_ratio(breadth or {})
        if ratio is not None and ratio >= 0.8 and int((breadth or {}).get("applicable") or 0) > 1:
            return HIGH
    return severity


def sort_key(finding: dict) -> tuple:
    """Deterministic report order. Applied *before* IDs are assigned, so two
    runs over an unchanged site produce identical F-NNN identifiers."""
    return (
        -int(finding.get("severity_score", 0)),
        SEVERITY_ORDER.get(finding.get("severity", LOW), 3),
        EFFORT_ORDER.get((finding.get("suggested_action", {}) or {}).get("effort", ""), 1),
        PILLAR_ORDER.get(finding.get("category", ""), 2),
        finding.get("check_id", ""),
        (finding.get("affected_urls") or [""])[0],
        finding.get("title", ""),
    )
