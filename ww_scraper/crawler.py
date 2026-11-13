"""PoliteCrawler: the only way ww-scraper actors fetch pages.

Per request it enforces, in this order: scheme + SSRF check (on every redirect hop),
robots.txt for the hop's origin (cached), a per-host minimum interval (raised by
robots Crawl-delay), retries with backoff on 429/5xx that honour Retry-After, a
content-type filter and a byte limit. There is no switch to skip robots.txt.

Pacing adapts per host: a 429 or 503 doubles that host's interval for the rest of the
run (up to MAX_ADAPTIVE_INTERVAL), and every SPEED_UP_AFTER plain successes ease it back
by a quarter. It never drops below the configured interval or the robots Crawl-delay,
so a server that struggles is asked less often, and one that recovers is not punished.
"""
from __future__ import annotations

import concurrent.futures
from email.utils import parsedate_to_datetime
import fnmatch
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from .compliance import (MAX_CRAWL_DELAY, MIN_INTERVAL_FLOOR, PoliteSession, RobotsCache,
                         SSRFBlocked, check_target, host_allowed, is_loopback_host, origin_of,
                         network_reason, parse_retry_after, pin_session)
from .storage import MemoryRequestQueue

HTML_TYPES = ("text/html", "application/xhtml+xml")
RETRY_CODES = (429, 500, 502, 503, 504)
# The server says it is overloaded: slow down for this host, not only for this request.
THROTTLE_CODES = (429, 503)
MAX_ADAPTIVE_INTERVAL = 60.0
ADAPTIVE_FIRST_STEP = 0.5
SPEED_UP_AFTER = 10
REDIRECT_CODES = (301, 302, 303, 307, 308)
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip",
             ".gz", ".tar", ".mp4", ".mp3", ".webm", ".mov", ".avi", ".woff", ".woff2",
             ".ttf", ".css", ".js", ".json", ".xml", ".dmg", ".exe")


def machine_load():
    """(1-minute load average per CPU, share of memory available or None). Memory is read
    from /proc/meminfo where it exists (Linux); elsewhere only the load is known."""
    try:
        load = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
    except OSError:
        load = 0.0
    free = None
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            info = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in fh if ":" in line)}
        free = info["MemAvailable"] / info["MemTotal"]
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    return load, free


class LoadScaler:
    """Concurrency that follows the machine: starts at 2 (or 1) and, every `interval`
    seconds, adds a worker while the load per CPU is under `low` and memory is fine, and
    drops one when the load is over `high` or less than `min_free` of memory is available.
    It never goes above the run's maxConcurrency, and per-host spacing is not affected:
    more workers only help a crawl that spans several hosts."""

    def __init__(self, maximum: int, *, low=0.6, high=0.9, min_free=0.1, interval=2.0, probe=machine_load):
        self.maximum = max(1, int(maximum))
        self.value = min(2, self.maximum)
        self.low, self.high, self.min_free, self.interval, self.probe = low, high, min_free, interval, probe
        self._next = 0.0
        self.changes = 0

    def current(self) -> int:
        now = time.monotonic()
        if now < self._next:
            return self.value
        self._next = now + self.interval
        load, free = self.probe()
        tight = free is not None and free < self.min_free
        if (load > self.high or tight) and self.value > 1:
            self.value -= 1
            self.changes += 1
        elif load < self.low and not tight and self.value < self.maximum:
            self.value += 1
            self.changes += 1
        return self.value


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    not_modified: bool = False   # 304 to a conditional request: body unchanged, not read
    headers: dict = field(default_factory=dict)
    set_cookies: list = field(default_factory=list)
    body: bytes = b""
    content_type: str = ""
    redirects: list = field(default_factory=list)
    error: str = ""
    blocked: bool = False      # refused by robots.txt or the SSRF guard (never fetched)
    elapsed_ms: int = 0
    rendered: bool = False     # body is the DOM after scripts ran (UTF-8)
    duplicate: bool = False    # a redirect led to a page the crawl already has (not fetched)
    artifacts: dict = field(default_factory=dict)   # {"screenshotKey", "pdfKey"} in the run's KV store

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return self.content_type in HTML_TYPES

    def text(self) -> str:
        if self.rendered:
            return self.body.decode("utf-8", "replace")
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


