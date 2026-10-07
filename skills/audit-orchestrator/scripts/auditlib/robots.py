"""A robots.txt parser built for compliance *and* for diagnosis.

`urllib.robotparser` decides allow/deny but hides the group structure, so it
cannot answer the question this audit cares about most: *which crawlers has this
site shut out?* This parser keeps every user-agent group intact so the
`crawl-render-audit` skill can report, with evidence, that (say) GPTBot is
disallowed while Googlebot is not.

Semantics follow RFC 9309:
  * group selection is by longest matching user-agent token, case-insensitive;
    `*` is the fallback group and is only used when no specific group matches
  * `Allow` and `Disallow` compete by longest matching pattern; ties go to Allow
  * `*` matches any run of characters, `$` anchors the end of the path
  * an empty `Disallow:` value means "allow everything"
  * a 5xx / unreachable robots.txt is treated as *disallow all* (conservative,
    matches Google's documented behaviour for persistent 5xx); a 404 means
    "no restrictions"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote, urlsplit

MAX_ROBOTS_BYTES = 512 * 1024  # RFC 9309 says parse at least 500 KiB

# Crawlers that feed AI answer engines, retrieval pipelines and assistant
# citations. Blocking these is invisible in classic SEO tooling but decides
# whether a brand can be quoted by an assistant at all.
AI_CRAWLERS = {
    "gptbot": "OpenAI - ChatGPT training and browsing corpus",
    "oai-searchbot": "OpenAI - ChatGPT search index",
    "chatgpt-user": "OpenAI - live page fetch when a user asks about a URL",
    "claudebot": "Anthropic - Claude training and retrieval corpus",
    "claude-web": "Anthropic - Claude live browsing",
    "claude-searchbot": "Anthropic - Claude search index",
    "anthropic-ai": "Anthropic - legacy crawler token",
    "perplexitybot": "Perplexity - answer engine index",
    "perplexity-user": "Perplexity - live page fetch for a user question",
    "google-extended": "Google - Gemini grounding and AI Overviews corpus",
    "applebot-extended": "Apple - Apple Intelligence corpus",
    "ccbot": "Common Crawl - upstream corpus for many AI systems",
    "bytespider": "ByteDance - assistant corpus",
    "meta-externalagent": "Meta - AI corpus",
    "meta-externalfetcher": "Meta - live page fetch",
    "amazonbot": "Amazon - Alexa/Rufus answers",
    "youbot": "You.com - answer engine index",
    "cohere-ai": "Cohere - retrieval corpus",
    "diffbot": "Diffbot - knowledge graph feeding several assistants",
    "timpibot": "Timpi - independent index",
    "petalbot": "Petal - assistant index",
    "mistralai-user": "Mistral - live page fetch",
}

# Classic crawlers. Blocking these removes the brand from the search results
# that assistants fall back on when they need a source.
SEARCH_CRAWLERS = {
    "googlebot": "Google Search",
    "bingbot": "Bing Search (also backs Copilot and several assistants)",
    "duckduckbot": "DuckDuckGo",
    "slurp": "Yahoo",
    "baiduspider": "Baidu",
    "yandexbot": "Yandex",
}


def _pattern_to_regex(pattern: str) -> re.Pattern:
    """Translate a robots path pattern into an anchored regex."""
    out = ["^"]
    i = 0
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    while i < len(body):
        ch = body[i]
        if ch == "*":
            out.append(".*")
        else:
            out.append(re.escape(ch))
        i += 1
    if anchored_end:
        out.append("$")
    return re.compile("".join(out))


@dataclass
class Rule:
    allow: bool
    pattern: str
    line_no: int
    regex: re.Pattern = field(repr=False, default=None)

    def __post_init__(self):
        if self.regex is None:
            self.regex = _pattern_to_regex(self.pattern)

    def match_length(self, path: str) -> int:
        """Length of the pattern if it matches `path`, else -1."""
        if self.pattern == "":
            return -1
        return len(self.pattern) if self.regex.match(path) else -1


@dataclass
class Group:
    agents: list = field(default_factory=list)
    rules: list = field(default_factory=list)
    crawl_delay: Optional[float] = None

    def blocks_everything(self) -> bool:
        """True when the group disallows `/` with no re-allowing rule."""
        has_root_block = any(
            r.pattern in ("/", "/*") and not r.allow for r in self.rules
        )
        if not has_root_block:
            return False
        return not any(r.allow for r in self.rules)


@dataclass
class RobotsPolicy:
    """Parsed robots.txt plus the transport facts needed to explain it."""

    url: str = ""
    status: Optional[int] = None
    fetched: bool = False
    error: str = ""
    raw_excerpt: str = ""
    byte_size: int = 0
    groups: list = field(default_factory=list)
    sitemaps: list = field(default_factory=list)
    parse_warnings: list = field(default_factory=list)
    # Transport outcome, one of: ok | missing | server_error | unreachable |
    # non_text | oversized
    outcome: str = "missing"
    # When `outcome` is "unreachable", which transport failure caused it. Empty
    # for every other outcome. Analyzers use it to separate "the server told us
    # something" from "we never reached the server".
    error_kind: str = ""

    # ---------------------------------------------------------------- queries

    def group_for(self, user_agent: str) -> Optional[Group]:
        """Longest specific user-agent token wins; `*` is the fallback."""
        ua = (user_agent or "").lower()
        best: Optional[Group] = None
        best_len = -1
        fallback: Optional[Group] = None
        for group in self.groups:
            for token in group.agents:
                t = token.lower()
                if t == "*":
                    if fallback is None:
                        fallback = group
                    continue
                if t in ua and len(t) > best_len:
                    best, best_len = group, len(t)
        return best or fallback

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.outcome in ("server_error", "unreachable"):
            # Conservative: an unreadable robots.txt means we stay out.
            return False
        group = self.group_for(user_agent)
        if group is None:
            return True
        path = _path_and_query(url)
        best_len, best_allow = -1, True
        for rule in group.rules:
            length = rule.match_length(path)
            if length > best_len or (length == best_len and rule.allow):
                if length >= 0:
                    best_len, best_allow = length, rule.allow
        return True if best_len < 0 else best_allow

    def crawl_delay_for(self, user_agent: str) -> Optional[float]:
        group = self.group_for(user_agent)
        return group.crawl_delay if group else None

    def blocked_agents(self, catalogue: dict) -> list:
        """Return [(token, reason, evidence_line)] for fully-blocked agents."""
        out = []
        for token, reason in sorted(catalogue.items()):
            group = self.group_for(token)
            if group is None:
                continue
            # Only report an explicit block, never the absence of a group.
            explicit = any(t.lower() == token for g in self.groups for t in g.agents)
            if group.blocks_everything() and (explicit or "*" in [t.lower() for t in group.agents]):
                scope = "explicit group" if explicit else "wildcard (*) group"
                out.append((token, reason, scope))
        return out

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "status": self.status,
            "fetched": self.fetched,
            "outcome": self.outcome,
            "error": self.error,
            "error_kind": self.error_kind,
            "byte_size": self.byte_size,
            "sitemaps": self.sitemaps,
            "parse_warnings": self.parse_warnings[:10],
            "raw_excerpt": self.raw_excerpt,
            "groups": [
                {
                    "agents": g.agents,
                    "crawl_delay": g.crawl_delay,
                    "blocks_everything": g.blocks_everything(),
                    "rules": [
                        {"allow": r.allow, "pattern": r.pattern, "line": r.line_no}
                        for r in g.rules[:40]
                    ],
                    "rule_count": len(g.rules),
                }
                for g in self.groups[:30]
            ],
            "group_count": len(self.groups),
        }


def _path_and_query(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "/"
    path = parts.path or "/"
    try:
        path = unquote(path)
    except Exception:
        pass
    if parts.query:
        path = path + "?" + parts.query
    return path


def parse(text: str, *, url: str = "", status: Optional[int] = None) -> RobotsPolicy:
    """Parse robots.txt content into a `RobotsPolicy`."""
    policy = RobotsPolicy(url=url, status=status, fetched=True, outcome="ok")
    policy.byte_size = len(text.encode("utf-8", "replace"))
    policy.raw_excerpt = text[:1200]

    groups: list = []
    current: Optional[Group] = None
    expecting_agent = False

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            policy.parse_warnings.append("line %d: no field separator" % line_no)
            continue
        field_name, _, value = line.partition(":")
        key = field_name.strip().lower()
        value = value.strip()

        if key in ("user-agent", "useragent"):
            if current is None or not expecting_agent:
                current = Group()
                groups.append(current)
                expecting_agent = True
            current.agents.append(value or "*")
            continue

        if key == "sitemap":
            if value:
                policy.sitemaps.append(value)
            continue

        if key in ("allow", "disallow"):
            expecting_agent = False
            if current is None:
                policy.parse_warnings.append(
                    "line %d: %s outside any user-agent group (ignored)" % (line_no, key)
                )
                continue
            if key == "disallow" and value == "":
                continue  # empty Disallow == allow everything; no rule needed
            if value and not value.startswith("/") and not value.startswith("*"):
                value = "/" + value
            current.rules.append(Rule(allow=(key == "allow"), pattern=value,
                                      line_no=line_no))
            continue

        if key == "crawl-delay":
            expecting_agent = False
            if current is not None:
                try:
                    current.crawl_delay = max(0.0, float(value))
                except ValueError:
                    policy.parse_warnings.append(
                        "line %d: unparseable crawl-delay %r" % (line_no, value)
                    )
            continue

        expecting_agent = False  # any other directive closes the agent list

    policy.groups = [g for g in groups if g.agents]
    return policy


def missing(url: str, status: Optional[int] = 404) -> RobotsPolicy:
    """No robots.txt published - everything is permitted."""
    return RobotsPolicy(url=url, status=status, fetched=False, outcome="missing")


def unreadable(url: str, status: Optional[int], error: str, *, server_error: bool,
               error_kind: str = "") -> RobotsPolicy:
    return RobotsPolicy(
        url=url,
        status=status,
        fetched=False,
        error=error,
        error_kind=error_kind,
        outcome="server_error" if server_error else "unreachable",
    )
