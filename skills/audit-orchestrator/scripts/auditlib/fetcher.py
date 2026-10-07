"""A deliberately small, polite, read-only HTTP client.

Design constraints, all enforced here rather than trusted to callers:

* **Read-only.** Only GET and HEAD are ever issued. There is no code path in
  this module that can emit POST/PUT/PATCH/DELETE, send a body, or attach
  credentials, so no skill in the marketplace can mutate a live site.
* **Polite.** Per-host token bucket (default 2 requests/second) and a hard cap
  on concurrent same-host sockets, plus honouring `Crawl-delay` when the target
  publishes one.
* **Bounded.** Per-response byte cap, per-audit byte cap, per-request timeout,
  at most one retry for transient failures, and a capped redirect chain.
* **Diagnostic.** Redirect hops, timings and transport errors are recorded
  rather than swallowed, because "this URL takes four hops to resolve" is
  itself a finding.
"""

from __future__ import annotations

import copy
import gzip
import re
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass, field
import errno
import os
import selectors
from http import client as httpclient
from http.client import HTTPException
from typing import Optional
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from .urls import normalize, origin

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; BrandAIReadinessAudit/1.0; +https://agentskills.io/) "
    "read-only-site-auditor"
)
ACCEPT_HTML = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5"
)

MAX_BODY_BYTES = 2 * 1024 * 1024        # 2 MB per response
MAX_TOTAL_BYTES = 20 * 1024 * 1024      # 20 MB per audit
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 8.0
DEFAULT_RATE_PER_SEC = 2.0
DEFAULT_HOST_CONCURRENCY = 2
# A site may declare any Crawl-delay it likes. It is honoured as declared up to
# this ceiling; past it no bounded audit could keep the pace, so the crawler
# shrinks its page sample instead of quietly requesting faster than asked.
MAX_HONOURED_CRAWL_DELAY = 60.0
# Time held back for analysis, composition and serialising the report. A polite
# wait is never allowed to eat into it: the request is skipped instead.
RESERVE_AFTER_CRAWL = 60.0
# Waits at or below this are ordinary pacing and are always affordable, whatever
# the budget. Above it, a wait is a site-declared Crawl-delay and must fit.
NORMAL_PACING_GRACE = 2.0

