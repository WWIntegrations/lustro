"""PoliteCrawler: the only way Lustro actors fetch pages.

Per request it enforces, in this order: scheme + SSRF check (on every redirect hop),
robots.txt for the hop's origin (cached), a per-host minimum interval (raised by
robots Crawl-delay), retries with backoff on 429/5xx that honour Retry-After, a
content-type filter and a byte limit. There is no switch to skip robots.txt.
"""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from .compliance import (MAX_CRAWL_DELAY, MIN_INTERVAL_FLOOR, PoliteSession, RobotsCache,
                         SSRFBlocked, check_target, is_loopback_host, origin_of,
                         parse_retry_after, pin_session)
from .storage import MemoryRequestQueue

HTML_TYPES = ("text/html", "application/xhtml+xml")
RETRY_CODES = (429, 500, 502, 503, 504)
REDIRECT_CODES = (301, 302, 303, 307, 308)
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip",
             ".gz", ".tar", ".mp4", ".mp3", ".webm", ".mov", ".avi", ".woff", ".woff2",
             ".ttf", ".css", ".js", ".json", ".xml", ".dmg", ".exe")


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    headers: dict = field(default_factory=dict)
    set_cookies: list = field(default_factory=list)
    body: bytes = b""
    content_type: str = ""
    redirects: list = field(default_factory=list)
    error: str = ""
    blocked: bool = False      # refused by robots.txt or the SSRF guard (never fetched)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return self.content_type in HTML_TYPES

    def text(self) -> str:
        charset = "utf-8"
        ct = self.headers.get("content-type", "") if self.headers else ""
        for part in ct.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                charset = v.strip('"\' ')
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")


def _lower_headers(resp) -> dict:
    return {k.lower(): v for k, v in resp.headers.items()}


def _cookie_flags(resp) -> list:
    """Set-Cookie names + attribute flags only; cookie values are never kept."""
    raw = getattr(resp, "raw", None)
    values = []
    try:
        values = raw.headers.getlist("Set-Cookie") if raw is not None else []
    except AttributeError:
        v = resp.headers.get("Set-Cookie")
        values = [v] if v else []
    out = []
    for v in values:
        parts = [p.strip() for p in v.split(";")]
        name = parts[0].split("=", 1)[0][:100] if parts else ""
        attrs = {p.split("=", 1)[0].lower(): (p.split("=", 1)[1] if "=" in p else True)
                 for p in parts[1:] if p}
        out.append({"name": name, "secure": "secure" in attrs, "httponly": "httponly" in attrs,
                    "samesite": str(attrs.get("samesite", "")) or None})
    return out


