"""An in-process HTTP server that serves synthetic websites to the test suite.

Unit tests must never depend on a live third-party website: results would drift,
runs would be slow, and we would be rate-abusing someone else's server to test
our own politeness. Every behaviour the crawler needs to handle - redirects,
404s, 5xx, noindex headers, robots rules, slow responses, non-HTML bodies,
malformed markup - is reproduced here deterministically.

Usage:

    with FixtureSite(ROUTES) as site:
        snapshot = audit(site.url)
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


class Route:
    """One response definition."""

    def __init__(
        self,
        body: str = "",
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: Optional[dict] = None,
        delay: float = 0.0,
        location: str = "",
    ):
        self.body = body
        self.status = status
        self.content_type = content_type
        self.headers = dict(headers or {})
        self.delay = delay
        if location:
            self.headers["Location"] = location


class FixtureSite:
    """Serves a `{path: Route}` mapping on an ephemeral localhost port."""

    def __init__(self, routes: dict, *, default_status: int = 404,
                 not_found_body: str = "<html><head><title>Not found</title></head>"
                                       "<body><h1>404</h1></body></html>",
                 hook: Optional[Callable] = None):
        self.routes = dict(routes)
        self.default_status = default_status
        self.not_found_body = not_found_body
        self.hook = hook
        self.requests: list = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "FixtureSite":
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output clean
                return

            def _record(self):
                with site._lock:
                    site.requests.append((self.command, self.path,
                                          self.headers.get("User-Agent", "")))

            def _resolve(self):
                if site.hook:
                    forced = site.hook(self.command, self.path)
                    if forced is not None:
                        return forced
                return site.routes.get(self.path)

            def _respond(self, write_body: bool):
                self._record()
                route = self._resolve()
                if route is None:
                    body = site.not_found_body.encode("utf-8")
                    self.send_response(site.default_status)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if write_body:
                        self.wfile.write(body)
                    return
                if route.delay:
                    import time
                    time.sleep(route.delay)
                body = route.body.encode("utf-8")
                self.send_response(route.status)
                if route.content_type:
                    self.send_header("Content-Type", route.content_type)
                for key, value in route.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if write_body:
                    self.wfile.write(body)

            def do_GET(self):
                self._respond(True)

            def do_HEAD(self):
                self._respond(False)

            # Any other verb is a test failure: the auditor must never send one.
            def do_POST(self):
                self._record()
                self.send_error(405, "the auditor must never issue POST")

            do_PUT = do_PATCH = do_DELETE = do_POST

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.timeout = 1
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        return False

    # -- accessors ---------------------------------------------------------

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.port

    def abs(self, path: str) -> str:
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def methods_used(self) -> set:
        with self._lock:
            return {method for method, _, _ in self.requests}

    def paths_requested(self) -> list:
        with self._lock:
            return [path for _, path, _ in self.requests]


# --------------------------------------------------------------------------
# Reusable page bodies. Each one isolates a specific defect (or its absence)
# so a test can assert on exactly one check at a time.
# --------------------------------------------------------------------------

def page(title="Page", *, body="<p>Some ordinary body copy that a reader can follow.</p>",
         head="", lang="en", h1="A heading", canonical=None, extra_head=""):
    canonical_tag = ('<link rel="canonical" href="%s">' % canonical) if canonical else ""
    return (
        '<!doctype html><html lang="%s"><head><meta charset="utf-8">'
        "<title>%s</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "%s%s%s</head><body><header><nav>"
        '<a href="/">Home</a><a href="/about">About</a><a href="/contact">Contact</a>'
        "</nav></header><main><h1>%s</h1>%s</main>"
        "<footer><p>&copy; 2026 Example</p></footer></body></html>"
        % (lang, title, canonical_tag, head, extra_head, h1, body)
    )


RICH_PARAGRAPH = (
    "<p>Northwind Analytics builds a workforce-planning platform for mid-sized "
    "hospitals. The product forecasts nurse demand from historical admissions "
    "and publishes a staffing plan every morning. Teams in twelve countries use "
    "it, and every plan is exportable as CSV.</p>"
    "<p>Pricing starts at 490 US dollars per month for a single facility, and "
    "the platform is billed annually. Support is available on weekdays between "
    "08:00 and 18:00 Central European Time.</p>"
)

ORG_JSONLD = (
    '<script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Organization",'
    '"name":"Northwind Analytics","url":"http://example.test/",'
    '"logo":"http://example.test/logo.png",'
    '"description":"Workforce planning for hospitals",'
    '"sameAs":["https://www.linkedin.com/company/northwind-analytics",'
    '"https://en.wikipedia.org/wiki/Northwind"]}'
    "</script>"
)

BROKEN_JSONLD = (
    '<script type="application/ld+json">{"@context":"https://schema.org",'
    '"@type":"Organization","name":"Northwind",}</script>'
)

CSR_SHELL = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    "<title>Northwind</title>"
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "</head><body><div id=\"root\"></div>"
    '<script>window.__NEXT_DATA__={"props":{"pageProps":{}}}</script>'
    '<script src="/static/app.js"></script></body></html>'
)