MAX_VALIDATOR = 200


def conditional_headers(validator) -> dict:
    """If-None-Match / If-Modified-Since from a stored {"etag", "lastModified"}.

    The values came from a server earlier and are sent back to it, so anything that is not
    a short single line of printable ASCII (or not an HTTP date) is dropped, not sent."""
    out = {}
    if not isinstance(validator, dict):
        return out
    etag, modified = validator.get("etag"), validator.get("lastModified")
    if (isinstance(etag, str) and 0 < len(etag) <= MAX_VALIDATOR
            and all(" " <= c <= "~" for c in etag)):
        out["If-None-Match"] = etag
    if isinstance(modified, str) and 0 < len(modified) <= MAX_VALIDATOR:
        try:
            parsedate_to_datetime(modified)
        except (TypeError, ValueError, IndexError):
            pass
        else:
            if all(" " <= c <= "~" for c in modified):
                out["If-Modified-Since"] = modified
    return out


def validator_of(headers) -> dict:
    """{"etag", "lastModified"} worth keeping from a response, or {}."""
    kept = conditional_headers({"etag": (headers or {}).get("etag"),
                                "lastModified": (headers or {}).get("last-modified")})
    return {k: v for k, v in (("etag", kept.get("If-None-Match")),
                              ("lastModified", kept.get("If-Modified-Since"))) if v}


