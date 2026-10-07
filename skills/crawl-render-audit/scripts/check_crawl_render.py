#!/usr/bin/env python3
"""Machine access and machine readability checks over an audit snapshot.

Reads a site snapshot produced by `audit-orchestrator` and writes a check-result
document to stdout (or `--out`). Standard library only; no network access; no
site is contacted from here.

    python check_crawl_render.py --snapshot snapshot.json --out crawl.json

Every check answers the first two of the three questions that decide whether a
page can be cited by an assistant at all:

  1. was the crawler let in?      (robots, status codes, redirects, bot walls)
  2. could it read the page?      (server-rendered text, titles, headings, lang)

The third question - can a specific fact be extracted - belongs to
`ai-citability-audit`.

Checks never assign severity. They report `impact_base`, `breadth` and
`confidence`; the orchestrator owns the severity model so that one policy
applies across the whole marketplace.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

PRODUCER = "crawl-render-audit"
SCHEMA_VERSION = "1.0"

PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_EVALUATED = "NOT_EVALUATED"
INCONCLUSIVE = "INCONCLUSIVE"

AI_DISCOVERABILITY = "ai_discoverability"
ENGAGEMENT = "on_site_engagement"


# ---------------------------------------------------------------------------
# Result container (shared contract, intentionally duplicated in each analyzer
# skill so every skill folder runs standalone).
# ---------------------------------------------------------------------------

class Results:
    def __init__(self, producer: str):
        self.producer = producer
        self.checks: list = []
        self.notes: list = []
        self.errors: list = []

    def add(self, check_id, title, *, pillar, category, status,
            evidence="", mechanism="", impact_base=0, confidence="high",
            affected=0, applicable=0, unit="pages", evidence_items=None,
            affected_urls=None, remediation=None, dedupe_key="", tags=None,
            applicability="", supersedes=None):
        self.checks.append({
            "check_id": check_id,
            "title": title,
            "pillar": pillar,
            "category": category,
            "status": status,
            "impact_base": int(impact_base),
            "confidence": confidence,
            "breadth": {"affected": int(affected), "applicable": int(applicable),
                        "unit": unit},
            "evidence": evidence,
            "evidence_items": (evidence_items or [])[:12],
            "mechanism": mechanism,
            "affected_urls": sorted(set(affected_urls or []))[:25],
            "remediation": remediation or {},
            "dedupe_key": dedupe_key or check_id,
            "tags": sorted(set(tags or [])),
            "applicability": applicability,
            "supersedes": sorted(set(supersedes or [])),
        })

    def note(self, message):
        if message not in self.notes:
            self.notes.append(message)

    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "producer": self.producer,
            "produced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "checks": sorted(self.checks, key=lambda c: c["check_id"]),
            "notes": self.notes,
            "errors": self.errors,
        }


def run_checks(results: Results, snapshot: dict, ctx: dict, checks: list) -> None:
    """Run each check in isolation: one crashing check never stops the rest."""
    for func in checks:
        try:
            func(snapshot, ctx, results)
        except Exception as exc:                       # pragma: no cover - safety net
            results.errors.append({
                "check": getattr(func, "__name__", "?"),
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            })


def load_snapshot(path: str) -> dict:
    if path in ("-", "", None):
        return json.loads(sys.stdin.read())
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "attention required! | cloudflare", "enable javascript and cookies to continue",
    "access denied", "request blocked", "are you a robot", "captcha",
    "px-captcha", "incapsula incident", "bot detection", "unusual traffic",
    "verify you are human", "ddos protection by",
)
SOFT_404_MARKERS = ("page not found", "404 not found", "not found", "no longer exists",
                    "page doesn't exist", "page does not exist", "we can't find",
                    "cannot be found", "error 404")


def as_dict(value):
    """Coerce a snapshot field to a dict. A malformed snapshot must degrade a
    field, never stop the skill producing output."""
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def html_pages_of(snapshot):
    """Successfully-parsed HTML pages, tolerant of a malformed snapshot."""
    pages = [p for p in as_list(snapshot.get("pages")) if isinstance(p, dict)
             and isinstance(p.get("url"), str)]
    html = [p for p in pages if p.get("ok") and isinstance(p.get("extraction"), dict)
            and not p["extraction"].get("non_html")]
    return pages, html


# Failure kinds where no HTTP response ever arrived from the target origin.
TRANSPORT_ERROR_KINDS = frozenset(
    {"dns", "connection_refused", "network_unreachable", "connection", "tls",
     "timeout", "protocol", "budget"})


def transport_of(snapshot):
    """The crawl's transport diagnosis, with a safe default for old snapshots."""
    transport = as_dict(snapshot.get("transport"))
    if transport:
        return transport
    pages = as_list(snapshot.get("pages"))
    responded = any(isinstance(p, dict) and p.get("status") is not None for p in pages)
    return {"target_responded": responded, "responses_received": int(responded),
            "transport_failures": {}, "failed_requests": 0, "dominant_failure": "",
            "state": "ok" if responded else "not_attempted",
            "detail": "No transport diagnosis was recorded in this snapshot."}


def transport_blocked(ctx, r, check_id, title, category, *, pillar=AI_DISCOVERABILITY):
    """Emit NOT_EVALUATED and return True when the origin never answered.

    This is the guard that stops a broken network on the auditing machine being
    reported as a defect in somebody's website: a finding about a site requires
    evidence *from* that site.
    """
    transport = ctx["transport"]
    if transport.get("target_responded"):
        return False
    r.add(check_id, title, pillar=pillar, category=category, status=NOT_EVALUATED,
          applicability="No HTTP response was received from the target origin, so there "
                        "is no evidence about the site to assess. %s"
                        % transport.get("detail", ""))
    return True


def build_context(snapshot):
    pages, html_pages = html_pages_of(snapshot)
    return {
        "pages": pages,
        "html_pages": html_pages,
        "by_url": {p["url"]: p for p in pages},
        "entry": next((p for p in pages if p.get("depth") == 0), None),
        "origin": snapshot.get("origin") or "",
        "site": snapshot.get("site") or "",
        "coverage": as_dict(snapshot.get("coverage")),
        "transport": transport_of(snapshot),
    }


def ex(page: dict) -> dict:
    return page.get("extraction") or {}


def short(url: str, origin: str) -> str:
    if origin and url.startswith(origin):
        return url[len(origin):] or "/"
    return url


def robots_directives(page: dict) -> list:
    """All indexing directives that apply to a page, from meta and headers."""
    out = []
    extraction = ex(page)
    for name, content in (extraction.get("meta_name") or {}).items():
        if name in ("robots", "googlebot", "bingbot", "google", "slurp",
                    "duckduckbot", "yandex", "ai", "chatgpt-user"):
            out.append(("meta name=%s" % name, (content or "").lower()))
    header = (page.get("headers") or {}).get("x-robots-tag")
    if header:
        out.append(("X-Robots-Tag header", header.lower()))
    return out


def has_directive(page: dict, token: str) -> list:
    hits = []
    for source, content in robots_directives(page):
        parts = [p.strip() for p in re.split(r"[,\s]+", content) if p.strip()]
        if token in parts:
            hits.append((source, content))
    return hits


def text_of(page: dict) -> str:
    return (ex(page).get("main_text_excerpt") or ex(page).get("text_excerpt") or "")


# ---------------------------------------------------------------------------
# 1. Was the crawler let in?
# ---------------------------------------------------------------------------

def check_entry_reachable(snap, ctx, r):
    entry = ctx["entry"]
    target = snap.get("target_url", "")
    transport = ctx["transport"]

    # A request that never reached the origin says nothing about the site. It is
    # reported as a coverage limitation, never as a critical crawlability defect:
    # the auditing host's DNS or network is not the website's problem.
    entry_responded = entry is not None and entry.get("status") is not None
    if not entry_responded and not transport.get("target_responded"):
        r.add("CR-01", "Audit entry URL reachability", pillar=AI_DISCOVERABILITY,
              category="crawl_access", status=INCONCLUSIVE,
              applicability="No HTTP response was received from %s. %s No conclusion "
                            "is drawn about the site's crawlability: re-run the audit "
                            "from a host with working connectivity to this origin."
                            % (target or "the target URL", transport.get("detail", "")))
        return
    if not entry_responded:
        failures = ", ".join("%s x%d" % (k, v) for k, v in
                             (transport.get("transport_failures") or {}).items())
        r.add("CR-01", "Audit entry URL reachability", pillar=AI_DISCOVERABILITY,
              category="crawl_access", status=INCONCLUSIVE,
              applicability="Other URLs on this origin answered (%d response(s)), but "
                            "the entry URL itself returned no HTTP response (%s). That "
                            "is consistent with a transient network fault as well as "
                            "with a site defect, so no finding is raised."
                            % (transport.get("responses_received", 0),
                               failures or "no response"))
        return

    # Reaching here means the entry page carries a real HTTP status, so the two
    # transport guards above have already handled every "no response" case.
    status = entry.get("status")
    if entry.get("ok"):
        r.add("CR-01", "Audit entry URL is reachable", pillar=AI_DISCOVERABILITY,
              category="crawl_access", status=PASS,
              evidence="%s returned %s." % (short(entry["url"], ctx["origin"]) or "/", status),
              applicable=1, unit="sites")
        return
    blocked = status in (401, 403, 407, 429, 451)
    r.add("CR-01", "Audit entry URL did not return a successful response",
          pillar=AI_DISCOVERABILITY, category="crawl_access", status=FAIL,
          impact_base=4, confidence="high", affected=1, applicable=1, unit="sites",
          evidence="GET %s returned HTTP %s%s."
                   % (entry.get("requested_url") or target, status,
                      " (%s)" % entry["error"] if entry.get("error") else ""),
          mechanism="Assistants and crawlers fetch the URL a user or link points at. "
                    "A non-2xx response there means the page is never parsed, so the "
                    "brand cannot be indexed or cited from it."
                    + (" A 401/403/429 answer specifically tells automated clients they are "
                       "not welcome, which removes the site from AI answer surfaces while "
                       "leaving it perfectly visible to humans." if blocked else ""),
          affected_urls=[entry.get("requested_url") or target],
          evidence_items=[{"url": entry.get("requested_url"), "observed": "HTTP %s" % status,
                           "expected": "HTTP 200"}],
          remediation={
              "summary": "Return 200 for the canonical entry URL to unauthenticated clients.",
              "steps": ["Reproduce with `curl -I` from an external network.",
                        "If a WAF/bot manager is responsible, allowlist declared crawler user agents rather than blocking by default.",
                        "If the URL moved, serve a single 301 to the live URL instead of an error."],
              "expected_benefit": "Makes the page eligible for indexing and citation.",
              "effort": "medium"},
          dedupe_key="entry-unreachable")


def check_bot_wall(snap, ctx, r):
    if transport_blocked(ctx, r, "CR-02", "Automated-client blocking", "crawl_access"):
        return
    pages = [p for p in ctx["pages"] if p.get("status") is not None]
    if not pages:
        r.add("CR-02", "Automated-client blocking", pillar=AI_DISCOVERABILITY,
              category="crawl_access", status=NOT_EVALUATED,
              applicability="No page returned an HTTP status, so blocking behaviour "
                            "cannot be assessed.")
        return
    hard_blocked = [p for p in pages if p.get("status") in (401, 403, 429, 451)]
    challenged = []
    for p in pages:
        body = (text_of(p) or "").lower()[:1500]
        title = (ex(p).get("title") or "").lower()
        if any(m in body or m in title for m in CHALLENGE_MARKERS):
            challenged.append(p)
    affected = {p["url"] for p in hard_blocked} | {p["url"] for p in challenged}
    if not affected:
        r.add("CR-02", "Site serves automated clients without an interstitial",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=PASS,
              evidence="%d of %d fetched URLs returned content without a bot challenge."
                       % (len(pages), len(pages)), applicable=len(pages))
        return
    ratio = len(affected) / max(1, len(pages))
    items = [{"url": p["url"], "observed": "HTTP %s" % p.get("status"),
              "expected": "HTTP 200 with readable HTML"} for p in hard_blocked[:5]]
    items += [{"url": p["url"], "observed": "interstitial page: %r" % (ex(p).get("title") or "")[:80],
               "expected": "the real page content"} for p in challenged[:5]]
    r.add("CR-02", "Site blocks or challenges automated clients",
          pillar=AI_DISCOVERABILITY, category="crawl_access", status=FAIL,
          impact_base=4 if ratio >= 0.5 else 3, confidence="high",
          affected=len(affected), applicable=len(pages),
          evidence="%d of %d fetched URLs answered with an access-denied status or a bot-"
                   "challenge interstitial instead of page content."
                   % (len(affected), len(pages)),
          mechanism="Answer engines fetch pages with declared, non-browser clients that do "
                    "not solve JavaScript challenges or CAPTCHAs. When the edge returns a "
                    "challenge, the assistant stores the challenge page - not your content - "
                    "so the brand is either omitted or described from third-party sources you "
                    "do not control.",
          evidence_items=items, affected_urls=sorted(affected),
          remediation={
              "summary": "Allow reputable crawler and assistant user agents through the edge "
                         "instead of challenging every non-browser client.",
              "steps": ["In the CDN/WAF bot ruleset, add an allow rule for verified search and "
                        "AI crawler user agents (verify by reverse DNS or published IP ranges rather than the UA string alone).",
                        "Keep rate limiting, but return 200 with content rather than 403 for allowed agents.",
                        "Re-test by requesting a key page with a non-browser client and confirming HTML in the body."],
              "expected_benefit": "Restores the ability of assistants to read and cite the site directly.",
              "effort": "medium"},
          dedupe_key="bot-wall", tags=["blocks-everything"])