_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""", re.I)
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})

# Failures where **no HTTP response ever came back from the target origin**.
# The distinction matters enormously for reporting: a site that answers 403 has
# told us something about itself, whereas a DNS failure tells us only about the
# machine running the audit. Findings may never be derived from the latter.
TRANSPORT_ERROR_KINDS = frozenset(
    {"dns", "connection_refused", "network_unreachable", "connection", "tls",
     "timeout", "protocol", "budget"})

# Human-readable cause per kind, used verbatim in coverage limitations.
TRANSPORT_CAUSES = {
    "dns": "the hostname could not be resolved (DNS failure)",
    "connection_refused": "the connection was refused",
    "network_unreachable": "the network was unreachable from this machine",
    "connection": "the connection could not be established",
    "tls": "the TLS handshake failed in this environment",
    "timeout": "the request timed out with no response",
    "protocol": "the server spoke an unexpected protocol",
    "budget": "the audit's byte budget was exhausted before the request",
}

_DNS_MARKERS = ("name or service not known", "getaddrinfo", "nodename nor servname",
                "name resolution", "no address associated", "11001", "11004",
                "servname not supported", "unknown host", "-2] name or service",
                "temporary failure in name resolution", "-3] temporary failure")
_REFUSED_MARKERS = ("connection refused", "actively refused", "10061", "errno 111",
                    "wsaeconnrefused")
_UNREACHABLE_MARKERS = ("network is unreachable", "host is unreachable", "10051",
                        "10065", "errno 101", "errno 113", "no route to host",
                        "unreachable network")


def classify_transport_error(reason) -> str:
    """Name the transport failure precisely, so reporting can gate on it.

    Ordering matters: a refused connection and an unreachable network both
    surface as generic OSErrors, and DNS failures are reported differently by
    every platform, so each is matched on its own markers before falling back to
    the generic `connection` bucket.
    """
    text = str(reason).lower()
    if isinstance(reason, socket.timeout) or "timed out" in text or "timeout" in text:
        return "timeout"
    if any(marker in text for marker in _DNS_MARKERS):
        return "dns"
    if any(marker in text for marker in _REFUSED_MARKERS):
        return "connection_refused"
    if any(marker in text for marker in _UNREACHABLE_MARKERS):
        return "network_unreachable"
    if isinstance(reason, ssl.SSLError) or "ssl" in text or "certificate" in text \
            or "handshake" in text:
        return "tls"
    return "connection"


@dataclass
class Hop:
    url: str
    status: int
    location: str = ""


@dataclass
class Response:
    """The outcome of one bounded fetch attempt."""

    url: str                       # URL actually delivered (after redirects)
    requested_url: str
    status: Optional[int] = None
    ok: bool = False
    headers: dict = field(default_factory=dict)
    body: str = ""
    body_bytes: int = 0
    truncated: bool = False
    content_type: str = ""
    encoding: str = ""
    elapsed_ms: int = 0
    redirects: list = field(default_factory=list)   # list[Hop]
    error: str = ""
    error_kind: str = ""           # timeout | dns | tls | connection | http | protocol
    method: str = "GET"
    from_cache: bool = False
    retried: bool = False

    @property
    def is_html(self) -> bool:
        ct = (self.content_type or "").lower()
        return "html" in ct or "xhtml" in ct or (not ct and bool(self.body))

    @property
    def redirect_hops(self) -> int:
        return len(self.redirects)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


class _HostLimiter:
    """Token bucket + concurrency semaphore, per host."""

    def __init__(self, rate: float, concurrency: int):
        self.rate = max(0.1, rate)
        self.min_interval = 1.0 / self.rate
        self.semaphore = threading.Semaphore(max(1, concurrency))
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def acquire(self, max_wait=None) -> bool:
        """Take the next slot, waiting the full declared interval for it.

        Returns False - without waiting and without consuming the slot - when
        the wait would exceed `max_wait`. That is how politeness and the
        deadline are reconciled: the pace is never shortened, so a request that
        cannot be made politely in the time left is simply not made.
        """
        self.semaphore.acquire()
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_slot - now)
            if max_wait is not None and wait > max_wait:
                self.semaphore.release()
                return False
            self.next_slot = max(now, self.next_slot) + self.min_interval
        if wait > 0:
            # Waited in full. Capping the sleep would mean requesting faster than
            # the site asked for, which is exactly what a declared Crawl-delay
            # forbids. The crawl fits the *sample* to the delay instead.
            time.sleep(wait)
        return True

    def release(self) -> None:
        self.semaphore.release()

    def slow_down(self, delay: float) -> None:
        """Honour a site-declared Crawl-delay (or a 429) for later requests.

        The delay is applied as declared, up to `MAX_HONOURED_CRAWL_DELAY`.
        Beyond that the site is asking for a pace no bounded audit can keep, so
        the crawler reduces its page target rather than quietly speeding up.
        """
        with self.lock:
            self.min_interval = max(self.min_interval,
                                    min(delay, MAX_HONOURED_CRAWL_DELAY))


class Fetcher:
    """Thread-safe, budget-aware HTTP client shared by the whole audit."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        host_concurrency: int = DEFAULT_HOST_CONCURRENCY,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        max_body_bytes: int = MAX_BODY_BYTES,
        verify_tls: bool = True,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.rate_per_sec = rate_per_sec
        self.host_concurrency = host_concurrency
        self.max_total_bytes = max_total_bytes
        self.max_body_bytes = max_body_bytes
        self._limiters: dict = {}
        self._limiter_lock = threading.Lock()
        self._bytes_lock = threading.Lock()
        self.total_bytes = 0
        self.request_count = 0
        self.transport_errors = 0
        self._cache: dict = {}
        # Set by the crawl. Politeness is never shortened to meet the deadline;
        # instead a request that cannot be made at the declared pace inside the
        # remaining budget is not made at all.
        self.budget = None
        self.skipped_for_politeness = 0
        self._cache_lock = threading.Lock()
        self._ssl_context = ssl.create_default_context()
        if not verify_tls:
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------- internals

    def _limiter(self, url: str) -> _HostLimiter:
        host = origin(url)
        with self._limiter_lock:
            lim = self._limiters.get(host)
            if lim is None:
                lim = _HostLimiter(self.rate_per_sec, self.host_concurrency)
                self._limiters[host] = lim
            return lim

    def _politeness_allowance(self) -> Optional[float]:
        """How long a request may wait for its polite slot, given the deadline.

        `None` means unbounded, which is correct when no budget is attached (a
        direct caller, or a test). With a budget, enough time is always held
        back for the analysis and report phases.
        """
        budget = self.budget
        if budget is None:
            return None
        # Ordinary rate-limit pacing is a fraction of a second and must always be
        # affordable, or a tight budget would refuse every request and be
        # reported as a transport failure. The gate exists for the long waits a
        # declared Crawl-delay imposes, so it never bites below this floor.
        return max(NORMAL_PACING_GRACE, budget.remaining() - RESERVE_AFTER_CRAWL)

    def set_crawl_delay(self, url: str, delay: Optional[float]) -> None:
        if delay and delay > 0:
            self._limiter(url).slow_down(delay)

    def budget_exhausted(self) -> bool:
        with self._bytes_lock:
            return self.total_bytes >= self.max_total_bytes

    def _account(self, n: int) -> None:
        with self._bytes_lock:
            self.total_bytes += n

    def _decode_body(self, raw: bytes, headers: dict) -> tuple:
        enc = (headers.get("content-encoding") or "").lower()
        try:
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass  # keep the raw bytes; the extractor is tolerant

        charset = ""
        m = _CHARSET_RE.search((headers.get("content-type") or "").encode("ascii", "ignore"))
        if m:
            charset = m.group(1).decode("ascii", "ignore")
        if not charset:
            m = _CHARSET_RE.search(raw[:4096])
            if m:
                charset = m.group(1).decode("ascii", "ignore")
        for candidate in (charset, "utf-8", "cp1252", "latin-1"):
            if not candidate:
                continue
            try:
                return raw.decode(candidate, "replace"), candidate.lower()
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", "replace"), "utf-8"

    def _open(self, url: str, method: str, timeout: float, extra_headers: dict) -> Response:
        """One single HTTP round trip. Never follows redirects itself."""
        started = time.monotonic()
        resp = Response(url=url, requested_url=url, method=method)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": ACCEPT_HTML,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en;q=0.9,*;q=0.5",
            "Connection": "close",
        }
        headers.update(extra_headers or {})
        req = urlrequest.Request(url, headers=headers, method=method)
        opener = urlrequest.build_opener(
            _NoRedirect, _FastHTTPHandler(),
            _FastHTTPSHandler(context=self._ssl_context)
        )
        raw = b""
        try:
            self.request_count += 1
            with opener.open(req, timeout=timeout) as handle:
                resp.status = handle.status
                resp.headers = {k.lower(): v for k, v in handle.headers.items()}
                if method == "GET":
                    remaining = self.max_body_bytes
                    with self._bytes_lock:
                        global_left = self.max_total_bytes - self.total_bytes
                    remaining = max(0, min(remaining, global_left))
                    chunks = []
                    while remaining > 0:
                        chunk = handle.read(min(65536, remaining + 1))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                    if len(raw) > self.max_body_bytes:
                        raw = raw[: self.max_body_bytes]
                        resp.truncated = True
        except HTTPError as exc:  # 4xx/5xx still carry headers and a body
            resp.status = exc.code
            try:
                resp.headers = {k.lower(): v for k, v in exc.headers.items()}
            except Exception:
                resp.headers = {}
            try:
                raw = exc.read(self.max_body_bytes) if method == "GET" else b""
            except Exception:
                raw = b""
            resp.error_kind = "http"
        except socket.timeout:
            resp.error, resp.error_kind = "request timed out after %.1fs" % timeout, "timeout"
            self.transport_errors += 1
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            resp.error, resp.error_kind = str(reason)[:300], classify_transport_error(reason)
            self.transport_errors += 1
        except (HTTPException, ConnectionError) as exc:
            resp.error, resp.error_kind = "%s: %s" % (type(exc).__name__, exc), "protocol"
            self.transport_errors += 1
        except Exception as exc:  # never let a transport quirk kill the audit
            resp.error, resp.error_kind = "%s: %s" % (type(exc).__name__, exc), "connection"
            self.transport_errors += 1

        if raw:
            self._account(len(raw))
            resp.body_bytes = len(raw)
            resp.body, resp.encoding = self._decode_body(raw, resp.headers)
        resp.content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        resp.elapsed_ms = round((time.monotonic() - started) * 1000)
        resp.ok = resp.status is not None and 200 <= resp.status < 300
        return resp

    # ----------------------------------------------------------------- public

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        timeout: Optional[float] = None,
        allow_retry: bool = True,
        use_cache: bool = True,
        extra_headers: Optional[dict] = None,
    ) -> Response:
        """Fetch `url`, following redirects manually and recording the chain."""
        if method not in ("GET", "HEAD"):
            raise ValueError("Fetcher is read-only: only GET and HEAD are permitted")
        url = normalize(url)
        if not url:
            r = Response(url=url, requested_url=url, method=method)
            r.error, r.error_kind = "unparseable URL", "connection"
            return r

        cache_key = (method, url)
        if use_cache:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if cached is not None:
                clone = copy.copy(cached)
                clone.redirects = list(cached.redirects)
                clone.headers = dict(cached.headers)
                clone.from_cache = True
                return clone

        if self.budget_exhausted():
            r = Response(url=url, requested_url=url, method=method)
            r.error, r.error_kind = "audit byte budget exhausted", "budget"
            return r

        timeout = timeout or self.timeout
        current = url
        hops: list = []
        response: Optional[Response] = None

        for _ in range(MAX_REDIRECTS + 1):
            limiter = self._limiter(current)
            if not limiter.acquire(max_wait=self._politeness_allowance()):
                # The pace the site asked for cannot be kept in the time left, so
                # the request is dropped rather than hurried.
                self.skipped_for_politeness += 1
                skipped = Response(url=current, requested_url=url, method=method)
                skipped.redirects = list(hops)
                skipped.error_kind = "budget"
                skipped.error = ("the site's declared crawl delay could not be "
                                 "honoured within the remaining runtime budget")
                return skipped
            try:
                response = self._open(current, method, timeout, extra_headers or {})
                transient = response.status in _TRANSIENT_STATUSES or (
                    response.status is None
                    and response.error_kind in ("timeout", "connection", "protocol")
                )
                if allow_retry and transient:
                    # Exactly one retry, after a short pause, for transient trouble.
                    time.sleep(0.6)
                    retried = self._open(current, method, timeout, extra_headers or {})
                    settled = retried.status is not None and retried.status not in _TRANSIENT_STATUSES
                    if retried.ok or settled or response.status is None:
                        response = retried
                    response.retried = True
            finally:
                limiter.release()

            if response.status in (301, 302, 303, 307, 308):
                location = response.header("location")
                nxt = normalize(_join(current, location)) if location else ""
                hops.append(Hop(url=current, status=response.status, location=nxt or location))
                if not nxt or nxt == current or any(h.url == nxt for h in hops):
                    response.error = response.error or "redirect loop or unresolvable Location"
                    response.error_kind = response.error_kind or "protocol"
                    break
                current = nxt
                continue
            break

        assert response is not None
        response.redirects = hops
        response.requested_url = url
        response.url = current
        if use_cache:
            with self._cache_lock:
                self._cache[cache_key] = response
        return response

    def head_or_get(self, url: str, *, timeout: Optional[float] = None) -> Response:
        """Cheap liveness probe: HEAD first, GET only if HEAD is unsupported.

        Many servers answer HEAD with 405/501 or lie about it, so a non-2xx,
        non-4xx-client answer is re-checked with a ranged GET before we call a
        link broken. That single extra request removes the most common source
        of false "broken link" reports.
        """
        head = self.fetch(url, method="HEAD", timeout=timeout)
        if head.ok:
            return head
        if head.status in (403, 405, 501, 400, 429) or head.status is None:
            got = self.fetch(url, method="GET", timeout=timeout,
                             extra_headers={"Range": "bytes=0-4095"})
            got.method = "GET"
            return got
        return head

    def stats(self) -> dict:
        return {
            "requests": self.request_count,
            "bytes": self.total_bytes,
            "transport_errors": self.transport_errors,
            "hosts": len(self._limiters),
        }