class PoliteCrawler:
    def __init__(self, ctx=None, *, session=None, min_interval: float = 1.0,
                 allow_loopback: bool = None, max_retries: int = 3, backoff_base: float = 1.0,
                 max_backoff: float = 30.0, max_bytes: int = 5 * 1024 * 1024,
                 timeout: float = 20, max_redirects: int = 10, log=None,
                 body_timeout: float = None):
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
        self.body_timeout = float(body_timeout) if body_timeout else max(60.0, 3 * float(timeout))
        self.max_redirects = max_redirects
        self.log = log or (ctx.log if ctx else None)
        self.stats = ctx.stats if ctx else {"requests": 0, "retries": 0, "robotsBlocked": 0,
                                            "ssrfBlocked": 0, "failed": 0, "pages": 0,
                                            "bytes": 0, "items": 0}
        self._hosts_seen: set = set()
        self._stats_lock = threading.Lock()
        # Adaptive pacing: host -> the interval it may never go below, and its success streak.
        self.adaptive = True
        self.dedupe = True           # crawl(): skip URLs leading to a page already crawled
        self._base: dict = {}
        self._streak: dict = {}
        self._pace_lock = threading.Lock()
        self.default_concurrency = 1
        self.autoscale = False       # True: concurrency follows the machine's load (LoadScaler)
        self.render = False          # True: HTML pages are re-read through headless Chrome;
        #                              "auto": only pages that look script-built (see needs_render)
        self.render_wait_ms = 3000
        self.accept_language = ""    # passed to the browser too, so a render keeps the language
        # waitForSelector, actions, screenshot ("viewport"/"fullPage"), format, pdf: any of them
        # switches rendering to the DevTools pipe (render.render_page)
        self.browser: dict = {}
        self._auto: dict = {}         # host -> [pages rendered by "auto", renders that helped]
        self._auto_lock = threading.Lock()
        # url -> {"etag", "lastModified"}: GETs of these URLs are conditional, a 304 is "unchanged"
        self.validators: dict = {}
        # url -> links seen the last time: followed when the page answers 304 (no body to parse)
        self.known_links: dict = {}
        self.robots = RobotsCache(self.session, allow_loopback=self.allow_loopback,
                                  on_fetch=self._host_setup_origin)

    def _inc(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

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
        base = self._effective_interval(host)
        with self._pace_lock:
            self._base[host] = base
        self.session.limiter.set_interval(host, base)

    def _apply_crawl_delay(self, url: str, entry) -> None:
        delay = entry.crawl_delay()
        if delay:
            host = urlparse(url).hostname or ""
            delay = min(delay, MAX_CRAWL_DELAY)
            with self._pace_lock:
                self._base[host] = max(self._base.get(host, 0.0), delay)
                cur = self.session.limiter.interval(host)
                self.session.limiter.set_interval(host, max(cur, delay))

    def _slow_down(self, host: str) -> None:
        """The host answered 429/503: double its interval for the rest of the run."""
        if not self.adaptive:
            return
        with self._pace_lock:
            cur = self.session.limiter.interval(host)
            new = min(MAX_ADAPTIVE_INTERVAL, max(cur * 2, cur + ADAPTIVE_FIRST_STEP))
            self._streak[host] = 0
            if new > cur:
                self.session.limiter.set_interval(host, new)
        if new > cur:
            self._inc("throttled")
            if self.log:
                self.log.info(f"{host} is overloaded: interval {cur:.2f}s -> {new:.2f}s")

    def _speed_up(self, host: str) -> None:
        """A plain success: after a streak, ease the interval back toward its floor."""
        if not self.adaptive:
            return
        with self._pace_lock:
            base = self._base.get(host, self._effective_interval(host))
            cur = self.session.limiter.interval(host)
            if cur <= base:
                self._streak[host] = 0
                return
            self._streak[host] = self._streak.get(host, 0) + 1
            if self._streak[host] < SPEED_UP_AFTER:
                return
            self._streak[host] = 0
            new = max(base, cur * 0.75)
            self.session.limiter.set_interval(host, new if new - base > 0.01 else base)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.ctx is not None and self.ctx.aborted:
                return
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))

    def _request(self, method: str, url: str, headers=None):
        """(response | None, error). Retries 429/5xx and network errors."""
        last_err = ""
        host = urlparse(url).hostname or ""
        for attempt in range(self.max_retries + 1):
            self._inc("requests")
            try:
                resp = self.session.request(method, url, stream=True, allow_redirects=False,
                                            timeout=self.timeout, headers=headers)
            except SSRFBlocked as e:
                self._inc("ssrfBlocked")
                return None, f"ssrf: {e}"
            except requests.RequestException as e:
                last_err = f"network: {network_reason(e)}"
                if attempt < self.max_retries:
                    self._inc("retries")
                    self._sleep(min(self.max_backoff, self.backoff_base * 2 ** attempt))
                    continue
                return None, last_err
            if resp.status_code in THROTTLE_CODES:
                self._slow_down(host)
            elif resp.status_code < 400:
                self._speed_up(host)
            if resp.status_code in RETRY_CODES and attempt < self.max_retries:
                wait = parse_retry_after(resp.headers.get("Retry-After"))
                if wait is not None and wait > self.max_backoff:
                    return resp, ""  # asked to come back later than we wait: give up politely
                if wait is None:
                    wait = min(self.max_backoff, self.backoff_base * 2 ** attempt)
                resp.close()
                self._inc("retries")
                if self.log:
                    self.log.info(f"HTTP {resp.status_code} {url}: retry in {wait:.1f}s")
                self._sleep(wait)
                continue
            return resp, ""
        return None, last_err or "retries exhausted"

    # -- fetch ------------------------------------------------------------------------
    def fetch(self, url: str, accept_types=HTML_TYPES, read_body: bool = True,
              method: str = "GET", max_bytes: int = None, skip_target=None) -> FetchResult:
        """Fetch url following redirects manually (each hop SSRF- and robots-checked).
        accept_types=None accepts any content-type. skip_target(url) -> True stops before
        following a redirect to url (the crawl already has that page)."""
        res = FetchResult(url=url)
        limit = self.max_bytes if max_bytes is None else max_bytes
        current = url
        t0 = time.monotonic()
        allow_hosts = getattr(self.ctx, "allow_hosts", None)
        for _ in range(self.max_redirects + 1):
            if allow_hosts:
                try:
                    host = (urlparse(current).hostname or "").lower()
                except ValueError:
                    host = ""
                if not host_allowed(host, allow_hosts):
                    # checked on every hop, before any DNS lookup or robots.txt request
                    res.final_url, res.error, res.blocked = current, f"host not allowed: {host or '?'}", True
                    self._inc("hostBlocked")
                    return res
            reason = check_target(current, self.allow_loopback)
            if reason:
                res.final_url, res.error = current, f"ssrf: {reason}"
                res.blocked = reason != "unresolvable host"
                if reason == "unresolvable host":
                    res.error = "dns: unresolvable host"
                else:
                    self._inc("ssrfBlocked")
                return res
            self._host_setup(urlparse(current).hostname or "")
            allowed, why = self.robots.allowed(current)
            if not allowed:
                if self.robots.get(current).status == "network-error":
                    # robots.txt could not be read: a failure to retry later, not a refusal
                    self._inc("robotsUnread")
                    res.final_url, res.error = current, f"network: {why}"
                    return res
                self._inc("robotsBlocked")
                if origin_of(current) != origin_of(url):
                    # say whose robots.txt it was: after a redirect to another site (a shop
                    # moved to a social profile) it is not the start site's own rule
                    why = f"{why} of {origin_of(current)} after a redirect from {url}"
                res.final_url, res.error, res.blocked = current, f"robots: {why}", True
                return res
            self._apply_crawl_delay(current, self.robots.get(current))
            extra = (conditional_headers(self.validators.get(url))
                     if method == "GET" and current == url and self.validators else None)
            resp, err = self._request(method, current, extra or None)
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
                    if skip_target is not None and skip_target(nxt):
                        res.final_url, res.duplicate = nxt, True
                        res.error = "duplicate: redirects to a page already crawled"
                        self._inc("duplicates")
                        return res
                    current = nxt
                    continue
                res.headers = _lower_headers(resp)
                res.set_cookies = _cookie_flags(resp)
                if resp.status_code == 304 and extra:
                    res.not_modified = True
                    self._inc("notModified")
                    return res
                res.content_type = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
                if accept_types is not None and res.content_type not in accept_types:
                    res.error = f"content-type: {res.content_type or 'none'}"
                    if resp.status_code >= 400:          # the status is the news, not the type
                        res.error = f"HTTP {resp.status_code} ({res.error})"
                    return res
                if read_body and method != "HEAD":
                    clen = res.headers.get("content-length", "")
                    if clen.isdigit() and int(clen) > limit:
                        res.error = f"too-large: {clen} bytes"
                        return res
                    buf = bytearray()
                    # `timeout` bounds each socket read; a server that keeps sending a little at
                    # a time never trips it, so the whole body also has a deadline
                    body_deadline = time.monotonic() + self.body_timeout
                    for chunk in resp.iter_content(16 * 1024):
                        buf += chunk
                        if len(buf) > limit:
                            res.error = f"too-large: > {limit} bytes"
                            return res
                        if time.monotonic() > body_deadline:
                            res.error = f"timeout: body not complete after {self.body_timeout:.0f} s"
                            return res
                    res.body = bytes(buf)
                    self._inc("bytes", len(buf))
                    if self.render and res.ok and res.is_html and self._should_render(res):
                        self._render_into(res)
                return res
            finally:
                resp.close()
                res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        res.error = "too-many-redirects"
        return res

    AUTO_PROBE = 3          # "auto": renders per host before deciding whether they help

    def _should_render(self, res) -> bool:
        """True for render=True. For "auto", only pages whose raw HTML looks script-built,
        and a host stops being rendered after AUTO_PROBE renders that added no text."""
        if self.render is not True and self.render != "auto":
            return False
        if self.render is True:
            return True
        from .render import needs_render
        host = urlparse(res.final_url or res.url).hostname or ""
        with self._auto_lock:
            tried, helped = self._auto.get(host, (0, 0))
        if tried >= self.AUTO_PROBE and not helped:
            self._inc("renderSkipped")
            return False
        if not needs_render(res.text()):
            self._inc("renderSkipped")
            return False
        return True

    def _render_into(self, res) -> None:
        """Replace the body with the DOM after scripts ran. The page URL already passed
        the SSRF and robots.txt gates above; the browser's own requests go through the
        filtering proxy (ww_scraper/proxy.py), which refuses private addresses."""
        import hashlib

        from .render import RenderError, render_dom, render_page, visible_words
        host = urlparse(res.final_url).hostname or ""
        self.session.limiter.wait(host)          # the render is a second visit: space it
        self._inc("requests")
        before = visible_words(res.text()) if self.render == "auto" else 0
        b = self.browser or {}
        shot_fmt = b.get("format") or "png"
        try:
            if any(b.get(k) for k in ("waitForSelector", "actions", "screenshot", "pdf")):
                out = render_page(res.final_url, allow_loopback=self.allow_loopback,
                                  wait_ms=self.render_wait_ms, max_bytes=self.max_bytes,
                                  wait_for=b.get("waitForSelector") or None, actions=b.get("actions") or None,
                                  screenshot=b.get("screenshot") or None, image_format=shot_fmt,
                                  pdf=bool(b.get("pdf")),
                                  accept_language=self.accept_language,
                                  allow_hosts=getattr(self.ctx, "allow_hosts", None))
                html = out.html
                for note in out.notes:
                    if self.log:
                        self.log.info(f"render {res.final_url}: {note}")
                if self.ctx is not None and (out.screenshot or out.pdf):
                    stem = hashlib.sha256(res.final_url.encode("utf-8")).hexdigest()[:16]
                    if out.screenshot:
                        key = f"screenshot-{stem}.{'jpg' if shot_fmt == 'jpeg' else 'png'}"
                        self.ctx.kv.set(key, out.screenshot, f"image/{shot_fmt}")
                        res.artifacts["screenshotKey"] = key
                        self._inc("screenshots")
                    if out.pdf:
                        key = f"page-{stem}.pdf"
                        self.ctx.kv.set(key, out.pdf, "application/pdf")
                        res.artifacts["pdfKey"] = key
            else:
                html = render_dom(res.final_url, allow_loopback=self.allow_loopback,
                                  wait_ms=self.render_wait_ms, max_bytes=self.max_bytes,
                                  accept_language=self.accept_language,
                                  allow_hosts=getattr(self.ctx, "allow_hosts", None))
        except RenderError as e:
            self._inc("renderFailed")
            if self.log:
                self.log.warning(f"render failed for {res.final_url}: {e}; using the raw HTML")
            return
        if self.render == "auto":
            with self._auto_lock:
                tried, helped = self._auto.get(host, (0, 0))
                self._auto[host] = (tried + 1, helped + (visible_words(html) > before + 20))
        res.body = html.encode("utf-8")
        res.rendered = True
        self._inc("rendered")

    # -- crawl ------------------------------------------------------------------------
    def crawl(self, start_urls, on_page, *, max_pages: int = 50, max_depth: int = 2,
              same_origin: bool = True, include=None, exclude=None, queue=None,
              enqueue_links: bool = True, concurrency: int = None, dedupe: bool = None):
        """Breadth-first crawl through a (durable) request queue.

        on_page(result, request, doc) is called for every processed request; doc is the
        parsed page (ww_scraper.extract.Page) for successful HTML responses, else None.
        Links are followed only within scope (same origin by default, include/exclude
        globs on the full URL), up to max_depth, and never past max_pages in total.
        concurrency > 1 fetches several pages at once; requests to one host stay spaced
        by the rate limiter, so it only helps when the crawl spans several hosts or the
        interval is short.
        dedupe (default on, see ww_scraper/dedupe.py) skips URLs that lead to a page the
        crawl already has: redirects to it, and variants its rel=canonical points away
        from. Skipped and duplicate pages are counted in stats["duplicates"] and never
        reach on_page."""
        from .dedupe import DuplicateGuard
        from .extract import parse_html

        queue = queue or (self.ctx.queue if self.ctx else MemoryRequestQueue())
        workers = max(1, int(concurrency or self.default_concurrency))
        guard = DuplicateGuard() if (self.dedupe if dedupe is None else dedupe) else None
        origins = {origin_of(u) for u in start_urls}
        for u in start_urls:
            queue.add(urldefrag(u)[0], depth=0)
        processed = queue.handled_count

        def work(req):
            res = self.fetch(req["url"], skip_target=guard.seen if guard else None)
            doc = None
            if res.ok and res.is_html:
                doc = parse_html(res.text(), res.final_url or req["url"])
                self._inc("pages")
            return res, doc

        def known(req) -> bool:
            """Before fetching: the crawl already has the page this URL leads to."""
            if guard is None or not guard.seen(req["url"]):
                return False
            queue.mark_handled(req)
            self._inc("duplicates")
            return True

        def finish(req, res, doc):
            if req.get("depth", 0) == 0 and res.final_url:
                origins.add(origin_of(res.final_url))
            if guard is not None:
                if res.duplicate:
                    queue.mark_handled(req)
                    return
                if res.ok and guard.register(req["url"], res.final_url,
                                             doc.canonical if doc is not None else ""):
                    self._inc("duplicates")
                    queue.mark_handled(req)
                    return
            on_page(res, req, doc)
            queue.mark_handled(req, error=res.error)
            if not enqueue_links or req.get("depth", 0) >= max_depth:
                return
            if doc is not None and not doc.nofollow and "nofollow" not in res.headers.get("x-robots-tag", ""):
                targets = [urldefrag(link["url"])[0] for link in doc.links]
            elif res.not_modified:
                targets = list(self.known_links.get(req["url"], ()))
            else:
                return
            for target in targets:
                if self.in_scope(target, origins, same_origin, include, exclude):
                    queue.add(target, depth=req.get("depth", 0) + 1)

        if workers == 1:
            while processed < max_pages:
                if self.ctx is not None:
                    self.ctx.check_abort()
                req = queue.fetch_next()
                if req is None:
                    break
                if known(req):
                    continue
                try:
                    res, doc = work(req)
                except Exception:
                    queue.reclaim(req)
                    raise
                finish(req, res, doc)
                processed += 1
            return processed

        # Several requests in flight, never two to one host at the same moment: the rate
        # limiter reserves a slot per host. on_page and the queue are only touched here,
        # in the calling thread, so actors need no locking.
        inflight: dict = {}
        scaler = LoadScaler(workers) if self.autoscale else None
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                   thread_name_prefix="ww-scraper") as pool:
            try:
                while True:
                    if self.ctx is not None:
                        self.ctx.check_abort()
                    limit = scaler.current() if scaler else workers
                    if scaler and limit != self.stats.get("concurrency"):
                        self.stats["concurrency"] = limit
                        if self.log:
                            self.log.info(f"autoscale: {limit} of at most {workers} workers")
                    while len(inflight) < limit and processed + len(inflight) < max_pages:
                        req = queue.fetch_next()
                        if req is None:
                            break
                        if known(req):
                            continue
                        inflight[pool.submit(work, req)] = req
                    if not inflight:
                        break
                    done, _ = concurrent.futures.wait(
                        inflight, timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED)
                    for fut in done:
                        req = inflight.pop(fut)
                        try:
                            res, doc = fut.result()
                        except Exception:
                            queue.reclaim(req)
                            raise
                        finish(req, res, doc)
                        processed += 1
            except BaseException:
                for fut, req in inflight.items():
                    fut.cancel()
                    queue.reclaim(req)
                raise
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