def check_declared_vs_enforced_access(snap, ctx, r):
    """Does the network layer actually honour the policy robots.txt publishes?

    robots.txt is a *published policy*. A CDN or WAF enforces a completely
    separate rule set at the network layer, usually by pattern-matching the
    User-Agent string. When the two disagree - the file says crawlers are
    welcome, the edge refuses them - the site looks open on paper and is closed
    in practice. Nothing in robots.txt is wrong, so a policy-only audit never
    sees it.

    This is measured with the auditor's own honest user agent. It never claims
    to be GPTBot or any other named crawler: the finding is that a request which
    *openly identifies itself as an automated client* was refused while the
    published policy invited it. That is the same contradiction, established
    without impersonating anyone.
    """
    robots = as_dict(snap.get("robots"))
    matrix = as_dict(snap.get("robots_agent_matrix"))
    transport = ctx["transport"]
    entry = ctx["entry"]

    if robots.get("outcome") != "ok" or not matrix:
        r.add("CR-31", "Declared crawler policy versus enforced access",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=NOT_APPLICABLE,
              applicability="No parseable robots.txt was retrieved, so there is no declared "
                            "policy to compare the site's actual behaviour against.")
        return

    allowed = sorted(k for k, v in matrix.items() if v.get("allowed_at_root"))
    if len(allowed) < max(2, len(matrix) // 2):
        r.add("CR-31", "Declared crawler policy versus enforced access",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=NOT_APPLICABLE,
              applicability="robots.txt does not broadly invite automated clients (%d of %d "
                            "catalogued crawlers allowed at the root), so refusing one is "
                            "consistent with the published policy rather than a contradiction "
                            "of it. Any blocking is reported by CR-02/CR-04/CR-05 instead."
                            % (len(allowed), len(matrix)))
        return

    status = entry.get("status") if entry else None
    refused = status in (401, 403, 407, 429, 451)

    if refused:
        r.add("CR-31", "The server refuses crawlers that robots.txt says are welcome",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=FAIL,
              impact_base=4, confidence="high", affected=1, applicable=1, unit="sites",
              evidence="robots.txt allows %d of %d catalogued crawlers at the site root "
                       "(including %s), but a request that openly identified itself as an "
                       "automated auditor received HTTP %s."
                       % (len(allowed), len(matrix), ", ".join(allowed[:4]), status),
              mechanism="robots.txt is a published policy; the CDN or WAF in front of the "
                        "site enforces a separate rule at the network layer, typically by "
                        "matching the User-Agent string. Here the two disagree: the file "
                        "invites crawlers and the edge turns them away before a page is ever "
                        "served. Because the policy file itself is faultless, this gap is "
                        "invisible to any audit that only reads robots.txt - yet it stops "
                        "citations just as completely as a site-wide Disallow would.",
              evidence_items=[{"url": (entry or {}).get("requested_url") or snap.get("target_url"),
                               "observed": "HTTP %s for a self-identifying automated client"
                                           % status,
                               "expected": "a normal response, consistent with robots.txt"}],
              affected_urls=[(entry or {}).get("requested_url") or snap.get("target_url", "")],
              remediation={
                  "summary": "Align the CDN/WAF bot rules with the policy robots.txt already "
                             "publishes - or change robots.txt to match what the edge does.",
                  "steps": ["Open the bot-management rules in your CDN/WAF (Cloudflare, Akamai, "
                            "Fastly and similar ship an 'AI bots' or 'known bots' category that "
                            "is often on by default) and find the rule catching this request.",
                            "Decide which layer states your real intent, then make the other "
                            "agree: allow the agents through the edge, or disallow them in "
                            "robots.txt so the published policy is honest.",
                            "Verify by requesting a key page with a non-browser client and "
                            "confirming HTML in the body rather than a challenge or a 403.",
                            "Keep rate limiting - it is the blanket identity-based block, not "
                            "throttling, that removes the site from answers."],
                  "expected_benefit": "Closes a silent gap between stated and enforced policy, "
                                      "restoring the crawler access the site already intended "
                                      "to grant.",
                  "effort": "medium"},
              dedupe_key="robots-enforcement-mismatch", tags=["blocks-everything"])
        return

    if not transport.get("target_responded"):
        r.add("CR-31", "Declared crawler policy versus enforced access",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=INCONCLUSIVE,
              applicability="robots.txt invites automated clients, but no HTTP response was "
                            "received at all, so it cannot be established whether the edge "
                            "silently drops crawler requests (which some bot-management "
                            "products do instead of returning 403) or whether this machine "
                            "simply could not reach the host. %s Re-run from a different "
                            "network to separate the two."
                            % transport.get("detail", ""))
        return

    r.add("CR-31", "Enforced access matches the declared crawler policy",
          pillar=AI_DISCOVERABILITY, category="crawl_access", status=PASS,
          evidence="robots.txt allows %d of %d catalogued crawlers at the site root, and a "
                   "self-identifying automated request received HTTP %s - the published "
                   "policy and the network layer agree."
                   % (len(allowed), len(matrix), status),
          applicable=1, unit="sites")


def check_robots_readable(snap, ctx, r):
    robots = as_dict(snap.get("robots"))
    outcome = robots.get("outcome")
    if outcome == "ok":
        r.add("CR-03", "robots.txt is retrievable and parseable",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=PASS,
              evidence="%s returned HTTP %s with %d user-agent group(s)."
                       % (robots.get("url"), robots.get("status"),
                          robots.get("group_count", 0)), applicable=1, unit="sites")
        return
    if outcome == "missing":
        r.add("CR-03", "No robots.txt published", pillar=AI_DISCOVERABILITY,
              category="crawl_access", status=PASS,
              evidence="%s returned HTTP %s; an absent robots.txt means 'crawl everything', "
                       "which is a valid configuration."
                       % (robots.get("url"), robots.get("status")),
              applicable=1, unit="sites",
              applicability="A missing robots.txt is not a defect. It only removes the "
                            "opportunity to declare sitemaps.")
        return
    # `unreachable` covers two very different situations: the server answered
    # with something unusable, or nothing answered at all. Only the first is a
    # site defect.
    if outcome == "unreachable" and robots.get("status") is None:
        kind = robots.get("error_kind") or "connection"
        r.add("CR-03", "robots.txt retrievability", pillar=AI_DISCOVERABILITY,
              category="crawl_access", status=NOT_EVALUATED,
              applicability="No HTTP response was received for %s (%s), so it is "
                            "unknown whether the site publishes a robots.txt. This "
                            "reflects the connection from the auditing host, not the "
                            "site." % (robots.get("url"), kind))
        return

    server_error = outcome == "server_error"
    r.add("CR-03", "robots.txt is unreadable", pillar=AI_DISCOVERABILITY,
          category="crawl_access", status=FAIL,
          impact_base=4 if server_error else 2,
          confidence="high" if server_error else "medium",
          affected=1, applicable=1, unit="sites",
          evidence="%s returned %s (%s)." % (robots.get("url"),
                                             robots.get("status") or "no response",
                                             outcome),
          mechanism="Major crawlers treat a persistently failing robots.txt (5xx or "
                    "unreachable) as 'disallow everything' rather than 'allow everything'. "
                    "A broken robots endpoint can therefore silently de-index an entire site "
                    "that is otherwise perfectly healthy."
                    if server_error else
                    "robots.txt must be served as plain text. A robots endpoint that returns "
                    "HTML is parsed as garbage, so declared sitemap locations and crawl rules "
                    "are ignored.",
          affected_urls=[robots.get("url", "")],
          remediation={
              "summary": "Serve /robots.txt as text/plain with a 200 or a 404 - never a 5xx.",
              "steps": ["Confirm the path is handled by the web server, not the application router.",
                        "Return HTTP 404 if you have no rules; that is safer than a 5xx.",
                        "Set `Content-Type: text/plain` explicitly."],
              "expected_benefit": "Removes the risk of a site-wide crawl block caused by an "
                                  "unrelated server error.",
              "effort": "low"},
          dedupe_key="robots-unreadable")


def check_robots_blocks_crawlers(snap, ctx, r):
    matrix = as_dict(snap.get("robots_agent_matrix"))
    if not matrix or (snap.get("robots", {}) or {}).get("outcome") not in ("ok",):
        r.add("CR-04", "Crawler-specific robots.txt directives",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=NOT_APPLICABLE,
              applicability="No parseable robots.txt, so there are no directives to assess.")
        return
    ai_blocked = sorted(k for k, v in matrix.items()
                        if v["kind"] == "ai" and not v["allowed_at_root"])
    search_blocked = sorted(k for k, v in matrix.items()
                            if v["kind"] == "search" and not v["allowed_at_root"])
    wildcard_all = any(g.get("blocks_everything") and "*" in g.get("agents", [])
                       for g in (snap.get("robots", {}) or {}).get("groups", []))

    if search_blocked:
        r.add("CR-05", "robots.txt blocks mainstream search crawlers",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=FAIL,
              impact_base=4, confidence="high",
              affected=1, applicable=1, unit="sites",
              evidence="robots.txt disallows the site root for: %s." % ", ".join(search_blocked),
              mechanism="Assistants that cannot fetch a page directly fall back on a search "
                        "index to find sources. Excluding the search crawlers removes the "
                        "brand from that fallback as well, so there is no path left by which "
                        "an answer can cite the site.",
              evidence_items=[{"url": (snap.get("robots") or {}).get("url"),
                               "observed": "Disallow: / for %s" % a,
                               "expected": "no root-level Disallow"} for a in search_blocked[:6]],
              remediation={
                  "summary": "Remove the root-level Disallow for search crawlers, or scope it "
                             "to the paths that genuinely must stay private.",
                  "steps": ["Replace `Disallow: /` with specific paths (for example `/cart`, `/account`).",
                            "Keep private areas behind authentication rather than relying on robots.txt.",
                            "Re-test each rule against a representative URL before deploying."],
                  "expected_benefit": "Restores eligibility for organic search and for the "
                                      "retrieval fallback assistants rely on.",
                  "effort": "low"},
              dedupe_key="robots-blocks-search")
    else:
        r.add("CR-05", "Mainstream search crawlers are allowed",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=PASS,
              evidence="All %d catalogued search crawlers may fetch the site root."
                       % len([v for v in matrix.values() if v["kind"] == "search"]),
              applicable=len([v for v in matrix.values() if v["kind"] == "search"]),
              unit="crawlers")

    ai_total = len([v for v in matrix.values() if v["kind"] == "ai"])
    if not ai_blocked:
        r.add("CR-04", "AI and answer-engine crawlers are allowed",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=PASS,
              evidence="All %d catalogued AI crawlers may fetch the site root." % ai_total,
              applicable=ai_total, unit="crawlers")
        return
    named = [k for k in ai_blocked if matrix[k]["has_explicit_group"]]
    purposes = ["%s (%s)" % (k, matrix[k]["purpose"]) for k in ai_blocked[:6]]
    # Breadth here is a site-level policy fact, not a proportion: "2 of 22
    # catalogued agents" would understate a deliberate block of the two agents
    # that matter most. Extent is carried by impact instead.
    live_fetchers = [a for a in ai_blocked
                     if "user" in a or "fetcher" in a or "web" in a]
    if wildcard_all or len(ai_blocked) >= 6:
        impact = 4
    elif live_fetchers or len(ai_blocked) >= 2:
        impact = 3
    else:
        impact = 2
    r.add("CR-04", "robots.txt blocks AI assistant and answer-engine crawlers",
          pillar=AI_DISCOVERABILITY, category="crawl_access", status=FAIL,
          impact_base=impact, confidence="high",
          affected=1, applicable=1, unit="sites",
          evidence="%d of %d catalogued AI crawlers are disallowed at the site root%s: %s."
                   % (len(ai_blocked), ai_total,
                      " by a wildcard `User-agent: *` group" if wildcard_all
                      else " by explicit user-agent groups", ", ".join(ai_blocked)),
          mechanism="Assistants only quote pages their fetcher is permitted to retrieve. "
                    "A root-level Disallow for these agents means the brand's own wording "
                    "never reaches an answer; the assistant either omits the brand or "
                    "describes it from third-party pages, which is exactly how "
                    "misrepresentation starts."
                    + (" Note that some of these agents are *live fetchers* used when a user "
                       "pastes your URL - blocking them breaks even explicitly requested "
                       "look-ups." if any("user" in a or "fetcher" in a for a in ai_blocked) else ""),
          evidence_items=[{"url": (snap.get("robots") or {}).get("url"),
                           "observed": "root disallowed for %s" % p.split(" (")[0],
                           "expected": "allowed, or a scoped Disallow"} for p in purposes],
          remediation={
              "summary": "Decide crawler policy deliberately per agent, and allow the "
                         "assistant fetchers you want citations from.",
              "steps": ["Separate *training* crawlers from *live retrieval* fetchers: blocking the "
                        "former is a business decision, blocking the latter removes you from answers "
                        "users explicitly ask for.",
                        "Allow at minimum the live-fetch agents (for example ChatGPT-User, Perplexity-User, "
                        "Claude-Web style agents) on public marketing and documentation paths.",
                        "Keep any genuine exclusions scoped to specific paths rather than `/`.",
                        "Re-deploy robots.txt and confirm each rule with a path test."],
              "expected_benefit": "Makes the brand's own pages eligible as cited sources "
                                  "instead of leaving assistants to infer facts elsewhere.",
              "effort": "low"},
          dedupe_key="robots-blocks-ai",
          applicability="Raised only where a root-level Disallow was observed for a "
                        "catalogued agent (%d via explicit groups)." % len(named))


def check_robots_blocks_own_pages(snap, ctx, r):
    blocked = as_list(as_dict(snap.get("coverage")).get("robots_blocked_sample"))
    total_seen = len(ctx["pages"]) + len(blocked)
    if not blocked:
        r.add("CR-06", "No linked page was withheld by robots.txt",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=PASS,
              evidence="0 of %d discovered URLs were disallowed by robots.txt." % max(1, total_seen),
              applicable=max(1, total_seen))
        return
    # Only meaningful when the blocked URL is one the site links to itself.
    linked = set()
    for edge in as_list(as_dict(snap.get("link_graph")).get("internal_edges")):
        linked.add(edge[1])
    self_linked = [u for u in blocked if u in linked]
    if not self_linked:
        r.add("CR-06", "robots.txt exclusions cover only unlinked paths",
              pillar=AI_DISCOVERABILITY, category="crawl_access", status=PASS,
              evidence="%d URL(s) were disallowed, none of which the site links to." % len(blocked),
              applicable=max(1, total_seen),
              applicability="Disallowing paths that are not linked is normal housekeeping.")
        return
    r.add("CR-06", "Pages the site links to are disallowed in robots.txt",
          pillar=AI_DISCOVERABILITY, category="crawl_access", status=FAIL,
          impact_base=3, confidence="high",
          affected=len(self_linked), applicable=max(1, total_seen),
          evidence="%d of %d discovered URLs are linked from the site's own pages yet "
                   "disallowed by robots.txt: %s."
                   % (len(self_linked), total_seen,
                      ", ".join(short(u, ctx["origin"]) for u in self_linked[:5])),
          mechanism="A page that is linked in navigation but disallowed cannot be read by any "
                    "crawler. Link equity and anchor context point at a document no machine "
                    "can open, and the facts on it are unavailable for citation.",
          evidence_items=[{"url": u, "observed": "disallowed by robots.txt but linked internally",
                           "expected": "crawlable, or not linked"} for u in self_linked[:6]],
          affected_urls=self_linked,
          remediation={
              "summary": "Reconcile navigation with robots.txt: either allow these paths or "
                         "stop linking to them from public pages.",
              "steps": ["List the disallowed paths that appear in site navigation.",
                        "For each, decide whether it is genuinely private; if it is, move it behind auth and remove the public link.",
                        "If it is public, narrow or delete the Disallow rule."],
              "expected_benefit": "Recovers crawlable, citable pages the site already promotes.",
              "effort": "low"},
          dedupe_key="robots-blocks-linked-pages")


def check_indexing_directives(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-08", "Indexing directives", pillar=AI_DISCOVERABILITY,
              category="indexability", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return
    noindex, nofollow, ai_optout = [], [], []
    for p in pages:
        if has_directive(p, "noindex") or has_directive(p, "none"):
            noindex.append(p)
        if has_directive(p, "nofollow"):
            nofollow.append(p)
        if has_directive(p, "noai") or has_directive(p, "noimageai"):
            ai_optout.append(p)

    if noindex:
        items = []
        for p in noindex[:6]:
            src, content = (has_directive(p, "noindex") or has_directive(p, "none"))[0]
            items.append({"url": p["url"], "observed": "%s: %s" % (src, content),
                          "expected": "index (or no directive)"})
        r.add("CR-08", "Pages instruct search engines not to index them",
              pillar=AI_DISCOVERABILITY, category="indexability", status=FAIL,
              impact_base=4 if len(noindex) == len(pages) else 3, confidence="high",
              affected=len(noindex), applicable=len(pages),
              evidence="%d of %d sampled pages carry a `noindex` directive (%s)."
                       % (len(noindex), len(pages),
                          ", ".join(short(p["url"], ctx["origin"]) for p in noindex[:4])),
              mechanism="`noindex` removes the page from search indexes. Assistants that "
                        "ground answers in a search index therefore never see it, and the "
                        "page cannot be offered as a citation even when it answers the "
                        "question precisely. This is frequently left over from a staging "
                        "configuration and is invisible to anyone browsing the site.",
              evidence_items=items, affected_urls=[p["url"] for p in noindex],
              remediation={
                  "summary": "Remove `noindex` from pages that should be publicly discoverable.",
                  "steps": ["Check both the `<meta name=\"robots\">` tag and the `X-Robots-Tag` response header - the header wins and is easy to miss.",
                            "Confirm the directive is not being injected by a staging/environment flag.",
                            "Keep `noindex` only on genuinely duplicate or utility pages."],
                  "expected_benefit": "Makes the affected pages eligible for indexing and citation.",
                  "effort": "low"},
              dedupe_key="noindex", tags=["indexation-signal"])
    else:
        r.add("CR-08", "No unintended noindex directives", pillar=AI_DISCOVERABILITY,
              category="indexability", status=PASS,
              evidence="0 of %d sampled pages carry `noindex`." % len(pages),
              applicable=len(pages))

    if nofollow and len(nofollow) >= max(1, len(pages) // 2):
        r.add("CR-09", "Page-level `nofollow` suppresses internal link discovery",
              pillar=AI_DISCOVERABILITY, category="indexability", status=FAIL,
              impact_base=2, confidence="high",
              affected=len(nofollow), applicable=len(pages),
              evidence="%d of %d sampled pages set a page-level `nofollow` robots directive."
                       % (len(nofollow), len(pages)),
              mechanism="A page-level `nofollow` tells crawlers to ignore every outgoing link "
                        "on the page, so pages reachable only through it are never discovered.",
              affected_urls=[p["url"] for p in nofollow],
              remediation={
                  "summary": "Drop page-level `nofollow`; mark individual links instead if needed.",
                  "steps": ["Remove `nofollow` from the page robots directive.",
                            "If specific links must not pass equity, use `rel=\"nofollow\"` (or `ugc`/`sponsored`) on those anchors only."],
                  "expected_benefit": "Restores crawl paths to pages currently reachable only via these pages.",
                  "effort": "low"},
              dedupe_key="page-nofollow")
    if ai_optout:
        r.add("CR-10", "Pages carry an AI opt-out meta directive",
              pillar=AI_DISCOVERABILITY, category="indexability", status=FAIL,
              impact_base=2, confidence="medium",
              affected=len(ai_optout), applicable=len(pages),
              evidence="%d of %d sampled pages declare `noai`/`noimageai`."
                       % (len(ai_optout), len(pages)),
              mechanism="`noai` is an emerging, voluntary opt-out honoured by a growing set "
                        "of AI crawlers. Where honoured it removes the page from assistant "
                        "answers. It is legitimate as a deliberate policy; it is a defect when "
                        "it was inherited from a template and contradicts a goal of being cited.",
              affected_urls=[p["url"] for p in ai_optout],
              remediation={
                  "summary": "Confirm the AI opt-out is intentional; remove it from pages you want cited.",
                  "steps": ["Identify which template injects the directive.",
                            "Keep it on proprietary or licensed content only."],
                  "expected_benefit": "Aligns AI visibility with commercial intent.",
                  "effort": "low"},
              dedupe_key="ai-optout",
              applicability="Reported as a policy inconsistency, not as an error, because "
                            "opting out can be deliberate.")


# ---------------------------------------------------------------------------
# 2. Could the crawler read the page?
# ---------------------------------------------------------------------------

def check_render_dependency(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-11", "Client-side render dependency", pillar=AI_DISCOVERABILITY,
              category="renderability", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return
    suspects, details = [], []
    for p in pages:
        e = ex(p)
        words = e.get("main_word_count", 0)
        signals = []
        empty_root = [c for c in (e.get("empty_root_containers") or [])
                      if c.get("inner_text_chars", 0) < 40]
        if empty_root:
            signals.append("empty mount point #%s" % empty_root[0].get("id"))
        if e.get("csr_markers"):
            signals.append("hydration payload (%s)" % ", ".join(e["csr_markers"][:2]))
        if e.get("script_count", 0) >= 3 and words < 120:
            signals.append("%d scripts but %d words of text" % (e["script_count"], words))
        if e.get("noscript_text_len", 0) > 60 and words < 120:
            signals.append("<noscript> fallback carries the message")
        # Two independent signals required. A framework marker alone proves
        # nothing: most frameworks can and do render on the server.
        if words < 150 and len(signals) >= 2:
            suspects.append(p)
            details.append({"url": p["url"],
                            "observed": "%d words in the server HTML; %s"
                                        % (words, "; ".join(signals)),
                            "expected": "the page's primary content present in the server response"})
    if not suspects:
        r.add("CR-11", "Primary content is present in the server response",
              pillar=AI_DISCOVERABILITY, category="renderability", status=PASS,
              evidence="%d of %d sampled pages returned readable text without JavaScript "
                       "execution." % (len(pages), len(pages)), applicable=len(pages))
        return
    ratio = len(suspects) / len(pages)
    r.add("CR-11", "Page content only exists after JavaScript runs",
          pillar=AI_DISCOVERABILITY, category="renderability", status=FAIL,
          impact_base=4 if ratio >= 0.6 else 3, confidence="high",
          affected=len(suspects), applicable=len(pages),
          evidence="%d of %d sampled pages returned an essentially empty document "
                   "(median %d words of body text) with a framework mount point and a "
                   "hydration payload, so the visible content is assembled client-side."
                   % (len(suspects), len(pages),
                      sorted(ex(p).get("main_word_count", 0) for p in suspects)[len(suspects) // 2]),
          mechanism="Most AI fetchers and many crawlers read the raw HTTP response and do not "
                    "execute JavaScript. If the text only appears after hydration, those "
                    "clients store an empty page: the brand looks contentless to machines while "
                    "looking complete to people. This is the single most common reason a "
                    "visually rich site is never quoted.",
          evidence_items=details[:8], affected_urls=[p["url"] for p in suspects],
          remediation={
              "summary": "Serve the primary content in the initial HTML response "
                         "(server-side rendering, static generation, or prerendering for bots).",
              "steps": ["Pick the highest-value templates first: home, offering/product, pricing, and the top editorial pages.",
                        "Enable SSR or build-time static generation for those routes so the H1, body copy, links and structured data are in the raw response.",
                        "Verify with `curl -s <url> | wc -w` (or view-source) - the key facts must be visible with JavaScript disabled.",
                        "Keep client-side rendering for genuinely interactive widgets only."],
              "expected_benefit": "Every downstream signal - text, headings, links, structured "
                                  "data - becomes visible to crawlers at once, which is why this "
                                  "is usually the highest-leverage single fix on a JS-heavy site.",
              "effort": "high"},
          dedupe_key="render-gap", tags=["render-gap-root-cause"])


def check_thin_content(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-12", "Server-rendered text volume", pillar=AI_DISCOVERABILITY,
              category="renderability", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return
    thin = [p for p in pages if ex(p).get("main_word_count", 0) < 120]
    if not thin:
        r.add("CR-12", "Pages carry enough extractable text",
              pillar=AI_DISCOVERABILITY, category="renderability", status=PASS,
              evidence="All %d sampled pages returned at least 120 words of body text."
                       % len(pages), applicable=len(pages))
        return
    r.add("CR-12", "Sampled pages return very little extractable text",
          pillar=AI_DISCOVERABILITY, category="renderability", status=FAIL,
          impact_base=3 if len(thin) / len(pages) >= 0.5 else 2, confidence="medium",
          affected=len(thin), applicable=len(pages),
          evidence="%d of %d sampled pages contain fewer than 120 words of body text "
                   "(%s)." % (len(thin), len(pages),
                              ", ".join("%s: %d words" % (short(p["url"], ctx["origin"]),
                                                          ex(p).get("main_word_count", 0))
                                        for p in thin[:4])),
          mechanism="Retrieval systems rank and quote passages. A page with almost no prose "
                    "offers nothing to quote, so even a perfectly crawlable page is passed "
                    "over in favour of a competitor's page that states the same fact in "
                    "sentences.",
          evidence_items=[{"url": p["url"],
                           "observed": "%d words of body text" % ex(p).get("main_word_count", 0),
                           "expected": "enough prose to answer a question about the page"}
                          for p in thin[:8]],
          affected_urls=[p["url"] for p in thin],
          remediation={
              "summary": "Add a short, factual prose block to the thin templates.",
              "steps": ["For each thin page, write 100-200 words that state what the page is about, who it is for, and one or two concrete facts.",
                        "Put the text in the server response, not behind a tab or accordion.",
                        "Avoid duplicating the same boilerplate across templates - identical text is treated as near-duplicate."],
              "expected_benefit": "Gives retrieval systems a quotable passage per page.",
              "effort": "medium"},
          dedupe_key="thin-content", tags=["render-gap-symptom"])


def check_titles(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-13", "Page titles", pillar=AI_DISCOVERABILITY, category="page_metadata",
              status=NOT_EVALUATED, applicability="No HTML page was successfully parsed.")
        return
    missing = [p for p in pages if not (ex(p).get("title") or "").strip()]
    titles: dict = {}
    for p in pages:
        t = (ex(p).get("title") or "").strip().lower()
        if t:
            titles.setdefault(t, []).append(p["url"])
    duplicated = {t: urls for t, urls in titles.items() if len(urls) > 1}
    dup_pages = sum(len(v) for v in duplicated.values())

    if not missing and not duplicated:
        r.add("CR-13", "Every sampled page has a distinct title",
              pillar=AI_DISCOVERABILITY, category="page_metadata", status=PASS,
              evidence="%d of %d sampled pages have a non-empty, unique `<title>`."
                       % (len(pages), len(pages)), applicable=len(pages))
        return
    if missing:
        r.add("CR-13", "Pages are missing a title element",
              pillar=AI_DISCOVERABILITY, category="page_metadata", status=FAIL,
              impact_base=3, confidence="high", affected=len(missing), applicable=len(pages),
              evidence="%d of %d sampled pages have no usable `<title>` (%s)."
                       % (len(missing), len(pages),
                          ", ".join(short(p["url"], ctx["origin"]) for p in missing[:4])),
              mechanism="The title is the primary label a retrieval system uses to decide what "
                        "a page is about and the string most assistants display when citing it. "
                        "Without one, the page is hard to match to a question and unattractive "
                        "to cite even when matched.",
              evidence_items=[{"url": p["url"], "observed": "no <title> content",
                               "expected": "a specific, page-level title"} for p in missing[:8]],
              affected_urls=[p["url"] for p in missing],
              remediation={
                  "summary": "Give every template a specific, front-loaded title.",
                  "steps": ["Pattern: `<specific subject> - <what it is> | <brand>`, under about 60 characters.",
                            "Put the distinguishing words first; do not lead every title with the brand name.",
                            "Generate titles server-side so they appear in the raw HTML."],
                  "expected_benefit": "Improves both match rate and click-through from citations.",
                  "effort": "low"},
              dedupe_key="title-missing", tags=["render-gap-symptom"])
    if duplicated:
        sample = sorted(duplicated.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:4]
        r.add("CR-14", "Different pages share the same title",
              pillar=AI_DISCOVERABILITY, category="page_metadata", status=FAIL,
              impact_base=2, confidence="high", affected=dup_pages, applicable=len(pages),
              evidence="%d of %d sampled pages share a title with another page; %d distinct "
                       "title string(s) are reused (e.g. %r appears on %d URLs)."
                       % (dup_pages, len(pages), len(duplicated), sample[0][0][:60],
                          len(sample[0][1])),
              mechanism="Identical titles make separate pages look like duplicates. Retrieval "
                        "systems collapse near-duplicates and keep one representative, so the "
                        "other pages stop being candidates for citation entirely.",
              evidence_items=[{"url": urls[0], "observed": "title %r reused on %d pages"
                                                           % (title[:60], len(urls)),
                               "expected": "a distinct title per page"}
                              for title, urls in sample],
              affected_urls=[u for _, urls in sample for u in urls],
              remediation={
                  "summary": "Make titles unique by injecting the page's own subject.",
                  "steps": ["Find the template producing the shared string.",
                            "Interpolate the page-specific entity (product name, article headline, location) into the title.",
                            "Re-check that paginated and filtered variants differ or are canonicalised."],
                  "expected_benefit": "Stops separate pages being de-duplicated away.",
                  "effort": "low"},
              dedupe_key="title-duplicate")


def check_meta_description(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-15", "Meta descriptions", pillar=AI_DISCOVERABILITY,
              category="page_metadata", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return
    missing = [p for p in pages if not (ex(p).get("meta_name", {}) or {}).get("description", "").strip()]
    seen: dict = {}
    for p in pages:
        d = ((ex(p).get("meta_name", {}) or {}).get("description") or "").strip().lower()
        if d:
            seen.setdefault(d, []).append(p["url"])
    duplicated = {d: u for d, u in seen.items() if len(u) > 1}
    if not missing and not duplicated:
        r.add("CR-15", "Meta descriptions are present and distinct",
              pillar=AI_DISCOVERABILITY, category="page_metadata", status=PASS,
              evidence="%d of %d sampled pages have a unique meta description."
                       % (len(pages), len(pages)), applicable=len(pages))
        return
    affected = {p["url"] for p in missing} | {u for urls in duplicated.values() for u in urls}
    duplicated_pages = sum(len(u) for u in duplicated.values())
    # Report only the half that actually applies, so the sentence reads as a
    # finding rather than as a template with a zero in it.
    parts = []
    if missing:
        parts.append("%d of %d sampled pages have no meta description"
                     % (len(missing), len(pages)))
    if duplicated:
        parts.append("%d of %d sampled pages reuse a description that also appears on "
                     "another page (%d distinct string(s) reused)"
                     % (duplicated_pages, len(pages), len(duplicated)))
    title = ("Meta descriptions are missing" if missing and not duplicated
             else "The same meta description is reused across pages"
             if duplicated and not missing
             else "Meta descriptions are missing or duplicated")
    r.add("CR-15", title,
          pillar=AI_DISCOVERABILITY, category="page_metadata", status=FAIL,
          impact_base=1, confidence="high", affected=len(affected), applicable=len(pages),
          evidence="; ".join(parts) + ".",
          mechanism="The description is a pre-written summary. Search and assistant surfaces "
                    "fall back to an arbitrary text fragment without one, which often lands on "
                    "navigation or legal boilerplate and misrepresents the page.",
          evidence_items=([{"url": p["url"], "observed": "no meta description",
                            "expected": "a 120-160 character page summary"} for p in missing[:5]]
                          + [{"url": urls[0], "observed": "description reused on %d pages" % len(urls),
                              "expected": "a distinct description"} for urls in list(duplicated.values())[:3]]),
          affected_urls=sorted(affected),
          remediation={
              "summary": "Write a distinct 120-160 character description per template.",
              "steps": ["State what the page offers and for whom, in one sentence, using the words a user would type.",
                        "Generate from page data (product name, article standfirst) rather than a fixed string."],
              "expected_benefit": "Controls the summary shown alongside a citation.",
              "effort": "low"},
          dedupe_key="meta-description")


def check_heading_structure(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-16", "Heading structure", pillar=AI_DISCOVERABILITY,
              category="document_structure", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return
    no_h1, many_h1, skipped = [], [], []
    for p in pages:
        heads = ex(p).get("headings") or []
        h1s = [h for h in heads if h["level"] == 1 and h["text"].strip()]
        if not h1s:
            no_h1.append(p)
        elif len(h1s) > 2:
            many_h1.append(p)
        levels = [h["level"] for h in heads]
        for prev, nxt in zip(levels, levels[1:]):
            if nxt - prev >= 2:
                skipped.append(p)
                break
    affected = {p["url"] for p in no_h1} | {p["url"] for p in many_h1}
    if not affected:
        r.add("CR-16", "Pages have a single, meaningful top-level heading",
              pillar=AI_DISCOVERABILITY, category="document_structure", status=PASS,
              evidence="%d of %d sampled pages expose exactly one usable H1."
                       % (len(pages) - len(many_h1), len(pages)), applicable=len(pages))
        return
    r.add("CR-16", "Heading hierarchy does not identify the page subject",
          pillar=AI_DISCOVERABILITY, category="document_structure", status=FAIL,
          impact_base=2, confidence="high", affected=len(affected), applicable=len(pages),
          evidence="%d of %d sampled pages have no H1; %d have more than two H1 elements; "
                   "%d skip a heading level."
                   % (len(no_h1), len(pages), len(many_h1), len(skipped)),
          mechanism="Passage-level retrieval segments a document by its headings and uses the "
                    "nearest heading as the label for each passage. Without a clear H1 and an "
                    "ordered hierarchy, passages are extracted with the wrong label, so a "
                    "correct answer on the page is attributed to the wrong topic - or missed.",
          evidence_items=([{"url": p["url"], "observed": "no H1 element",
                            "expected": "one H1 naming the page subject"} for p in no_h1[:5]]
                          + [{"url": p["url"], "observed": "%d H1 elements"
                                                           % len([h for h in ex(p).get("headings", []) if h["level"] == 1]),
                              "expected": "one H1"} for p in many_h1[:3]]),
          affected_urls=sorted(affected),
          remediation={
              "summary": "One descriptive H1 per page, then H2/H3 in order.",
              "steps": ["Make the H1 name the specific subject, not the brand or a slogan.",
                        "Demote decorative headings to styled text, and promote real section labels to H2.",
                        "Do not skip levels (H2 then H4)."],
              "expected_benefit": "Lets retrieval systems label and quote the right passage.",
              "effort": "low"},
          dedupe_key="heading-structure", tags=["render-gap-symptom"])


def check_language_declaration(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-17", "Language declaration", pillar=AI_DISCOVERABILITY,
              category="document_structure", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return
    missing = [p for p in pages if not (ex(p).get("html_lang") or "").strip()]
    if not missing:
        r.add("CR-17", "Pages declare their language", pillar=AI_DISCOVERABILITY,
              category="document_structure", status=PASS,
              evidence="%d of %d sampled pages set `<html lang>`." % (len(pages), len(pages)),
              applicable=len(pages))
        return
    r.add("CR-17", "Pages do not declare a content language",
          pillar=AI_DISCOVERABILITY, category="document_structure", status=FAIL,
          impact_base=1, confidence="high", affected=len(missing), applicable=len(pages),
          evidence="%d of %d sampled pages omit the `lang` attribute on `<html>`."
                   % (len(missing), len(pages)),
          mechanism="Language detection drives which locale's queries a page is eligible for "
                    "and how screen readers pronounce it. An undeclared language is guessed, "
                    "and short or mixed-language pages are guessed wrong.",
          evidence_items=[{"url": p["url"], "observed": "<html> without lang",
                           "expected": 'e.g. <html lang="en">'} for p in missing[:6]],
          affected_urls=[p["url"] for p in missing],
          remediation={"summary": "Set `<html lang>` in the base template.",
                       "steps": ["Add the BCP-47 code for the page's language (e.g. `en`, `en-GB`, `de`).",
                                 "For multilingual sites, set it per rendered locale, and pair with hreflang alternates."],
                       "expected_benefit": "Correct locale targeting and accessible pronunciation.",
                       "effort": "low"},
          dedupe_key="html-lang")


def check_semantic_landmarks(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-18", "Semantic landmarks", pillar=AI_DISCOVERABILITY,
              category="document_structure", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return
    bare = [p for p in pages
            if not any((ex(p).get("landmark_counts") or {}).get(t) for t in ("main", "article"))
            and ex(p).get("main_word_count", 0) >= 120]
    considered = [p for p in pages if ex(p).get("main_word_count", 0) >= 120]
    if not considered:
        r.add("CR-18", "Semantic landmarks", pillar=AI_DISCOVERABILITY,
              category="document_structure", status=NOT_APPLICABLE,
              applicability="No sampled page carried enough text for main-content "
                            "identification to matter.")
        return
    if not bare:
        r.add("CR-18", "Main content is marked up semantically",
              pillar=AI_DISCOVERABILITY, category="document_structure", status=PASS,
              evidence="%d of %d content pages wrap their body in <main> or <article>."
                       % (len(considered), len(considered)), applicable=len(considered))
        return
    r.add("CR-18", "No semantic boundary separates content from site furniture",
          pillar=AI_DISCOVERABILITY, category="document_structure", status=FAIL,
          impact_base=1, confidence="medium", affected=len(bare), applicable=len(considered),
          evidence="%d of %d content pages use neither `<main>` nor `<article>` to delimit "
                   "the page body." % (len(bare), len(considered)),
          mechanism="Extractors use landmark elements to separate the article from navigation, "
                    "promos and footers. Without them, boilerplate is mixed into the extracted "
                    "passage, which dilutes relevance and produces citations that quote a menu.",
          evidence_items=[{"url": p["url"], "observed": "no <main>/<article> element",
                           "expected": "body content wrapped in <main> or <article>"}
                          for p in bare[:6]],
          affected_urls=[p["url"] for p in bare],
          remediation={"summary": "Wrap the unique page content in `<main>` (and `<article>` for editorial).",
                       "steps": ["Add exactly one `<main>` per page around the content that differs between pages.",
                                 "Move navigation into `<nav>`, and site-wide furniture into `<header>`/`<footer>`."],
                       "expected_benefit": "Cleaner passage extraction and less boilerplate in citations.",
                       "effort": "low"},
          dedupe_key="semantic-landmarks", tags=["render-gap-symptom"])


def check_viewport(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-19", "Mobile viewport", pillar=ENGAGEMENT, category="document_structure",
              status=NOT_EVALUATED, applicability="No HTML page was successfully parsed.")
        return
    missing = [p for p in pages if not (ex(p).get("meta_name", {}) or {}).get("viewport")]
    if not missing:
        r.add("CR-19", "Pages declare a mobile viewport", pillar=ENGAGEMENT,
              category="document_structure", status=PASS,
              evidence="%d of %d sampled pages set a viewport meta tag." % (len(pages), len(pages)),
              applicable=len(pages))
        return
    r.add("CR-19", "Pages do not declare a mobile viewport", pillar=ENGAGEMENT,
          category="document_structure", status=FAIL,
          impact_base=3 if len(missing) == len(pages) else 2, confidence="high",
          affected=len(missing), applicable=len(pages),
          evidence="%d of %d sampled pages have no `<meta name=\"viewport\">`."
                   % (len(missing), len(pages)),
          mechanism="Without a viewport declaration, mobile browsers render the page at a "
                    "desktop width and scale it down, so text arrives unreadably small and "
                    "tap targets are too close together. On mobile traffic this produces an "
                    "immediate bounce regardless of how good the content is.",
          evidence_items=[{"url": p["url"], "observed": "no viewport meta tag",
                           "expected": 'width=device-width, initial-scale=1'} for p in missing[:6]],
          affected_urls=[p["url"] for p in missing],
          remediation={"summary": "Add the viewport meta tag to the base template.",
                       "steps": ['Insert `<meta name="viewport" content="width=device-width, initial-scale=1">` in `<head>`.',
                                 "Then verify the layout does not overflow horizontally at 375px width."],
                       "expected_benefit": "Removes the most common cause of instant mobile abandonment.",
                       "effort": "low"},
          dedupe_key="viewport-missing")


# ---------------------------------------------------------------------------
# 3. URL, canonical and link hygiene
# ---------------------------------------------------------------------------

def check_canonical(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-20", "Canonical URLs", pillar=AI_DISCOVERABILITY, category="canonicalisation",
              status=NOT_EVALUATED, applicability="No HTML page was successfully parsed.")
        return
    missing, multiple, offsite, collapsed = [], [], [], []
    origin_host = urlsplit(ctx["origin"]).hostname or ""
    canon_targets: dict = {}
    for p in pages:
        e = ex(p)
        canonical = (e.get("canonical") or "").strip()
        if not canonical:
            missing.append(p)
            continue
        if e.get("canonical_count", 0) > 1:
            multiple.append(p)
        host = urlsplit(canonical).hostname or ""
        if host and origin_host and host.replace("www.", "") != origin_host.replace("www.", ""):
            offsite.append((p, canonical))
        canon_targets.setdefault(canonical.rstrip("/"), []).append(p["url"])
    for target, sources in canon_targets.items():
        distinct = {s.rstrip("/") for s in sources} - {target}
        if len(distinct) >= 2:
            collapsed.append((target, sorted(distinct)))

    # A missing canonical only causes harm once duplicate URL variants actually
    # exist. Without evidence that they do, the finding is an inference about
    # what *could* happen, so it is reported at medium confidence - which the
    # severity model then caps below critical and pulls out of the high band.
    titles_seen = {}
    for p in pages:
        title = ((ex(p).get("title") or "").strip().lower())
        if title:
            titles_seen[title] = titles_seen.get(title, 0) + 1
    link_targets = {edge[1] for edge in
                    as_list(as_dict(snap.get("link_graph")).get("internal_edges"))}
    schemes = {urlsplit(p["url"]).scheme for p in ctx["pages"] if p.get("url")}
    duplicate_signals = []
    if any(count > 1 for count in titles_seen.values()):
        duplicate_signals.append("duplicate page titles")
    if any("?" in url for url in link_targets):
        duplicate_signals.append("parameterised internal links")
    if len(schemes) > 1:
        duplicate_signals.append("both http and https URLs")
    canonical_confidence = "high" if duplicate_signals else "medium"
    duplicate_note = (" Duplicate-variant evidence observed: %s."
                      % ", ".join(duplicate_signals) if duplicate_signals
                      else " No duplicate URL variants were observed in the sample, so this "
                           "is reported as exposure rather than confirmed harm.")

    if missing and len(missing) == len(pages):
        r.add("CR-20", "No page declares a canonical URL", pillar=AI_DISCOVERABILITY,
              category="canonicalisation", status=FAIL, impact_base=2,
              confidence=canonical_confidence,
              affected=len(missing), applicable=len(pages),
              evidence="0 of %d sampled pages declare `<link rel=\"canonical\">`.%s"
                       % (len(pages), duplicate_note),
              mechanism="Without a canonical, every URL variant a site emits - tracking "
                        "parameters, trailing slashes, http/https, www/non-www, filtered "
                        "listings - is a separate candidate document. Ranking signals split "
                        "across the duplicates and an arbitrary variant is picked as the one "
                        "to cite.",
              evidence_items=[{"url": p["url"], "observed": "no canonical link",
                               "expected": "self-referencing canonical"} for p in missing[:6]],
              affected_urls=[p["url"] for p in missing],
              remediation={"summary": "Emit a self-referencing absolute canonical on every page.",
                           "steps": ["Render `<link rel=\"canonical\" href=\"https://host/path\">` with the clean, parameter-free URL.",
                                     "Use one host and one protocol consistently; redirect the rest.",
                                     "For paginated series, canonical each page to itself, not to page 1."],
                           "expected_benefit": "Consolidates duplicate variants onto one citable URL.",
                           "effort": "low"},
              dedupe_key="canonical-missing")
    elif missing:
        r.add("CR-20", "Some pages declare no canonical URL", pillar=AI_DISCOVERABILITY,
              category="canonicalisation", status=FAIL, impact_base=1,
              confidence=canonical_confidence,
              affected=len(missing), applicable=len(pages),
              evidence="%d of %d sampled pages declare no canonical URL (%s)."
                       % (len(missing), len(pages),
                          ", ".join(short(p["url"], ctx["origin"]) for p in missing[:4])),
              mechanism="Mixed canonical coverage leaves the unmarked templates exposed to "
                        "duplicate-variant splitting while the rest of the site is protected.",
              affected_urls=[p["url"] for p in missing],
              evidence_items=[{"url": p["url"], "observed": "no canonical link",
                               "expected": "self-referencing canonical"} for p in missing[:6]],
              remediation={"summary": "Extend the canonical tag to the templates that lack it.",
                           "steps": ["Identify which templates render without a canonical and add it to the shared head partial."],
                           "expected_benefit": "Consistent duplicate handling across the site.",
                           "effort": "low"},
              dedupe_key="canonical-missing")
    else:
        r.add("CR-20", "Pages declare canonical URLs", pillar=AI_DISCOVERABILITY,
              category="canonicalisation", status=PASS,
              evidence="%d of %d sampled pages declare a canonical URL." % (len(pages), len(pages)),
              applicable=len(pages))

    if collapsed:
        target, sources = max(collapsed, key=lambda t: len(t[1]))
        total = sum(len(s) for _, s in collapsed)
        r.add("CR-21", "Distinct pages point their canonical at another page",
              pillar=AI_DISCOVERABILITY, category="canonicalisation", status=FAIL,
              impact_base=3, confidence="high", affected=total, applicable=len(pages),
              evidence="%d sampled pages declare a canonical URL that is a different page; "
                       "%d of them collapse onto %s."
                       % (total, len(sources), short(target, ctx["origin"])),
              mechanism="A cross-page canonical is an instruction to drop this URL and index "
                        "the target instead. Applied to distinct content, it deletes those "
                        "pages from the index: they can never be returned or cited, and the "
                        "site appears to have far less content than it does.",
              evidence_items=[{"url": s, "observed": "canonical -> %s" % target,
                               "expected": "canonical -> itself"} for s in sources[:6]],
              affected_urls=sources,
              remediation={"summary": "Make canonicals self-referencing unless the page is a true duplicate.",
                           "steps": ["Find the template hard-coding a single canonical value (often the home page URL).",
                                     "Render the canonical from the current request path.",
                                     "Reserve cross-page canonicals for genuine duplicates such as print or parameter variants."],
                           "expected_benefit": "Restores index eligibility for every distinct page.",
                           "effort": "low"},
              dedupe_key="canonical-collapse", tags=["indexation-signal"])
    if offsite:
        r.add("CR-22", "Canonical URLs point to a different domain",
              pillar=AI_DISCOVERABILITY, category="canonicalisation", status=FAIL,
              impact_base=3, confidence="high", affected=len(offsite), applicable=len(pages),
              evidence="%d of %d sampled pages declare a canonical URL on another host (e.g. %s -> %s)."
                       % (len(offsite), len(pages), short(offsite[0][0]["url"], ctx["origin"]),
                          offsite[0][1]),
              mechanism="A cross-domain canonical hands the indexing credit for this page to "
                        "another site. Assistants then cite that other domain, so the brand's "
                        "own URL never appears even when the content originates here.",
              evidence_items=[{"url": p["url"], "observed": "canonical -> %s" % c,
                               "expected": "canonical on this host"} for p, c in offsite[:6]],
              affected_urls=[p["url"] for p, _ in offsite],
              remediation={"summary": "Point canonicals at this host unless syndication is intentional.",
                           "steps": ["Check whether the value is a leftover from a migration or a staging domain.",
                                     "If content is syndicated to you, canonical to your own URL and ask the origin to link back instead."],
                           "expected_benefit": "Keeps citation credit on the brand's own domain.",
                           "effort": "low"},
              dedupe_key="canonical-offsite")
    if multiple:
        r.add("CR-23", "Pages declare more than one canonical URL",
              pillar=AI_DISCOVERABILITY, category="canonicalisation", status=FAIL,
              impact_base=2, confidence="high", affected=len(multiple), applicable=len(pages),
              evidence="%d of %d sampled pages contain multiple `rel=canonical` links."
                       % (len(multiple), len(pages)),
              mechanism="Conflicting canonical declarations are treated as an unreliable signal "
                        "and are usually ignored altogether, so the duplicate handling the tags "
                        "were meant to provide silently stops working.",
              evidence_items=[{"url": p["url"],
                               "observed": "%d canonical links" % ex(p).get("canonical_count", 0),
                               "expected": "exactly one"} for p in multiple[:6]],
              affected_urls=[p["url"] for p in multiple],
              remediation={"summary": "Emit exactly one canonical link per page.",
                           "steps": ["Look for a plugin or template partial adding a second tag.",
                                     "Consolidate to a single head partial that owns the canonical."],
                           "expected_benefit": "Makes the canonical signal usable again.",
                           "effort": "low"},
              dedupe_key="canonical-multiple")


def check_redirects(snap, ctx, r):
    pages = ctx["pages"]
    if not pages:
        r.add("CR-24", "Redirect behaviour", pillar=AI_DISCOVERABILITY,
              category="canonicalisation", status=NOT_EVALUATED,
              applicability="No page responses were captured.")
        return
    chained = [p for p in pages if len(p.get("redirects") or []) >= 2]
    link_chains = [l for l in as_list(snap.get("link_checks"))
                   if isinstance(l, dict) and l.get("redirect_hops", 0) >= 2]
    total = len(pages) + len(snap.get("link_checks") or [])
    if not chained and not link_chains:
        r.add("CR-24", "URLs resolve without redirect chains", pillar=AI_DISCOVERABILITY,
              category="canonicalisation", status=PASS,
              evidence="No sampled URL required more than one redirect hop.",
              applicable=max(1, total))
        return
    items = [{"url": p.get("requested_url"),
              "observed": "%d hops: %s" % (len(p["redirects"]),
                                           " -> ".join(h["status"] and str(h["status"]) or "?"
                                                       for h in p["redirects"])),
              "expected": "at most one redirect"} for p in chained[:5]]
    items += [{"url": l["url"], "observed": "%d hops" % l["redirect_hops"],
               "expected": "at most one redirect"} for l in link_chains[:5]]
    r.add("CR-24", "URLs resolve through multi-hop redirect chains",
          pillar=AI_DISCOVERABILITY, category="canonicalisation", status=FAIL,
          impact_base=2, confidence="high",
          affected=len(chained) + len(link_chains), applicable=max(1, total),
          evidence="%d of %d probed URLs needed two or more redirect hops to resolve."
                   % (len(chained) + len(link_chains), max(1, total)),
          mechanism="Each hop costs a request and some fetchers cap redirect depth or drop the "
                    "request entirely. Chains also dilute the signal attached to the original "
                    "URL, so the destination is treated as weaker than it is.",
          evidence_items=items,
          affected_urls=[p.get("requested_url") for p in chained] + [l["url"] for l in link_chains],
          remediation={"summary": "Collapse chains so every legacy URL redirects once to the final URL.",
                       "steps": ["Map each legacy URL to its final destination and rewrite the rule to point there directly.",
                                 "Order rules so protocol and host normalisation happen in the same hop as the path rewrite.",
                                 "Update internal links to the final URL so the redirect is only needed for external traffic."],
                       "expected_benefit": "Fewer dropped fetches and undiluted signals on the destination.",
                       "effort": "medium"},
          dedupe_key="redirect-chains")


def check_broken_internal_links(snap, ctx, r):
    checks = as_list(snap.get("link_checks"))
    page_failures = [p for p in ctx["pages"]
                     if p.get("status") and 400 <= p["status"] < 600
                     and p.get("discovery") == "link"]
    # Only an actual error status proves a link is broken. A probe that never
    # got a response failed at the transport layer, and since internal links
    # share the audited host, that means the audit's connectivity failed rather
    # than the link.
    broken = [l for l in checks if l.get("status") and 400 <= l["status"] < 600]
    unverified = [l for l in checks if l.get("status") is None]
    total = len(checks) + len([p for p in ctx["pages"] if p.get("discovery") == "link"])
    if total == 0:
        r.add("CR-25", "Internal link integrity", pillar=AI_DISCOVERABILITY,
              category="link_integrity", status=NOT_EVALUATED,
              applicability="No internal links were available to probe.")
        return
    all_broken = broken + [{"url": p["url"], "status": p["status"], "reference_count": 1,
                            "referenced_from": [{"from": p.get("discovered_from", ""),
                                                 "text": p.get("anchor_text", "")}]}
                           for p in page_failures]
    if not all_broken:
        if unverified and len(unverified) == len(checks) and checks:
            r.add("CR-25", "Internal link integrity", pillar=AI_DISCOVERABILITY,
                  category="link_integrity", status=NOT_EVALUATED,
                  applicability="None of the %d probed internal links returned an HTTP "
                                "response, so link integrity could not be established."
                                % len(unverified))
            return
        r.add("CR-25", "Sampled internal links resolve", pillar=AI_DISCOVERABILITY,
              category="link_integrity", status=PASS,
              evidence="%d of %d probed internal links returned a successful status%s."
                       % (total - len(unverified), total,
                          "; %d could not be verified (no response)" % len(unverified)
                          if unverified else ""),
              applicable=total, unit="links")
        return
    worst = sorted(all_broken, key=lambda l: (-l.get("reference_count", 1), l["url"]))
    r.add("CR-25", "Internal links point at pages that do not resolve",
          pillar=AI_DISCOVERABILITY, category="link_integrity", status=FAIL,
          impact_base=3 if any(l.get("reference_count", 1) >= 3 for l in all_broken) else 2,
          confidence="high", affected=len(all_broken), applicable=total, unit="links",
          evidence="%d of %d probed internal links returned an error (%s)."
                   % (len(all_broken), total,
                      ", ".join("%s -> %s" % (short(l["url"], ctx["origin"]),
                                              l.get("status") or "no response")
                                for l in worst[:4])),
          mechanism="A broken link is a dead end for both audiences: a crawler discovers "
                    "nothing beyond it, and a visitor who follows it lands on an error and "
                    "usually leaves. Links repeated in site-wide navigation multiply the cost "
                    "across every page that carries them.",
          evidence_items=[{"url": l["url"], "observed": "HTTP %s, linked from %d page(s)"
                                                        % (l.get("status") or "no response",
                                                           l.get("reference_count", 1)),
                           "expected": "HTTP 200",
                           "excerpt": (l.get("referenced_from") or [{}])[0].get("text", "")[:80]}
                          for l in worst[:8]],
          affected_urls=[l["url"] for l in worst],
          remediation={"summary": "Fix or remove the broken targets, starting with the most-referenced.",
                       "steps": ["Order by how many pages link to each target; site-wide navigation links come first.",
                                 "For moved content, add a single 301 to the new URL.",
                                 "For retired content, remove the link rather than leaving it pointing at a 404."],
                       "expected_benefit": "Recovers crawl paths and removes visible dead ends.",
                       "effort": "low"},
          dedupe_key="broken-internal-links")


def check_soft_404(snap, ctx, r):
    probe = as_dict(snap.get("not_found_probe"))
    if not probe.get("evaluated"):
        r.add("CR-26", "Missing-page handling", pillar=AI_DISCOVERABILITY,
              category="link_integrity", status=NOT_EVALUATED,
              applicability="The 404 probe was skipped for time budget reasons.")
        return
    status = probe.get("status")
    if status is None:
        r.add("CR-26", "Missing-page handling", pillar=AI_DISCOVERABILITY,
              category="link_integrity", status=INCONCLUSIVE,
              applicability="The probe request did not complete: %s" % (probe.get("error") or "no response"))
        return
    if status == 404 or status == 410:
        r.add("CR-26", "Missing pages return a correct 404", pillar=AI_DISCOVERABILITY,
              category="link_integrity", status=PASS,
              evidence="A request for a non-existent path returned HTTP %s." % status,
              applicable=1, unit="sites")
        return
    if status == 200:
        r.add("CR-26", "Missing pages return HTTP 200 (soft 404)",
              pillar=AI_DISCOVERABILITY, category="link_integrity", status=FAIL,
              impact_base=2, confidence="high", affected=1, applicable=1, unit="sites",
              evidence="A request for %s returned HTTP 200 with %d words instead of 404."
                       % (probe.get("url"), probe.get("word_count", 0)),
              mechanism="When every URL returns 200, crawlers cannot tell real pages from "
                        "mistakes. Typos, stale links and generated URL variants all become "
                        "indexable near-duplicates, which dilutes the site's real pages and "
                        "wastes crawl budget that would otherwise reach new content.",
              evidence_items=[{"url": probe.get("url"), "observed": "HTTP 200",
                               "expected": "HTTP 404 or 410",
                               "excerpt": (probe.get("title") or "")[:80]}],
              remediation={"summary": "Return a real 404 status for unknown URLs.",
                           "steps": ["Make the catch-all route respond with status 404 while still rendering the friendly page.",
                                     "Reserve redirects for URLs that genuinely moved; do not redirect all unknown paths to the home page."],
                           "expected_benefit": "Stops unbounded duplicate URLs entering the index.",
                           "effort": "low"},
              dedupe_key="soft-404")
        return
    if 300 <= status < 400 or probe.get("redirected_to"):
        r.add("CR-26", "Missing pages redirect instead of returning 404",
              pillar=AI_DISCOVERABILITY, category="link_integrity", status=FAIL,
              impact_base=1, confidence="medium", affected=1, applicable=1, unit="sites",
              evidence="A request for a non-existent path returned HTTP %s and resolved to %s."
                       % (status, probe.get("redirected_to") or "another URL"),
              mechanism="Redirecting all unknown URLs hides broken links from monitoring and "
                        "creates an unbounded set of URLs that all resolve to the same page.",
              remediation={"summary": "Return 404 for unknown paths; redirect only real moves.",
                           "steps": ["Restrict the catch-all redirect to a known list of legacy URLs."],
                           "expected_benefit": "Broken links become visible and fixable.",
                           "effort": "low"},
              dedupe_key="soft-404")
        return
    r.add("CR-26", "Missing-page handling", pillar=AI_DISCOVERABILITY,
          category="link_integrity", status=INCONCLUSIVE,
          applicability="The probe returned HTTP %s, which is neither a clear 404 nor a "
                        "clear soft-404." % status)


def check_sitemap(snap, ctx, r):
    if transport_blocked(ctx, r, "CR-27", "Sitemap discoverability", "discovery_paths"):
        return
    sitemaps = as_list(snap.get("sitemaps"))
    robots = as_dict(snap.get("robots"))
    usable = [s for s in sitemaps if s.get("ok") and s.get("url_count", 0) > 0]
    declared = robots.get("sitemaps") or []
    if usable:
        if not declared:
            r.add("CR-27", "Sitemap exists but is not declared in robots.txt",
                  pillar=AI_DISCOVERABILITY, category="discovery_paths", status=FAIL,
                  impact_base=1, confidence="high", affected=1, applicable=1, unit="sites",
                  evidence="%s is reachable with %d URLs, but robots.txt declares no `Sitemap:` line."
                           % (usable[0]["url"], usable[0]["url_count"]),
                  mechanism="robots.txt is the one place every crawler checks before anything "
                            "else. An undeclared sitemap is only found by guessing the "
                            "conventional filename, so crawlers that do not guess rely entirely "
                            "on following links.",
                  remediation={"summary": "Add a `Sitemap:` line to robots.txt.",
                               "steps": ["Append `Sitemap: https://host/sitemap.xml` (absolute URL) to robots.txt.",
                                         "List every sitemap or a single sitemap index."],
                               "expected_benefit": "Guaranteed discovery of the full URL inventory.",
                               "effort": "low"},
                  dedupe_key="sitemap-undeclared")
        else:
            r.add("CR-27", "A sitemap is published and discoverable",
                  pillar=AI_DISCOVERABILITY, category="discovery_paths", status=PASS,
                  evidence="%d sitemap file(s) returned %d URLs; robots.txt declares %d sitemap location(s)."
                           % (len(usable), sum(s["url_count"] for s in usable), len(declared)),
                  applicable=1, unit="sites")
        return
    broken = [x for x in sitemaps
              if (x.get("responded") or x.get("status") is not None)
              and (not x.get("ok") or x.get("url_count", 0) == 0)]
    if declared and broken:
        r.add("CR-27", "Declared sitemap is unreachable or empty", pillar=AI_DISCOVERABILITY,
              category="discovery_paths", status=FAIL, impact_base=2, confidence="high",
              affected=len(broken), applicable=max(1, len(sitemaps)), unit="sitemaps",
              evidence="robots.txt declares %d sitemap location(s); %d returned an error or "
                       "contained no URLs (%s -> HTTP %s)."
                       % (len(declared), len(broken), broken[0]["url"],
                          broken[0].get("status")),
              mechanism="Crawlers use the declared sitemap as the authoritative inventory. When "
                        "it fails, discovery falls back entirely to internal links, so any page "
                        "not linked from a crawled page is never found.",
              evidence_items=[{"url": s["url"], "observed": "HTTP %s, %d URLs"
                                                            % (s.get("status"), s.get("url_count", 0)),
                               "expected": "HTTP 200 with the site's URL inventory"} for s in broken[:5]],
              remediation={"summary": "Repair the declared sitemap or remove the stale declaration.",
                           "steps": ["Fetch the declared URL and confirm it returns 200 with valid XML.",
                                     "Regenerate it on publish so new URLs appear automatically.",
                                     "Delete `Sitemap:` lines that point at files you no longer produce."],
                           "expected_benefit": "Restores complete, prompt URL discovery.",
                           "effort": "low"},
              dedupe_key="sitemap-broken")
        return
    # Absence may only be asserted from real answers. A candidate that never got
    # a response proves nothing about whether a sitemap exists.
    answered = [x for x in sitemaps
                if x.get("responded") or x.get("status") is not None]
    silent = [x for x in sitemaps
              if not (x.get("responded") or x.get("status") is not None)]
    if not answered:
        kinds = sorted({x.get("error_kind") or "no response" for x in silent})
        r.add("CR-27", "Sitemap discoverability", pillar=AI_DISCOVERABILITY,
              category="discovery_paths", status=NOT_EVALUATED,
              applicability="None of the %d sitemap location(s) tried returned an HTTP "
                            "response (%s), so it is unknown whether the site "
                            "publishes a sitemap."
                            % (max(1, len(silent)), ", ".join(kinds) or "no response"))
        return
    statuses = "/".join(sorted({str(x.get("status")) for x in answered}))
    r.add("CR-27", "No XML sitemap was found", pillar=AI_DISCOVERABILITY,
          category="discovery_paths", status=FAIL, impact_base=2, confidence="high",
          affected=1, applicable=1, unit="sites",
          evidence="Neither robots.txt nor the conventional locations (/sitemap.xml, "
                   "/sitemap_index.xml) produced a usable sitemap; %d candidate "
                   "location(s) answered with HTTP %s." % (len(answered), statuses),
          mechanism="Without a sitemap, a crawler can only find pages by following links from "
                    "pages it already knows. New, deep and weakly-linked pages are discovered "
                    "late or not at all, and there is no signal about when a page last changed.",
          remediation={"summary": "Publish an XML sitemap and declare it in robots.txt.",
                       "steps": ["Generate a sitemap listing every canonical, indexable URL.",
                                 "Include an accurate `<lastmod>` per URL, updated on publish.",
                                 "Add `Sitemap: https://host/sitemap.xml` to robots.txt.",
                                 "Split into a sitemap index above 50,000 URLs."],
                       "expected_benefit": "Complete discovery of the URL inventory plus a "
                                           "freshness signal per page.",
                       "effort": "low"},
          dedupe_key="sitemap-missing")


def check_crawl_traps(snap, ctx, r):
    pages = ctx["pages"]
    edges = as_list(as_dict(snap.get("link_graph")).get("internal_edges"))
    if not edges:
        r.add("CR-28", "Crawl-space hygiene", pillar=AI_DISCOVERABILITY,
              category="discovery_paths", status=NOT_EVALUATED,
              applicability="No internal link graph was captured.")
        return
    targets = {edge[1] for edge in edges}
    parameterised: dict = {}
    for url in targets:
        parts = urlsplit(url)
        if not parts.query:
            continue
        keys = tuple(sorted({kv.split("=", 1)[0].lower() for kv in parts.query.split("&") if kv}))
        parameterised.setdefault((parts.path, keys), set()).add(url)
    explosive = {k: v for k, v in parameterised.items() if len(v) >= 8}
    if not explosive:
        r.add("CR-28", "No parameter-driven crawl trap observed", pillar=AI_DISCOVERABILITY,
              category="discovery_paths", status=PASS,
              evidence="%d distinct internal link targets, none producing 8+ parameter "
                       "permutations of the same path." % len(targets),
              applicable=len(targets), unit="links")
        return
    worst = max(explosive.items(), key=lambda kv: len(kv[1]))
    total = sum(len(v) for v in explosive.values())
    r.add("CR-28", "Faceted parameters generate large numbers of near-duplicate URLs",
          pillar=AI_DISCOVERABILITY, category="discovery_paths", status=FAIL,
          impact_base=2, confidence="medium", affected=total, applicable=len(targets),
          unit="links",
          evidence="%d internal links point at parameter permutations of %d path(s); the "
                   "worst is %s with %d variants of the parameters %s."
                   % (total, len(explosive), worst[0][0], len(worst[1]), ", ".join(worst[0][1])),
          mechanism="Every parameter combination is a distinct URL to a crawler. A handful of "
                    "filters multiplies into thousands of near-identical pages, which consumes "
                    "the crawl budget that should be reaching real content and splits ranking "
                    "signals across duplicates.",
          evidence_items=[{"url": sorted(worst[1])[0],
                           "observed": "%d permutations of parameters %s"
                                       % (len(worst[1]), ", ".join(worst[0][1])),
                           "expected": "one canonical listing URL"}],
          affected_urls=sorted(worst[1])[:20],
          remediation={"summary": "Stop filter combinations from becoming separate crawlable URLs.",
                       "steps": ["Canonicalise every filtered variant to the unfiltered listing URL.",
                                 "Render filter controls so that combinations do not produce crawlable `<a href>` links (use form controls or add `rel=\"nofollow\"`).",
                                 "Keep as indexable only the small set of filter pages with genuine search demand."],
                       "expected_benefit": "Crawl budget reaches real content instead of permutations.",
                       "effort": "medium"},
          dedupe_key="crawl-trap")


# ISO 639-1 primary subtags. Used only to decide whether a two-letter path
# segment is plausibly a language root, so that /it/ (Italian) is treated as a
# locale and /us/ or /go/ are not.
ISO_639_1 = frozenset("""
aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co
cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl
gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg
ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk
ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps
pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta
te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za
zh zu
""".split())


# ISO 3166-1 alpha-2 regions. `uk` is deliberately absent: it is reserved but
# never assigned, and `hreflang="en-uk"` is the most common invalid value there
# is - the region subtag for the United Kingdom is `gb`.
ISO_3166_1 = frozenset("""
ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bl
bm bn bo bq br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv
cw cx cy cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr ga gb gd
ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im
in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk
lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw
mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr
ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss
st sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug um us uy
uz va vc ve vg vi vn vu wf ws ye yt za zm zw
""".split())


def valid_language_tag(code):
    """Is `code` a usable hreflang value?

    BCP 47 shape: language, optional 4-letter script, optional region that is
    either an ISO 3166-1 alpha-2 code or a three-digit UN M.49 area.
    """
    code = (code or "").strip().lower()
    if code == "x-default":
        return True
    parts = re.split(r"[-_]", code)
    if not parts or parts[0] not in ISO_639_1:
        return False
    rest = parts[1:]
    if rest and len(rest[0]) == 4 and rest[0].isalpha():
        rest = rest[1:]                      # script subtag, e.g. zh-hant-tw
    if not rest:
        return True
    region = rest[0]
    if len(region) == 2 and region.isalpha():
        return region in ISO_3166_1
    if len(region) == 3 and region.isdigit():
        return True                          # UN M.49 area code, e.g. es-419
    return False


def locale_root(url, origin):
    """The first path segment of `url`, when it is a plausible locale root."""
    path = short(url, origin)
    first = path.strip("/").split("/")[0].lower() if path.strip("/") else ""
    if not first:
        return ""
    stem = re.split(r"[-_]", first)[0]
    if len(first) <= 5 and stem in ISO_639_1:
        return first
    return ""


def check_meta_refresh(snap, ctx, r):
    """A <meta http-equiv="refresh"> used as a redirect.

    This is a client-side redirect expressed in the document body rather than in
    the HTTP response. A browser honours it; a fetcher frequently does not. The
    page that gets stored is therefore the near-empty waypoint, not the
    destination - so the content the author intended to publish is never seen.
    """
    if transport_blocked(ctx, r, "CR-32", "Meta-refresh redirects", "crawl_access"):
        return
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-32", "Meta-refresh redirects", pillar=AI_DISCOVERABILITY,
              category="crawl_access", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return

    offenders = []
    for page in pages:
        equiv = as_dict(ex(page).get("meta_http_equiv"))
        raw = ""
        for key, value in equiv.items():
            if str(key).strip().lower() == "refresh":
                raw = str(value or "")
                break
        if not raw:
            continue
        target = re.search(r"url\s*=\s*(.+)$", raw, re.I)
        if not target:
            # A refresh with no URL reloads the same page. That is a polling
            # widget, not a redirect, and it hides nothing.
            continue
        delay = re.match(r"\s*(\d+)", raw)
        offenders.append({
            "page": page,
            "target": target.group(1).strip().strip("'\"")[:200],
            "delay": int(delay.group(1)) if delay else 0,
            "words": int(ex(page).get("main_word_count") or ex(page).get("word_count") or 0),
        })

    if not offenders:
        r.add("CR-32", "No page redirects with a meta refresh", pillar=AI_DISCOVERABILITY,
              category="crawl_access", status=PASS, applicable=len(pages),
              evidence="0 of %d sampled pages use `<meta http-equiv=\"refresh\">` to "
                       "redirect." % len(pages))
        return

    worst = sorted(offenders, key=lambda o: (o["words"], o["delay"]))[0]
    r.add("CR-32", "Pages redirect with a meta refresh instead of an HTTP redirect",
          pillar=AI_DISCOVERABILITY, category="crawl_access", status=FAIL,
          impact_base=3, confidence="high",
          affected=len(offenders), applicable=len(pages),
          evidence="%d of %d sampled pages redirect using "
                   "`<meta http-equiv=\"refresh\">` (e.g. %s redirects to %r after %ds "
                   "and carries %d words of its own)."
                   % (len(offenders), len(pages), short(worst["page"]["url"], ctx["origin"]),
                      worst["target"], worst["delay"], worst["words"]),
          mechanism="A meta refresh is a redirect written in the document rather than in "
                    "the HTTP response. Browsers follow it, but many crawlers and most "
                    "AI fetchers do not - they keep the document they were served. That "
                    "document is the waypoint, which is usually near-empty, so the "
                    "destination content is never indexed and the URL that gets cited "
                    "resolves to nothing worth quoting. A 301 or 302 carries the same "
                    "intent in a form every client already honours.",
          evidence_items=[{"url": o["page"]["url"],
                           "observed": "meta refresh to %r after %ds, %d words on the page"
                                       % (o["target"], o["delay"], o["words"]),
                           "expected": "an HTTP 301/302 redirect"} for o in offenders[:8]],
          affected_urls=[o["page"]["url"] for o in offenders],
          remediation={
              "summary": "Replace every meta-refresh redirect with a server-issued HTTP "
                         "301 or 302.",
              "steps": ["Return `301 Moved Permanently` for a permanent move and `302 "
                        "Found` for a temporary one, with the destination in the "
                        "`Location` header.",
                        "Remove the `<meta http-equiv=\"refresh\">` tag once the server "
                        "redirect is in place; leaving both causes a double hop.",
                        "Where the waypoint exists for consent, language selection or "
                        "interstitial messaging, serve the real content at the URL and "
                        "layer the interstitial over it rather than redirecting away.",
                        "Verify with `curl -sI <url>` that the status is 30x and "
                        "`Location` is set."],
              "expected_benefit": "The destination becomes the document that fetchers "
                                  "store, so the page can be read and cited at all.",
              "effort": "low"},
          dedupe_key="meta-refresh-redirect")


def check_duplicate_head_metadata(snap, ctx, r):
    """More than one <title>, or a repeated <meta name=...>, in one document.

    HTML permits exactly one title. When a template and a plugin each emit one,
    which survives is decided by the parser, not the author - and different
    parsers decide differently. Repeated `robots` is the dangerous case: two
    directives that disagree are usually resolved to the most restrictive.
    """
    if transport_blocked(ctx, r, "CR-33", "Duplicated head metadata", "page_metadata"):
        return
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-33", "Duplicated head metadata", pillar=AI_DISCOVERABILITY,
              category="page_metadata", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return

    # Only metadata whose value changes what is indexed or displayed. A repeated
    # `viewport` is untidy; a repeated `robots` can silently deindex a page.
    DECISIVE = ("robots", "description")
    offenders, has_robots_conflict = [], False
    for page in pages:
        e = ex(page)
        problems = []
        if int(e.get("title_count") or 0) > 1:
            problems.append("%d <title> elements" % int(e["title_count"]))
        repeated = [n for n in as_list(e.get("meta_duplicates"))
                    if str(n).strip().lower() in DECISIVE]
        for name in repeated:
            problems.append("repeated <meta name=\"%s\">" % name)
            if str(name).strip().lower() == "robots":
                has_robots_conflict = True
        if problems:
            offenders.append({"page": page, "problems": problems})

    if not offenders:
        r.add("CR-33", "Head metadata is declared once per page",
              pillar=AI_DISCOVERABILITY, category="page_metadata", status=PASS,
              applicable=len(pages),
              evidence="%d of %d sampled pages declare a single `<title>` and no repeated "
                       "decisive `<meta name>`." % (len(pages), len(pages)))
        return

    r.add("CR-33", "Pages declare the same head metadata more than once",
          pillar=AI_DISCOVERABILITY, category="page_metadata", status=FAIL,
          impact_base=3 if has_robots_conflict else 2, confidence="high",
          affected=len(offenders), applicable=len(pages),
          evidence="%d of %d sampled pages emit duplicated head metadata (e.g. %s: %s)."
                   % (len(offenders), len(pages),
                      short(offenders[0]["page"]["url"], ctx["origin"]),
                      "; ".join(offenders[0]["problems"])),
          mechanism="HTML allows one `<title>` and one meaningful value per metadata "
                    "name. When a theme, a plugin and a CMS field each emit their own, "
                    "the winner is chosen by whichever parser reads the page - so the "
                    "title an assistant cites, or the description it shows, is not the "
                    "one the author wrote%s"
                    % (". A repeated `robots` directive is worse: conflicting values are "
                       "typically resolved to the most restrictive, so a stray `noindex` "
                       "silently removes the page from every index that reads it."
                       if has_robots_conflict else
                       ". The value is arbitrary rather than chosen."),
          evidence_items=[{"url": o["page"]["url"],
                           "observed": "; ".join(o["problems"]),
                           "expected": "one <title> and one value per decisive meta name"}
                          for o in offenders[:8]],
          affected_urls=[o["page"]["url"] for o in offenders],
          remediation={
              "summary": "Emit each head element exactly once, from a single owner in "
                         "the template.",
              "steps": ["Find every component that writes a `<title>` or a `<meta "
                        "name=\"description\">` and leave exactly one responsible for it.",
                        "Where an SEO plugin overlaps a hand-written layout, disable the "
                        "duplicate in whichever layer is not authoritative.",
                        "For `robots`, consolidate to one tag; if a page must be excluded, "
                        "say so once rather than contradicting an earlier directive.",
                        "Assert it in CI: fetch each template and count occurrences of "
                        "`<title` and `name=\"description\"` in the raw HTML."],
              "expected_benefit": "Restores author control over the string that is indexed "
                                  "and displayed, and removes the risk of an accidental "
                                  "noindex.",
              "effort": "low"},
          dedupe_key="duplicate-head-metadata", tags=["render-gap-symptom"])


def check_locale_alternates(snap, ctx, r):
    """Whether a multilingual site declares hreflang alternates, and correctly.

    Localised variants of one page are near-duplicates in different languages.
    Without `hreflang` they compete with each other: a retrieval system picks one
    arbitrarily, so a reader can be handed the wrong-language page, and the
    authority earned by the set is split across its members. This is only asked
    of sites that show evidence of being localised at all.
    """
    if transport_blocked(ctx, r, "CR-34", "Locale alternates", "internationalisation"):
        return
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-34", "Locale alternates", pillar=AI_DISCOVERABILITY,
              category="internationalisation", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return

    declared, langs, roots = {}, set(), set()
    for page in pages:
        e = ex(page)
        lang = (e.get("html_lang") or "").strip().lower()
        if lang:
            langs.add(re.split(r"[-_]", lang)[0])
        root = locale_root(page["url"], ctx["origin"])
        if root:
            roots.add(root)
        entries = [en for en in as_list(e.get("hreflang")) if isinstance(en, dict)]
        if entries:
            declared[page["url"]] = entries

    # Localisation evidence, from the sample only. Two distinct language roots in
    # the URL space, or two distinct <html lang> values, mean variants exist.
    signals = []
    if len(roots) >= 2:
        signals.append("%d locale path roots (%s)"
                       % (len(roots), ", ".join("/%s/" % x for x in sorted(roots)[:4])))
    if len(langs) >= 2:
        signals.append("%d distinct <html lang> values (%s)"
                       % (len(langs), ", ".join(sorted(langs)[:4])))

    if not signals and not declared:
        r.add("CR-34", "Locale alternates", pillar=AI_DISCOVERABILITY,
              category="internationalisation", status=NOT_APPLICABLE,
              applicability="The sample shows one language and no locale-segmented URLs, "
                            "so there are no alternate versions for `hreflang` to relate. "
                            "A monolingual site does not need it.")
        return

    if signals and not declared:
        r.add("CR-34", "Localised pages exist but declare no hreflang alternates",
              pillar=AI_DISCOVERABILITY, category="internationalisation", status=FAIL,
              impact_base=2, confidence="medium",
              affected=len(pages), applicable=len(pages),
              evidence="The sample shows %s, but 0 of %d sampled pages declare "
                       "`<link rel=\"alternate\" hreflang=...>`."
                       % (" and ".join(signals), len(pages)),
              mechanism="Localised pages say much the same thing in different languages, "
                        "so to a machine they are near-duplicates competing for the same "
                        "meaning. `hreflang` is what says 'these are the same document for "
                        "different audiences, pick by locale'. Without it the system picks "
                        "one variant and suppresses the rest, so a reader can be given the "
                        "wrong language and the ranking signals earned by the whole set are "
                        "split across its members instead of pooled.",
              evidence_items=[{"url": p["url"],
                               "observed": "no hreflang alternates",
                               "expected": "a reciprocal alternate per locale, plus x-default"}
                              for p in pages[:6]],
              affected_urls=[p["url"] for p in pages],
              remediation={
                  "summary": "Declare reciprocal hreflang alternates on every localised "
                             "page, including a self-reference and an x-default.",
                  "steps": ["In each page's `<head>`, list every locale variant of that "
                            "page: `<link rel=\"alternate\" hreflang=\"en-gb\" "
                            "href=\"https://host/en-gb/path\">`.",
                            "Include the page itself in its own list; a set without a "
                            "self-reference is ignored by most consumers.",
                            "Add `hreflang=\"x-default\"` pointing at the locale selector "
                            "or the most general variant.",
                            "Keep the set reciprocal: if A lists B, B must list A, or the "
                            "whole cluster is discarded.",
                            "Use language codes alone (`en`) unless the regional variants "
                            "genuinely differ in content (`en-gb` versus `en-us`)."],
                  "expected_benefit": "Consolidates the authority of the localised set and "
                                      "makes the right-language page the one that is served "
                                      "and cited.",
                  "effort": "medium"},
              dedupe_key="hreflang-missing")
        return

    # hreflang is declared. Check the two failures that void a cluster.
    bad_codes, missing_self = [], []
    for url, entries in declared.items():
        codes = []
        for entry in entries:
            code = str(entry.get("hreflang") or "").strip().lower()
            codes.append(code)
            if not valid_language_tag(code):
                bad_codes.append((url, code))
        targets = {str(e.get("href") or "").rstrip("/") for e in entries}
        if url.rstrip("/") not in targets:
            missing_self.append(url)

    if bad_codes:
        r.add("CR-34", "hreflang alternates use language codes that are not valid",
              pillar=AI_DISCOVERABILITY, category="internationalisation", status=FAIL,
              impact_base=2, confidence="high",
              affected=len({u for u, _ in bad_codes}), applicable=len(declared),
              evidence="%d hreflang value(s) across %d of %d declaring pages are not a "
                       "valid language subtag (e.g. %r on %s)."
                       % (len(bad_codes), len({u for u, _ in bad_codes}), len(declared),
                          bad_codes[0][1], short(bad_codes[0][0], ctx["origin"])),
              mechanism="An `hreflang` value must be an ISO 639-1 language code, "
                        "optionally with a region, or `x-default`. A country code on its "
                        "own (`uk`, `eu`) or an invented value does not parse, and a "
                        "cluster containing an unparseable member is commonly discarded "
                        "whole - so the site pays for the markup and gets none of the "
                        "consolidation it was meant to buy.",
              evidence_items=[{"url": url, "observed": "hreflang=%r" % code,
                               "expected": "an ISO 639-1 language code, optionally with a "
                                           "region, or x-default"}
                              for url, code in bad_codes[:8]],
              affected_urls=[u for u, _ in bad_codes],
              remediation={
                  "summary": "Correct every hreflang value to a valid language subtag.",
                  "steps": ["Use the language first: `en`, `de`, `pt`. Add a region only "
                            "when the content differs: `pt-br` versus `pt-pt`.",
                            "`en-uk` is invalid; the region code for the United Kingdom is "
                            "`gb`, so the value is `en-gb`.",
                            "Never use a bare country code as the whole value.",
                            "Re-validate the full set after the fix, since one bad member "
                            "invalidates the cluster."],
                  "expected_benefit": "Makes the alternate set parse, so locale selection "
                                      "works and the variants stop competing.",
                  "effort": "low"},
              dedupe_key="hreflang-invalid")
        return

    if missing_self and len(missing_self) > len(declared) // 2:
        r.add("CR-34", "hreflang sets omit a self-reference",
              pillar=AI_DISCOVERABILITY, category="internationalisation", status=FAIL,
              impact_base=1, confidence="medium",
              affected=len(missing_self), applicable=len(declared),
              evidence="%d of %d pages that declare hreflang alternates do not list "
                       "themselves in their own set (e.g. %s)."
                       % (len(missing_self), len(declared),
                          short(missing_self[0], ctx["origin"])),
              mechanism="A page's hreflang set is read as the complete membership list for "
                        "that cluster. A page that omits itself is asserting a group it "
                        "does not belong to, and consumers commonly reject the whole set "
                        "rather than guess - which leaves the variants competing exactly "
                        "as they would with no markup at all.",
              evidence_items=[{"url": url, "observed": "no self-referencing alternate",
                               "expected": "an alternate whose href is this page"}
                              for url in missing_self[:8]],
              affected_urls=missing_self,
              remediation={
                  "summary": "Add a self-referencing alternate to every page in each "
                             "locale cluster.",
                  "steps": ["Generate the alternate list once per cluster and render the "
                            "identical list on every member, including the current page.",
                            "Use absolute URLs that match the canonical exactly, including "
                            "protocol, host and trailing slash.",
                            "Add `x-default` for the fallback the selector should send "
                            "unmatched visitors to."],
                  "expected_benefit": "Makes each cluster self-consistent so locale "
                                      "selection is honoured.",
                  "effort": "low"},
              dedupe_key="hreflang-no-self-reference")
        return

    r.add("CR-34", "Locale alternates are declared and well formed",
          pillar=AI_DISCOVERABILITY, category="internationalisation", status=PASS,
          affected=0, applicable=len(declared), evidence=
          "%d of %d sampled pages declare hreflang alternates; all values are valid "
          "language subtags and each set references itself."
          % (len(declared), len(pages)))


def check_transport_and_weight(snap, ctx, r):
    pages = ctx["html_pages"]
    if not pages:
        r.add("CR-29", "Response weight and compression", pillar=ENGAGEMENT,
              category="delivery", status=NOT_EVALUATED,
              applicability="No HTML page was successfully parsed.")
        return
    uncompressed = [p for p in pages
                    if p.get("bytes", 0) > 100 * 1024
                    and "gzip" not in (p.get("headers", {}) or {}).get("content-encoding", "")
                    and "br" not in (p.get("headers", {}) or {}).get("content-encoding", "")
                    and "deflate" not in (p.get("headers", {}) or {}).get("content-encoding", "")]
    heavy = [p for p in pages if p.get("bytes", 0) > 900 * 1024]
    host = (urlsplit(ctx["origin"]).hostname or "")
    # Plain HTTP is only a defect for a publicly addressable host. Local and
    # private addresses are how the suite (and any staging harness) runs.
    public_host = bool(host) and not (
        host in ("localhost", "::1") or re.match(r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)", host)
        or host.endswith(".local") or host.endswith(".test") or host.endswith(".internal"))
    insecure = [p for p in ctx["pages"] if p["url"].startswith("http://")] if public_host else []
    if insecure:
        r.add("CR-30", "Pages are served over plain HTTP", pillar=AI_DISCOVERABILITY,
              category="delivery", status=FAIL, impact_base=3, confidence="high",
              affected=len(insecure), applicable=len(ctx["pages"]),
              evidence="%d of %d fetched URLs resolved over http:// rather than https://."
                       % (len(insecure), len(ctx["pages"])),
              mechanism="Browsers mark plain HTTP as not secure, which suppresses conversion, "
                        "and search systems prefer the secure variant. Where both exist and "
                        "neither redirects, the two are competing duplicates of every page.",
              evidence_items=[{"url": p["url"], "observed": "served over http://",
                               "expected": "https:// with a permanent redirect from http"}
                              for p in insecure[:5]],
              affected_urls=[p["url"] for p in insecure],
              remediation={"summary": "Serve everything over HTTPS and 301 http to https.",
                           "steps": ["Install a valid certificate for every host you serve.",
                                     "Add a single permanent redirect from http to https.",
                                     "Then enable HSTS once the redirect is verified."],
                           "expected_benefit": "Removes the duplicate host and the browser warning.",
                           "effort": "medium"},
              dedupe_key="insecure-transport")
    if not uncompressed and not heavy:
        r.add("CR-29", "HTML responses are compressed and reasonably sized",
              pillar=ENGAGEMENT, category="delivery", status=PASS,
              evidence="No sampled HTML response exceeded 900 KB or arrived uncompressed above 100 KB.",
              applicable=len(pages))
        return
    affected = {p["url"] for p in uncompressed} | {p["url"] for p in heavy}
    r.add("CR-29", "HTML responses are heavy or uncompressed", pillar=ENGAGEMENT,
          category="delivery", status=FAIL, impact_base=2, confidence="high",
          affected=len(affected), applicable=len(pages),
          evidence="%d of %d sampled pages exceeded 100 KB without content compression; "
                   "%d exceeded 900 KB of HTML (largest: %d KB)."
                   % (len(uncompressed), len(pages), len(heavy),
                      max(p.get("bytes", 0) for p in pages) // 1024),
          mechanism="Document size is on the critical path to first render: the browser cannot "
                    "paint until the HTML arrives and parses. Uncompressed or oversized HTML "
                    "delays the first meaningful paint on slow connections, which is where "
                    "most abandonment happens.",
          evidence_items=[{"url": p["url"],
                           "observed": "%d KB, content-encoding: %s"
                                       % (p.get("bytes", 0) // 1024,
                                          (p.get("headers", {}) or {}).get("content-encoding") or "none"),
                           "expected": "compressed transfer, well under 900 KB"}
                          for p in sorted(uncompressed + heavy, key=lambda x: -x.get("bytes", 0))[:6]],
          affected_urls=sorted(affected),
          remediation={"summary": "Enable text compression and reduce inlined payloads.",
                       "steps": ["Turn on gzip or brotli for text/html, CSS, JS and JSON at the server or CDN.",
                                 "Move large inline scripts, styles and base64 images into cacheable external files.",
                                 "Where a page inlines a hydration payload, ship only the data the first screen needs."],
                       "expected_benefit": "Faster first render, particularly on mobile networks.",
                       "effort": "medium"},
          dedupe_key="page-weight")


CHECKS = [
    check_entry_reachable,
    check_bot_wall,
    check_declared_vs_enforced_access,
    check_robots_readable,
    check_robots_blocks_crawlers,
    check_robots_blocks_own_pages,
    check_indexing_directives,
    check_render_dependency,
    check_thin_content,
    check_titles,
    check_meta_description,
    check_heading_structure,
    check_language_declaration,
    check_semantic_landmarks,
    check_viewport,
    check_canonical,
    check_redirects,
    check_broken_internal_links,
    check_soft_404,
    check_sitemap,
    check_crawl_traps,
    check_meta_refresh,
    check_duplicate_head_metadata,
    check_locale_alternates,
    check_transport_and_weight,
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", default="-", help="snapshot JSON path, or - for stdin")
    parser.add_argument("--out", default="-", help="output path, or - for stdout")
    args = parser.parse_args(argv)

    results = Results(PRODUCER)
    try:
        snapshot = load_snapshot(args.snapshot)
    except Exception as exc:
        results.errors.append({"check": "load_snapshot",
                               "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
        snapshot = {}
    if snapshot:
        try:
            context = build_context(snapshot)
        except Exception as exc:
            results.errors.append({"check": "build_context",
                                   "error": "%s: %s" % (type(exc).__name__,
                                                          str(exc)[:200])})
            context = {"pages": [], "html_pages": [], "entry": None,
                       "origin": "", "site": "", "coverage": {}}
        run_checks(results, snapshot, context, CHECKS)

    payload = json.dumps(results.to_dict(), indent=2, ensure_ascii=False)
    if args.out in ("-", "", None):
        sys.stdout.write(payload + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