class PoliteCrawler:
    def __init__(self, ctx=None, *, session=None, min_interval: float = 1.0,
                 allow_loopback: bool = None, max_retries: int = 3, backoff_base: float = 1.0,
                 max_backoff: float = 30.0, max_bytes: int = 5 * 1024 * 1024,
                 timeout: float = 20, max_redirects: int = 10, log=None):
        self.ctx = ctx
        self.session = session or (ctx.session if ctx else PoliteSession(MIN_INTERVAL_FLOOR))
        self.allow_loopback = (ctx.allow_loopback if ctx and allow_loopback is None
                               else bool(allow_loopback))
        pin_session(self.session, self.allow_loopback)  # connect only to validated IPs
        self.min_interval = float(min_interval)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.log = log or (ctx.log if ctx else None)
        self.stats = ctx.stats if ctx else {"requests": 0, "retries": 0, "robotsBlocked": 0,
                                            "ssrfBlocked": 0, "failed": 0, "pages": 0,
                                            "bytes": 0, "items": 0}
        self._hosts_seen: set = set()
        self.robots = RobotsCache(self.session, allow_loopback=self.allow_loopback,
                                  on_fetch=self._host_setup_origin)

    # -- politeness -------------------------------------------------------------------
    def _effective_interval(self, host: str) -> float:
        if self.allow_loopback and is_loopback_host(host):
            return max(0.0, self.min_interval)
        return max(MIN_INTERVAL_FLOOR, self.min_interval)

    def _host_setup_origin(self, origin: str) -> None:
        self._host_setup(urlparse(origin).hostname or "")

    def _host_setup(self, host: str) -> None:
        if host in self._hosts_seen:
            return
        self._hosts_seen.add(host)
        self.session.limiter.set_interval(host, self._effective_interval(host))

    def _apply_crawl_delay(self, url: str, entry) -> None:
        delay = entry.crawl_delay()
        if delay:
            host = urlparse(url).hostname or ""
            cur = self.session.limiter.interval(host)
            self.session.limiter.set_interval(host, max(cur, min(delay, MAX_CRAWL_DELAY)))

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.ctx is not None and self.ctx.aborted:
                return
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))

    def _request(self, method: str, url: str):
        """(response | None, error). Retries 429/5xx and network errors."""
        last_err = ""
        for attempt in range(self.max_retries + 1):
            self.stats["requests"] += 1
            try:
                resp = self.session.request(method, url, stream=True, allow_redirects=False,
                                            timeout=self.timeout)
            except SSRFBlocked as e:
                self.stats["ssrfBlocked"] += 1
                return None, f"ssrf: {e}"
            except requests.RequestException as e:
                last_err = f"network: {type(e).__name__}"
                if attempt < self.max_retries:
                    self.stats["retries"] += 1
                    self._sleep(min(self.max_backoff, self.backoff_base * 2 ** attempt))
                    continue
                return None, last_err
            if resp.status_code in RETRY_CODES and attempt < self.max_retries:
                wait = parse_retry_after(resp.headers.get("Retry-After"))
                if wait is not None and wait > self.max_backoff:
                    return resp, ""  # asked to come back later than we wait: give up politely
                if wait is None:
                    wait = min(self.max_backoff, self.backoff_base * 2 ** attempt)
                resp.close()
                self.stats["retries"] += 1
                if self.log:
                    self.log.info(f"HTTP {resp.status_code} {url}: retry in {wait:.1f}s")
                self._sleep(wait)
                continue
            return resp, ""
        return None, last_err or "retries exhausted"

    # -- fetch ------------------------------------------------------------------------
    def fetch(self, url: str, accept_types=HTML_TYPES, read_body: bool = True,
              method: str = "GET", max_bytes: int = None) -> FetchResult:
        """Fetch url following redirects manually (each hop SSRF- and robots-checked).
        accept_types=None accepts any content-type."""
        res = FetchResult(url=url)
        limit = self.max_bytes if max_bytes is None else max_bytes
        current = url
        t0 = time.monotonic()
        for _ in range(self.max_redirects + 1):
            reason = check_target(current, self.allow_loopback)
            if reason:
                res.final_url, res.error = current, f"ssrf: {reason}"
                res.blocked = reason != "unresolvable host"
                if reason == "unresolvable host":
                    res.error = "dns: unresolvable host"
                else:
                    self.stats["ssrfBlocked"] += 1
                return res
            self._host_setup(urlparse(current).hostname or "")
            allowed, why = self.robots.allowed(current)
            if not allowed:
                self.stats["robotsBlocked"] += 1
                res.final_url, res.error, res.blocked = current, f"robots: {why}", True
                return res
            self._apply_crawl_delay(current, self.robots.get(current))
            resp, err = self._request(method, current)
            if resp is None:
                res.final_url, res.error = current, err
                res.blocked = err.startswith("ssrf:")
                return res
            try:
                res.status = resp.status_code
                res.final_url = current
                if resp.status_code in REDIRECT_CODES and resp.headers.get("Location"):
                    nxt = urljoin(current, resp.headers["Location"])
                    res.redirects.append({"url": current, "status": resp.status_code,
                                          "location": nxt})
                    current = nxt
                    continue
                res.headers = _lower_headers(resp)
                res.set_cookies = _cookie_flags(resp)
                res.content_type = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
                if accept_types is not None and res.content_type not in accept_types:
                    res.error = f"content-type: {res.content_type or 'none'}"
                    return res
                if read_body and method != "HEAD":
                    clen = res.headers.get("content-length", "")
                    if clen.isdigit() and int(clen) > limit:
                        res.error = f"too-large: {clen} bytes"
                        return res
                    buf = bytearray()
                    for chunk in resp.iter_content(64 * 1024):
                        buf += chunk
                        if len(buf) > limit:
                            res.error = f"too-large: > {limit} bytes"
                            return res
                    res.body = bytes(buf)
                    self.stats["bytes"] += len(buf)
                return res
            finally:
                resp.close()
                res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        res.error = "too-many-redirects"
        return res

    # -- crawl ------------------------------------------------------------------------
    def crawl(self, start_urls, on_page, *, max_pages: int = 50, max_depth: int = 2,
              same_origin: bool = True, include=None, exclude=None, queue=None,
              enqueue_links: bool = True):
        """Breadth-first crawl through a (durable) request queue.

        on_page(result, request, doc) is called for every processed request; doc is the
        parsed page (lustro.extract.Page) for successful HTML responses, else None.
        Links are followed only within scope (same origin by default, include/exclude
        globs on the full URL), up to max_depth, and never past max_pages in total."""
        from .extract import parse_html

        queue = queue or (self.ctx.queue if self.ctx else MemoryRequestQueue())
        origins = {origin_of(u) for u in start_urls}
        for u in start_urls:
            queue.add(urldefrag(u)[0], depth=0)
        processed = queue.handled_count
        while processed < max_pages:
            if self.ctx is not None:
                self.ctx.check_abort()
            req = queue.fetch_next()
            if req is None:
                break
            try:
                res = self.fetch(req["url"])
            except Exception:
                queue.reclaim(req)
                raise
            doc = None
            if res.ok and res.is_html:
                doc = parse_html(res.text(), res.final_url or req["url"])
                self.stats["pages"] += 1
            if req.get("depth", 0) == 0 and res.final_url:
                origins.add(origin_of(res.final_url))
            on_page(res, req, doc)
            queue.mark_handled(req, error=res.error)
            processed += 1
            if (enqueue_links and doc is not None and req.get("depth", 0) < max_depth
                    and not doc.nofollow and "nofollow" not in res.headers.get("x-robots-tag", "")):
                for link in doc.links:
                    target = urldefrag(link["url"])[0]
                    if self.in_scope(target, origins, same_origin, include, exclude):
                        queue.add(target, depth=req.get("depth", 0) + 1)
        return processed

    @staticmethod
    def in_scope(url, origins, same_origin=True, include=None, exclude=None) -> bool:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        if p.path.lower().endswith(_SKIP_EXT):
            return False
        if same_origin and origin_of(url) not in origins:
            return False
        if include and not any(fnmatch.fnmatchcase(url, g) for g in include):
            return False
        if exclude and any(fnmatch.fnmatchcase(url, g) for g in exclude):
            return False
        return True