class _AddressCache:
    """Remembers which resolved address actually connects for each host.

    A machine whose IPv6 path is black-holed - routine in containers, CI runners
    and offices with partial dual-stack - resolves AAAA records that never
    connect. `socket.create_connection` walks the resolver's order, so every
    single request pays a full connect timeout before falling back to IPv4. On a
    20-page crawl that is the difference between 30 seconds and four minutes,
    and none of it is the audited site's fault.

    The first connection to a host tries each address with a short cap instead
    of the whole request timeout, and the *address family* that answered is
    remembered and tried first from then on.

    Deliberately the family and not the individual address: a large site puts
    many edge IPs behind one name, and pinning to whichever answered first would
    concentrate the whole crawl on a single node - which reads as a traffic
    spike to that node and gets throttled. Remembering only the family skips the
    dead path while still spreading requests the way the resolver intended.
    Ordering stays deterministic: the resolver's order is preserved within the
    promoted family.
    """

    def __init__(self):
        self._working = {}
        self._lock = threading.Lock()

    def preferred(self, host):
        with self._lock:
            return self._working.get(host)

    def remember(self, host, family):
        with self._lock:
            self._working[host] = family

    def forget(self, host):
        with self._lock:
            self._working.pop(host, None)


_ADDRESS_CACHE = _AddressCache()
CONNECT_STAGGER = 0.25            # RFC 8305 calls this the Connection Attempt Delay
# A non-blocking connect that has not finished yet reports one of these rather
# than failing. Windows uses WSAEWOULDBLOCK where POSIX uses EINPROGRESS.
_CONNECT_IN_PROGRESS = frozenset(
    code for code in (getattr(errno, name, None)
                      for name in ("EINPROGRESS", "EWOULDBLOCK", "EAGAIN",
                                   "EALREADY", "WSAEWOULDBLOCK", "WSAEINPROGRESS"))
    if code is not None) | {10035, 10036}


