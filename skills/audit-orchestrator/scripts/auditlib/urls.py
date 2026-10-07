"""Deterministic URL normalisation, classification and crawl-trap avoidance.

Every URL that enters the crawl frontier passes through `normalize()`. Two URLs
that normalise to the same string are treated as the same page, which is what
makes page selection, finding IDs and de-duplication reproducible across runs.
"""

from __future__ import annotations

import re
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urldefrag,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

# Query parameters that never change the rendered document. Dropping them
# collapses tracking permutations onto one canonical crawl target.
TRACKING_PARAMS = frozenset(
    {
        "gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "twclid",
        "igshid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "hsctatracking",
        "vero_id", "vero_conv", "yclid", "ttclid", "li_fat_id", "epik",
        "ref_src", "ref_url", "s_kwcid", "trk", "trkcampaign", "cmpid",
        "campaignid", "adgroupid", "gad_source", "gad_campaignid", "srsltid",
        "spm", "scid", "sc_cid", "wickedid", "irclickid", "at_medium",
    }
)
TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "matomo_", "hsa_", "_ga", "_gl")

# Path fragments that indicate a state-changing, authenticated or infinite space.
# These are never fetched: the audit is strictly read-only and must not wander
# into calendars, faceted explosions or logout endpoints.
UNSAFE_PATH_TOKENS = (
    "/logout", "/signout", "/sign-out", "/log-out", "/login", "/signin",
    "/sign-in", "/register", "/signup", "/sign-up", "/checkout", "/cart",
    "/basket", "/account", "/admin", "/wp-admin", "/wp-login", "/my-account",
    "/password", "/reset", "/unsubscribe", "/delete", "/remove", "/edit",
    "/preferences", "/subscribe", "/oauth", "/sso", "/xmlrpc.php", "/cgi-bin/",
)
UNSAFE_QUERY_KEYS = frozenset(
    {"add-to-cart", "remove_item", "logout", "action", "do", "delete", "confirm"}
)
# Infinite / low-value crawl spaces keyed by query parameter.
TRAP_QUERY_KEYS = frozenset(
    {"replytocom", "share", "print", "outputtype", "calendar", "ical",
     "add_to_wishlist", "orderby", "filter", "filters", "facet", "sort_by"}
)

NON_HTML_EXTENSIONS = frozenset(
    {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico",
        ".bmp", ".tiff", ".mp4", ".webm", ".mov", ".avi", ".mkv", ".mp3",
        ".wav", ".ogg", ".flac", ".m4a", ".zip", ".gz", ".tar", ".bz2", ".7z",
        ".rar", ".dmg", ".exe", ".msi", ".pkg", ".deb", ".rpm", ".apk",
        ".woff", ".woff2", ".ttf", ".otf", ".eot", ".css", ".js", ".mjs",
        ".map", ".wasm", ".psd", ".ai", ".eps", ".iso", ".bin",
    }
)
# Documents we acknowledge but do not download (they can hold facts, which is a
# finding in itself, but fetching them burns budget for little parseable value).
DOCUMENT_EXTENSIONS = frozenset(
    {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".rtf"}
)

DEFAULT_PORTS = {"http": "80", "https": "443"}
_INDEX_FILE_RE = re.compile(r"/(index|default|home)\.(html?|php|aspx?|jsp)$", re.I)
_SAFE_PATH_CHARS = "/-._~!$&()*+,;=:@%" + "'"


def _safe_quote(path: str) -> str:
    """Percent-encode a path idempotently (decode once, re-encode known-safe)."""
    try:
        return quote(unquote(path), safe=_SAFE_PATH_CHARS)
    except Exception:
        return path


