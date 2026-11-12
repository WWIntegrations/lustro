"""Helpers for actors that read official JSON endpoints instead of HTML pages.

Every request still goes through PoliteCrawler.fetch: robots.txt of the API host, per-host
spacing, the SSRF guard on every hop and a byte limit all apply exactly as for pages.
"""
from __future__ import annotations

import json
import re
import time
from html import unescape
from html.parser import HTMLParser

JSON_TYPES = ("application/json", "application/vnd.api+json", "text/json", "application/ld+json")
MAX_JSON_BYTES = 16 * 1024 * 1024
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


def valid_slug(value: str) -> bool:
    """Board tokens, company handles: letters, digits, '.', '_' and '-' only (no path tricks)."""
    return isinstance(value, str) and bool(_SLUG.fullmatch(value)) and ".." not in value


TRANSIENT = ("dns:", "timed out", "timeout", "connection", "reset", "refused")


def get_json(crawler, url: str, max_bytes: int = MAX_JSON_BYTES, retries: int = 2, pause: float = 3.0):
    """Return (data, None) or (None, reason). Network failures (DNS, timeouts, resets) are
    tried again ``retries`` times; HTTP 429/503 pacing is already handled by the crawler."""
    for attempt in range(retries + 1):
        try:
            res = crawler.fetch(url, accept_types=JSON_TYPES, max_bytes=max_bytes)
        except Exception as e:  # noqa: BLE001 - a dropped connection must not end the whole run
            res = None
            reason = f"{type(e).__name__}: {e}"[:300]
        else:
            reason = "" if res.ok else (res.error or ("blocked" if res.blocked else f"HTTP {res.status}"))
        err = (res.error or "").lower() if res is not None else ""
        # a body that missed its deadline would only miss it again: not worth another wait
        transient = res is None or (err and not res.blocked and not err.startswith("timeout: body")
                                    and any(t in err for t in TRANSIENT))
        if not reason or not transient or attempt == retries:
            break
        time.sleep(pause * (attempt + 1))
    if res is None:
        return None, reason
    if res.blocked or res.error:
        return None, res.error or "blocked"
    if not res.ok:
        return None, f"HTTP {res.status}"
    try:
        return json.loads(res.body.decode("utf-8", "replace")), None
    except (ValueError, RecursionError):
        return None, "response is not valid JSON"


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str, limit: int = 20000) -> str:
    """Plain text from an HTML fragment (job descriptions arrive as HTML, sometimes escaped)."""
    if not isinstance(html, str) or not html:
        return ""
    if "&lt;" in html and "<" not in html:
        html = unescape(html)              # Greenhouse sends HTML-escaped HTML
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup from a third party: fall back to raw text
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:limit]
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()[:limit]


def first(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None
