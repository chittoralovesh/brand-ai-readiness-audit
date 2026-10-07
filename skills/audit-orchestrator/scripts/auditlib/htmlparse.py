"""One tolerant HTML pass that produces everything the analyzers need.

Parsing happens exactly once per page, in the orchestrator, and the *structured*
result is what travels to the analyzer skills. That has three consequences worth
stating plainly:

* analyzer skills never see raw HTML, so they cannot drift apart on parsing
  details and they stay cheap to run;
* the snapshot is small enough to hand to a language model as context;
* every extracted field is computed with the same definition of "visible text",
  "main content" and "navigation", so cross-skill findings compare like for like.

The extractor is built on `html.parser` (standard library) and is deliberately
forgiving: real pages are malformed, and a parse error must degrade a single
field, never the audit.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Tags whose text content is never shown to a reader.
INVISIBLE_TAGS = frozenset({"script", "style", "noscript", "template", "svg",
                            "canvas", "head", "title", "iframe", "object"})
# Elements whose descendants may legitimately contain their own <title> that is
# not the document title: SVG and MathML use it as an accessible name, and a
# <template> holds markup that was never rendered. Icon systems put one in every
# glyph, so a page can carry dozens without having more than one real title.
EMBEDDED_TITLE_HOSTS = ("svg", "math", "template")
# Tags that force a line break when flattening text.
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "nav", "aside", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "tr", "td", "th", "blockquote", "pre", "figure", "figcaption",
    "form", "fieldset", "hr", "br", "address", "details", "summary", "output",
})
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"})
LANDMARK_TAGS = ("main", "nav", "header", "footer", "article", "aside", "section",
                 "figure", "address", "time", "table", "form")

# Framework bootstrap markers. Presence alone proves nothing (many frameworks
# render on the server); they are only ever used together with a low-text signal.
CSR_ROOT_IDS = frozenset({"root", "app", "__next", "__nuxt", "___gatsby",
                          "svelte", "q-app", "app-root", "main-app"})
CSR_ATTR_MARKERS = ("data-reactroot", "data-react-helmet", "ng-app", "ng-version",
                    "data-server-rendered", "data-svelte", "v-cloak", "wire:id",
                    "data-turbo-root", "data-astro-cid")
CSR_SCRIPT_MARKERS = ("__NEXT_DATA__", "__NUXT__", "__remixContext",
                      "__staticRouterHydrationData", "window.__INITIAL_STATE__",
                      "window.__APOLLO_STATE__", "__SVELTEKIT", "self.__next_f")

GENERIC_LINK_TEXT = frozenset({
    "click here", "here", "read more", "learn more", "more", "more info",
    "more information", "this", "this page", "link", "continue", "go", "details",
    "see more", "view more", "find out more", "discover more", "explore",
    "submit", "click", "download", "start", "next", "previous", "info",
})
# Words that signal an intent-bearing call to action in any vertical.
CTA_TOKENS = (
    "start", "get started", "sign up", "book", "buy", "order", "request",
    "contact", "demo", "quote", "apply", "join", "subscribe", "try", "schedule",
    "call", "shop", "donate", "register", "enroll", "reserve", "download",
    "talk to", "get a", "free trial", "add to", "checkout", "consult", "enquire",
    "inquire", "appointment", "estimate", "pricing", "hire", "install",
)

_WS_RE = re.compile(r"[ \t\r\f\v ​]+")
_NEWLINES_RE = re.compile(r"\n{2,}")
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]+|\S[^.!?\n]*$")


def collapse(text: str) -> str:
    text = _WS_RE.sub(" ", text or "")
    text = _NEWLINES_RE.sub("\n", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


class Extraction:
    """Structured view of one HTML document. Plain attributes, JSON-ready."""

    def __init__(self):
        self.title = None                # str | None  (None == element absent)
        self.title_count = 0
        self.html_lang = ""
        self.meta_name = {}              # name -> content (first wins)
        self.meta_property = {}          # property -> content
        self.meta_http_equiv = {}
        self.meta_duplicates = []        # names seen more than once
        self.links = []                  # <link rel=...> descriptors
        self.canonical = ""
        self.canonical_count = 0
        self.hreflang = []
        self.headings = []               # {level, text, order}
        self.anchors = []
        self.images = []
        self.scripts = []
        self.jsonld_blocks = []          # raw strings
        self.inline_script_bytes = 0
        self.inline_style_bytes = 0
        self.iframes = []
        self.forms = []
        self.buttons = []
        self.landmark_counts = {t: 0 for t in LANDMARK_TAGS}
        self.microdata_types = []
        self.rdfa_types = []
        self.time_elements = []          # {datetime, text}
        self.csr_markers = []
        self.empty_root_containers = []
        self.noscript_text = ""
        self.text = ""
        self.main_text = ""
        self.nav_text = ""
        self.first_screen_text = ""
        self.paragraphs = []
        self.list_items = 0
        self.tables = 0
        self.table_headers = 0           # <th> cells: what binds a value to a column
        self.table_captions = 0
        self.has_skip_link = False
        self.media_counts = {"video": 0, "audio": 0, "picture": 0, "canvas": 0,
                             "object": 0, "embed": 0}
        self.parse_error = ""
        self.raw_bytes = 0

    # -- derived -----------------------------------------------------------

    @property
    def word_count(self) -> int:
        return word_count(self.text)

    @property
    def main_word_count(self) -> int:
        return word_count(self.main_text)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "title_count": self.title_count,
            "html_lang": self.html_lang,
            "meta_name": self.meta_name,
            "meta_property": self.meta_property,
            "meta_http_equiv": self.meta_http_equiv,
            "meta_duplicates": self.meta_duplicates,
            "canonical": self.canonical,
            "canonical_count": self.canonical_count,
            "hreflang": self.hreflang[:25],
            "links": self.links[:40],
            "headings": self.headings[:120],
            "anchors": self.anchors,
            "images": self.images[:150],
            "scripts": self.scripts[:60],
            "script_count": len(self.scripts),
            "jsonld_blocks": self.jsonld_blocks[:25],
            "inline_script_bytes": self.inline_script_bytes,
            "inline_style_bytes": self.inline_style_bytes,
            "iframes": self.iframes[:20],
            "forms": self.forms[:15],
            "buttons": self.buttons[:60],
            "landmark_counts": self.landmark_counts,
            "microdata_types": self.microdata_types[:30],
            "rdfa_types": self.rdfa_types[:30],
            "time_elements": self.time_elements[:40],
            "csr_markers": sorted(set(self.csr_markers))[:12],
            "empty_root_containers": self.empty_root_containers[:8],
            "noscript_text_len": len(self.noscript_text),
            "text_excerpt": self.text[:3000],
            "nav_text_excerpt": self.nav_text[:1500],
            "main_text_excerpt": self.main_text[:3000],
            "first_screen_text": self.first_screen_text[:900],
            "word_count": self.word_count,
            "main_word_count": self.main_word_count,
            "paragraph_count": len(self.paragraphs),
            "long_paragraphs": sum(1 for p in self.paragraphs if word_count(p) > 120),
            "list_items": self.list_items,
            "tables": self.tables,
            "table_headers": self.table_headers,
            "table_captions": self.table_captions,
            "has_skip_link": self.has_skip_link,
            "media_counts": self.media_counts,
            "parse_error": self.parse_error,
        }


class _Extractor(HTMLParser):
    def __init__(self, ex: Extraction, *, has_main_landmark: bool = False):
        super().__init__(convert_charrefs=True)
        self.ex = ex
        self._root_watch = []
        self.stack = []                       # open tag names
        self.buffers = {"all": [], "main": [], "nav": [], "noscript": []}
        self._collecting_jsonld = False
        self._jsonld_parts = []
        self._script_attrs = {}
        self._inline_script_parts = []
        self._heading = None
        self._heading_parts = []
        self._anchor = None
        self._anchor_parts = []
        self._button = None
        self._button_parts = []
        self._para_parts = []
        self._form = None
        self._seen_meta_names = {}
        # Decided up-front by a cheap pre-scan so that text appearing *before*
        # the <main> element is classified consistently with text after it.
        self._has_main_landmark = has_main_landmark

    # -- helpers -----------------------------------------------------------

    def _in(self, *tags) -> bool:
        return any(t in self.stack for t in tags)

    def _region(self) -> str:
        """Where the current text sits: main content, chrome, or neither."""
        if self._in("nav") or self._in("footer") or self._in("header") or self._in("aside"):
            return "nav"
        if self._has_main_landmark:
            return "main" if self._in("main", "article") else "other"
        return "main"

    def _emit_text(self, data: str):
        if not data:
            return
        if self._in(*INVISIBLE_TAGS) and not self._in("noscript"):
            return
        if self._in("noscript"):
            self.buffers["noscript"].append(data)
            return
        self.buffers["all"].append(data)
        region = self._region()
        if region == "main":
            self.buffers["main"].append(data)
        elif region == "nav":
            self.buffers["nav"].append(data)
        if self._heading is not None:
            self._heading_parts.append(data)
        if self._anchor is not None:
            self._anchor_parts.append(data)
        if self._button is not None:
            self._button_parts.append(data)
        self._para_parts.append(data)

    def _flush_paragraph(self):
        text = collapse("".join(self._para_parts))
        if len(text) >= 40:
            self.ex.paragraphs.append(text)
        self._para_parts = []

    # -- HTMLParser API ----------------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {}
        for k, v in attrs:
            k = (k or "").lower()
            if k not in a:
                a[k] = v if v is not None else ""
        ex = self.ex

        if tag in BLOCK_TAGS:
            self._flush_paragraph()
            self.buffers["all"].append("\n")
            self.buffers["main"].append("\n")

        if tag in ex.landmark_counts:
            ex.landmark_counts[tag] += 1

        # Structured-data attribute vocabularies (guards against a false
        # "no structured data" verdict on microdata/RDFa sites).
        if "itemtype" in a and a.get("itemtype"):
            ex.microdata_types.append(a["itemtype"])
        if "typeof" in a and a.get("typeof"):
            ex.rdfa_types.append(a["typeof"])
        for marker in CSR_ATTR_MARKERS:
            if marker in a:
                ex.csr_markers.append(marker)

        if tag == "body":
            # Real parsers close an unterminated <title>/<head> at <body>.
            # Without this, one missing </title> swallows the whole document.
            for orphan in ("title", "head", "style", "script"):
                if orphan in self.stack:
                    while self.stack and self.stack[-1] != orphan:
                        self.stack.pop()
                    if self.stack:
                        self.stack.pop()

        if tag == "html":
            ex.html_lang = (a.get("lang") or "").strip()
        elif tag == "title" and not self._in(*EMBEDDED_TITLE_HOSTS):
            ex.title_count += 1
            if ex.title is None:
                ex.title = ""
        elif tag == "meta":
            self._handle_meta(a)
        elif tag == "link":
            self._handle_link(a)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = int(tag[1])
            self._heading_parts = []
        elif tag == "a":
            self._anchor = a
            self._anchor_parts = []
        elif tag == "button":
            self._button = a
            self._button_parts = []
            # A <button> with no type defaults to submit inside a form.
            if self._form is not None and (a.get("type") or "submit").lower() == "submit":
                self._form["has_submit"] = True
        elif tag == "img":
            ex.images.append({
                "src": (a.get("src") or a.get("data-src") or "")[:400],
                "alt": a.get("alt"),           # None means the attribute is absent
                "title": a.get("title"),
                "width": a.get("width"),
                "height": a.get("height"),
                "loading": a.get("loading"),
                "region": self._region(),
                "in_link": self._anchor is not None,
                "aria_hidden": a.get("aria-hidden") == "true",
                "role": a.get("role"),
            })
        elif tag == "script":
            self._script_attrs = a
            stype = (a.get("type") or "").lower().strip()
            src = a.get("src") or ""
            ex.scripts.append({
                "src": src[:400],
                "type": stype,
                "async": "async" in a,
                "defer": "defer" in a,
                "module": stype == "module",
                "inline": not src,
            })
            if stype in ("application/ld+json", "application/json+ld"):
                self._collecting_jsonld = True
                self._jsonld_parts = []
            elif not src:
                self._inline_script_parts = []
        elif tag == "iframe":
            ex.iframes.append({"src": (a.get("src") or "")[:300],
                               "title": a.get("title"),
                               "region": self._region()})
        elif tag == "form":
            self._form = {
                "action": (a.get("action") or "")[:300],
                "method": (a.get("method") or "get").lower(),
                "name": a.get("name") or a.get("id") or "",
                "inputs": [],
                "labels": 0,
                "has_submit": False,
                "region": self._region(),
            }
        elif tag in ("input", "select", "textarea"):
            itype = (a.get("type") or ("text" if tag == "input" else tag)).lower()
            if self._form is not None:
                self._form["inputs"].append({
                    "type": itype,
                    "name": a.get("name") or a.get("id") or "",
                    "required": "required" in a,
                    "labelled": bool(a.get("aria-label") or a.get("aria-labelledby")
                                     or a.get("title")),
                    "placeholder": bool(a.get("placeholder")),
                })
                if itype in ("submit", "image"):
                    self._form["has_submit"] = True
            if itype in ("submit", "button") and a.get("value"):
                ex.buttons.append({"text": collapse(a.get("value"))[:120],
                                   "region": self._region(), "kind": "input"})
        elif tag == "label":
            if self._form is not None:
                self._form["labels"] += 1
        elif tag == "time":
            ex.time_elements.append({"datetime": (a.get("datetime") or "")[:60],
                                     "text": "", "region": self._region()})
        elif tag in ("video", "audio", "picture", "canvas", "object", "embed"):
            ex.media_counts[tag] = ex.media_counts.get(tag, 0) + 1
        elif tag == "table":
            ex.tables += 1
        elif tag == "th":
            ex.table_headers += 1
        elif tag == "caption":
            ex.table_captions += 1
        elif tag == "li":
            ex.list_items += 1

        # Framework mount point. We record how much *text* it actually holds in
        # the server response; an empty one is the strongest single signal that
        # the page only exists after JavaScript runs.
        ident = (a.get("id") or "").lower()
        if tag in ("div", "section", "main") and ident in CSR_ROOT_IDS:
            self._root_watch.append({
                "tag": tag, "id": ident, "depth": len(self.stack),
                "buffer_start": len(self.buffers["all"]), "descendants": 0,
            })

        for watch in self._root_watch:
            if len(self.stack) > watch["depth"]:
                watch["descendants"] += 1

        if tag not in VOID_TAGS:
            self.stack.append(tag)
        elif tag == "br":
            self._emit_text(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS and self.stack and self.stack[-1] == tag.lower():
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        ex = self.ex

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._heading is not None:
            text = collapse("".join(self._heading_parts))
            ex.headings.append({"level": self._heading, "text": text[:300],
                                "order": len(ex.headings), "region": self._region()})
            self._heading = None
            self._heading_parts = []
        elif tag == "a" and self._anchor is not None:
            self._close_anchor()
        elif tag == "button" and self._button is not None:
            ex.buttons.append({"text": collapse("".join(self._button_parts))[:120],
                               "region": self._region(), "kind": "button"})
            self._button = None
            self._button_parts = []
        elif tag == "script":
            if self._collecting_jsonld:
                raw = "".join(self._jsonld_parts).strip()
                if raw:
                    ex.jsonld_blocks.append(raw[:60000])
                self._collecting_jsonld = False
                self._jsonld_parts = []
            elif self._inline_script_parts:
                blob = "".join(self._inline_script_parts)
                ex.inline_script_bytes += len(blob)
                for marker in CSR_SCRIPT_MARKERS:
                    if marker in blob:
                        ex.csr_markers.append(marker)
                self._inline_script_parts = []
        elif tag == "form" and self._form is not None:
            ex.forms.append(self._form)
            self._form = None
        elif tag == "time" and ex.time_elements:
            last = ex.time_elements[-1]
            if not last["text"]:
                last["text"] = collapse("".join(self._para_parts[-6:]))[:120]

        if tag in BLOCK_TAGS:
            self._flush_paragraph()
            self.buffers["all"].append("\n")
            self.buffers["main"].append("\n")

        # Pop to the matching open tag (tolerates unclosed elements).
        if tag in self.stack:
            while self.stack:
                popped = self.stack.pop()
                if popped == tag:
                    break

        # Close any mount-point watch that this end tag terminated.
        still_open = []
        for watch in self._root_watch:
            if len(self.stack) <= watch["depth"]:
                inner = collapse("".join(self.buffers["all"][watch["buffer_start"]:]))
                ex.empty_root_containers.append({
                    "tag": watch["tag"], "id": watch["id"],
                    "descendants": watch["descendants"],
                    "inner_text_chars": len(inner),
                })
            else:
                still_open.append(watch)
        self._root_watch = still_open

    def _close_anchor(self):
        a = self._anchor or {}
        text = collapse("".join(self._anchor_parts))
        rel = (a.get("rel") or "").lower()
        href = (a.get("href") or "").strip()
        aria = (a.get("aria-label") or a.get("title") or "").strip()
        label = text or aria
        cls = (a.get("class") or "").lower()
        self.ex.anchors.append({
            "href": href[:500],
            "text": label[:200],
            "text_source": "text" if text else ("aria" if aria else "none"),
            "rel": rel[:100],
            "region": self._region(),
            "button_like": ("btn" in cls or "button" in cls
                            or a.get("role") == "button"),
            "target": a.get("target") or "",
        })
        if href.startswith("#") and "skip" in (label or cls).lower():
            self.ex.has_skip_link = True
        self._anchor = None
        self._anchor_parts = []

    def handle_data(self, data):
        if self._collecting_jsonld:
            self._jsonld_parts.append(data)
            return
        if self._in("script"):
            self._inline_script_parts.append(data)
            return
        if self._in("style"):
            self.ex.inline_style_bytes += len(data)
            return
        if "title" in self.stack:
            # An inline <svg><title> is an accessible name for an icon, not the
            # document title. Counting it corrupts the title an assistant would
            # cite, and icon systems emit dozens per page.
            if self.ex.title is not None and not self._in(*EMBEDDED_TITLE_HOSTS):
                self.ex.title = (self.ex.title + data)[:600]
            return
        self._emit_text(data)

    def handle_comment(self, data):
        return

    def error(self, message):  # Python <3.10 compatibility hook
        return

    def _handle_meta(self, a: dict):
        ex = self.ex
        content = (a.get("content") or "").strip()
        if "charset" in a:
            ex.meta_http_equiv["charset"] = a.get("charset", "")
        name = (a.get("name") or "").strip().lower()
        prop = (a.get("property") or "").strip().lower()
        equiv = (a.get("http-equiv") or "").strip().lower()
        if name:
            self._seen_meta_names[name] = self._seen_meta_names.get(name, 0) + 1
            if self._seen_meta_names[name] == 2:
                ex.meta_duplicates.append(name)
            if name not in ex.meta_name:
                ex.meta_name[name] = content[:1000]
        if prop and prop not in ex.meta_property:
            ex.meta_property[prop] = content[:1000]
        if equiv and equiv not in ex.meta_http_equiv:
            ex.meta_http_equiv[equiv] = content[:300]
        # Per-bot robots directives, e.g. <meta name="googlebot" content="noindex">
        if name in ("robots", "googlebot", "bingbot", "google", "ai", "chatgpt-user"):
            ex.meta_name.setdefault(name, content[:1000])

    def _handle_link(self, a: dict):
        ex = self.ex
        rel = (a.get("rel") or "").strip().lower()
        href = (a.get("href") or "").strip()
        entry = {"rel": rel, "href": href[:400], "type": a.get("type") or "",
                 "hreflang": a.get("hreflang") or "", "title": a.get("title") or ""}
        ex.links.append(entry)
        rels = set(rel.split())
        if "canonical" in rels:
            ex.canonical_count += 1
            if not ex.canonical:
                ex.canonical = href
        if "alternate" in rels and a.get("hreflang"):
            ex.hreflang.append({"hreflang": a.get("hreflang"), "href": href[:300]})


_MAIN_LANDMARK_RE = re.compile(r"<(?:main|article)[\s>]", re.I)


def extract(html: str, *, raw_bytes: int = 0) -> Extraction:
    """Parse `html` into an `Extraction`. Never raises."""
    ex = Extraction()
    html = html or ""
    ex.raw_bytes = raw_bytes or len(html.encode("utf-8", "replace"))
    parser = _Extractor(ex, has_main_landmark=bool(_MAIN_LANDMARK_RE.search(html)))
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as exc:                      # malformed beyond recovery
        ex.parse_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    try:
        parser._flush_paragraph()
        if parser._anchor is not None:
            parser._close_anchor()
    except Exception:
        pass

    ex.text = collapse("".join(parser.buffers["all"]))
    ex.main_text = collapse("".join(parser.buffers["main"]))
    ex.nav_text = collapse("".join(parser.buffers["nav"]))
    ex.noscript_text = collapse("".join(parser.buffers["noscript"]))
    if not ex.main_text:
        ex.main_text = ex.text
    ex.first_screen_text = ex.main_text[:900]
    if ex.title is not None:
        ex.title = collapse(ex.title)
    # Mount points still open at EOF (unclosed markup) still carry a verdict.
    for watch in parser._root_watch:
        inner = collapse("".join(parser.buffers["all"][watch["buffer_start"]:]))
        ex.empty_root_containers.append({
            "tag": watch["tag"], "id": watch["id"],
            "descendants": watch["descendants"],
            "inner_text_chars": len(inner),
        })
    return ex


# --------------------------------------------------------------------------
# Small shared text utilities used by several analyzers via the snapshot.
# --------------------------------------------------------------------------

def sentences(text: str, limit: int = 40) -> list:
    return [collapse(s) for s in _SENTENCE_RE.findall(text or "")[:limit]]


def is_generic_link_text(text: str) -> bool:
    t = collapse(text or "").lower().strip(" .,:;!?->→»")
    return t in GENERIC_LINK_TEXT or len(t) <= 2


def looks_like_cta(text: str) -> bool:
    t = collapse(text or "").lower()
    if not t or len(t) > 60:
        return False
    return any(tok in t for tok in CTA_TOKENS)