def _ordered_addresses(infos, preferred):
    """Resolver order, families interleaved, the known-good family first.

    Interleaving is what stops a run of dead addresses in one family from
    delaying the other family's first attempt.
    """
    families = []
    for info in infos:
        if info[0] not in families:
            families.append(info[0])
    if preferred in families:
        families.remove(preferred)
        families.insert(0, preferred)
    buckets = [[i for i in infos if i[0] == fam] for fam in families]
    ordered = []
    for row in range(max((len(b) for b in buckets), default=0)):
        for bucket in buckets:
            if row < len(bucket):
                ordered.append(bucket[row])
    return ordered


def _create_connection(address, timeout=None, source_address=None):
    """Connect to `address`, racing the resolved records against each other.

    A drop-in replacement for `socket.create_connection` with the same
    signature, so it can be installed on an `http.client` connection class.

    `socket.create_connection` walks the resolver's addresses strictly in order,
    waiting for each to time out. On a host whose IPv6 path is black-holed that
    costs the full connect timeout on *every request*, which is the difference
    between a 40-second audit and a four-minute one - and none of it is the
    audited site's fault.

    This follows RFC 8305 ("Happy Eyeballs") instead: attempts are started a
    short delay apart and run concurrently, and the first socket to complete
    wins while the rest are closed. A dead path costs nothing but an unused
    socket, so the first request is as fast as the healthy family.
    """
    host, port = address[0], address[1]
    if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = DEFAULT_TIMEOUT
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not infos:
        raise socket.gaierror("no address records for %s" % host)

    ordered = _ordered_addresses(infos, _ADDRESS_CACHE.preferred(host))
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    pending, last_error, index, next_launch = {}, None, 0, 0.0

    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if index < len(ordered) and now >= next_launch:
                family, socktype, proto, _canon, sockaddr = ordered[index]
                index += 1
                next_launch = now + CONNECT_STAGGER
                try:
                    sock = socket.socket(family, socktype, proto)
                    sock.setblocking(False)
                    if source_address:
                        sock.bind(source_address)
                    err = sock.connect_ex(sockaddr)
                    if err in (0,):
                        sock.setblocking(True)
                        sock.settimeout(timeout)
                        _ADDRESS_CACHE.remember(host, family)
                        return sock
                    if err not in _CONNECT_IN_PROGRESS:
                        last_error = OSError(err, os.strerror(err))
                        sock.close()
                    else:
                        selector.register(sock, selectors.EVENT_WRITE,
                                          (family, sockaddr))
                        pending[sock] = family
                except OSError as exc:
                    last_error = exc

            if not pending and index >= len(ordered):
                break

            # Wake up either when something connects or when the next attempt is
            # due, whichever comes first.
            wait = min(deadline, next_launch if index < len(ordered) else deadline)
            wait = max(0.0, wait - time.monotonic())
            for key, _events in selector.select(timeout=wait or 0.05):
                sock = key.fileobj
                family, _sockaddr = key.data
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                selector.unregister(sock)
                pending.pop(sock, None)
                if code == 0:
                    sock.setblocking(True)
                    # Hand back a socket carrying the caller's timeout, not the
                    # race's, or the TLS handshake and body read inherit it.
                    sock.settimeout(timeout)
                    _ADDRESS_CACHE.remember(host, family)
                    return sock
                last_error = OSError(code, os.strerror(code))
                sock.close()
    finally:
        for sock in list(pending):
            try:
                selector.unregister(sock)
            except (KeyError, ValueError):
                pass
            sock.close()
        selector.close()

    raise last_error if last_error else socket.timeout(
        "no address for %s accepted a connection within %.1fs" % (host, timeout))


class _FastHTTPConnection(httpclient.HTTPConnection):
    # CPython binds `_create_connection` as an *instance* attribute in
    # HTTPConnection.__init__, so a class attribute here would be shadowed. It
    # has to be replaced on the instance, after the base class has set it.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _FastHTTPSConnection(httpclient.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _FastHTTPHandler(urlrequest.HTTPHandler):
    def http_open(self, req):  # noqa: D102
        return self.do_open(_FastHTTPConnection, req)


class _FastHTTPSHandler(urlrequest.HTTPSHandler):
    def https_open(self, req):  # noqa: D102
        return self.do_open(_FastHTTPSConnection, req, context=self._context)


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Surface redirects to the caller instead of following them silently."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: D102
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def _join(base: str, location: str) -> str:
    from urllib.parse import urljoin

    try:
        return urljoin(base, (location or "").strip())
    except Exception:
        return ""