def normalize(url: str, *, drop_index: bool = True) -> str:
    """Return a canonical, comparable form of `url`.

    Deterministic transformations only: lowercase scheme/host, strip default
    ports, drop the fragment, remove tracking parameters, sort the remaining
    query pairs, collapse duplicate slashes and normalise the trailing slash.
    """
    if not url:
        return ""
    url = url.strip()
    url, _ = urldefrag(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    scheme = (parts.scheme or "").lower()
    try:
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return ""
    if not scheme or not host:
        return ""
    netloc = host
    if port and str(port) != DEFAULT_PORTS.get(scheme, ""):
        netloc = "%s:%d" % (host, port)

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if drop_index:
        path = _INDEX_FILE_RE.sub("/", path)
    if not path.startswith("/"):
        path = "/" + path
    # The trailing slash is deliberately preserved. `/a` and `/a/` are different
    # URLs to a server, and a great many sites 301 from one to the other; folding
    # them together here made every such redirect look like a loop, so those
    # sites appeared to be entirely broken. Frontier de-duplication uses
    # `canonical_key()` instead, which is slash-insensitive without lying about
    # which URL to request.
    path = _safe_quote(path)

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_param(k)
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def canonical_key(url: str) -> str:
    """A slash-insensitive identity for a URL, for de-duplication only.

    `/pricing` and `/pricing/` are almost always the same page, so the crawl
    frontier should not spend two of its twenty slots on them. But the *request*
    must still use the exact URL the site published, which is why this is a
    separate function rather than part of `normalize()`.
    """
    normalized = normalize(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_PARAMS or any(k.startswith(p) for p in TRACKING_PREFIXES)


def resolve(base: str, href: str) -> str:
    """Join `href` against `base` and normalise, rejecting non-HTTP schemes."""
    if not href:
        return ""
    href = href.strip()
    low = href.lower()
    if low.startswith(("javascript:", "mailto:", "tel:", "data:", "sms:", "ftp:",
                       "file:", "about:", "callto:", "whatsapp:", "#")):
        return ""
    try:
        joined = urljoin(base, href)
    except Exception:
        return ""
    if not joined.lower().startswith(("http://", "https://")):
        return ""
    return normalize(joined)


def registrable_host(url: str) -> str:
    """Host without a leading `www.` - the unit we treat as the same site."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def same_site(a: str, b: str) -> bool:
    """True when two URLs belong to the same registrable host (www-insensitive).

    Subdomains are deliberately treated as different sites: auditing
    `blog.example.com` from `example.com` would silently change the crawl scope
    the operator asked for.
    """
    return bool(registrable_host(a)) and registrable_host(a) == registrable_host(b)


def path_of(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "/"


def extension(url: str) -> str:
    path = path_of(url)
    dot = path.rfind(".")
    slash = path.rfind("/")
    return path[dot:].lower() if dot > slash else ""


def is_document(url: str) -> bool:
    return extension(url) in DOCUMENT_EXTENSIONS


def is_probably_html(url: str) -> bool:
    ext = extension(url)
    return ext not in NON_HTML_EXTENSIONS and ext not in DOCUMENT_EXTENSIONS


def is_unsafe(url: str) -> bool:
    """Reject anything that could mutate state or require authentication."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    path = (parts.path or "/").lower()
    if any(token in path for token in UNSAFE_PATH_TOKENS):
        return True
    keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    return bool(keys & UNSAFE_QUERY_KEYS)


def trap_score(url: str) -> int:
    """Heuristic 0..3 for 'this URL leads into an unbounded space'.

    Higher is worse. Used only to de-prioritise the frontier - never on its own
    to raise a finding.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return 3
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    keys = {k.lower() for k, _ in pairs}
    score = 0
    if keys & TRAP_QUERY_KEYS:
        score += 2
    if len(pairs) >= 3:
        score += 1
    depth = len([s for s in (parts.path or "").split("/") if s])
    if depth >= 6:
        score += 1
    # Pagination beyond the first couple of pages adds nothing to a sample.
    m = re.search(r"/(?:page|p)/(\d+)", parts.path or "", re.I)
    if m and int(m.group(1)) > 2:
        score += 2
    for k, v in pairs:
        if k.lower() in ("page", "paged", "p", "start", "offset") and v.isdigit() and int(v) > 2:
            score += 2
            break
    if re.search(r"/(19|20)\d{2}/\d{1,2}/\d{1,2}/", parts.path or ""):
        score += 1  # dated archive listing
    return min(score, 3)


def depth_of(url: str, root: str) -> int:
    """Path-segment distance from the audited root."""
    root_segs = [s for s in path_of(root).split("/") if s]
    segs = [s for s in path_of(url).split("/") if s]
    if segs[: len(root_segs)] == root_segs:
        return len(segs) - len(root_segs)
    return len(segs)


def origin(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return "%s://%s" % (parts.scheme, parts.netloc)


def coerce_input_url(value: str) -> str:
    """Accept `example.com`, `//example.com`, `http://example.com/path`.

    Returns "" when the input cannot be turned into an absolute HTTP(S) URL.
    """
    v = (value or "").strip().strip('"').strip("'")
    if not v or any(ch.isspace() for ch in v):
        return ""
    if v.startswith("//"):
        v = "https:" + v
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", v, re.I):
        v = "https://" + v
    if not v.lower().startswith(("http://", "https://")):
        return ""
    try:
        parts = urlsplit(v)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return ""
    if not host:
        return ""
    if "." not in host and host not in ("localhost", "127.0.0.1", "::1"):
        return ""
    return normalize(v)
