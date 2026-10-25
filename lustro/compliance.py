"""Compliance primitives: User-Agent, host allowlist, SSRF guard, robots.txt, rate limit,
Retry-After parsing and PII redaction.

Lustro never evades access controls. These helpers put that stance in code: a request
that is not clearly permitted is refused, never disguised.
"""
from __future__ import annotations

import email.utils
import ipaddress
import os
import re
import socket
import threading
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
import requests.adapters

from ._version import __version__

_CONTACT = os.environ.get("LUSTRO_CONTACT", "https://github.com/WWIntegrations/lustro")
USER_AGENT = f"Lustro/{__version__} (+{_CONTACT}) python-requests"
ROBOTS_TOKEN = "Lustro"

TIMEOUT = 30
MAX_BYTES = 15 * 1024 * 1024

# Floor for the per-host interval of crawler-style fetches. It can only be lowered for
# loopback hosts, and only when the explicit test switch (allow_loopback) is on.
MIN_INTERVAL_FLOOR = 0.5
MAX_CRAWL_DELAY = 30.0

_REDIRECT_CODES = (301, 302, 303, 307, 308)


def make_session(min_interval: float = 1.0) -> requests.Session:
    """Polite session: sets the Lustro UA and enforces a per-host minimum interval between
    requests, so a run never overloads a source (the 'server stability' rule). See
    PoliteSession below."""
    return PoliteSession(min_interval)


def host_allowed(host: str, allowlist) -> bool:
    """True if host equals, or is a subdomain of, an allowlisted host."""
    host = (host or "").lower().strip(".")
    return any(host == a or host.endswith("." + a) for a in allowlist)


def is_public_address(host: str) -> bool:
    """False for unresolvable or private/loopback/link-local/reserved hosts (SSRF guard)."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if ip_blocked(ip):
            return False
    return True


def is_loopback_host(host: str) -> bool:
    """True only for literal loopback IPs (127.0.0.0/8, ::1). Names such as 'localhost'
    are deliberately not accepted, so the test switch cannot be steered through DNS."""
    h = (host or "").strip("[]").lower()
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    # canonical forms only: 127.0.0.0/8 or ::1 (no IPv4-mapped / embedded variants)
    return (ip.version == 4 and ip.is_loopback) or ip == ipaddress.IPv6Address("::1")


_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")      # RFC 6052 well-known prefix
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")  # RFC 8215 local-use prefix


def _embedded_ipv4(ip) -> list:
    """IPv4 addresses carried inside an IPv6 address (mapped, 6to4, Teredo server and
    client, NAT64 /96 and /48)."""
    if ip.version != 6:
        return []
    out = []
    if ip.ipv4_mapped is not None:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        out.append(ip.sixtofour)
    if ip.teredo is not None:
        out.extend(ip.teredo)
    if ip in _NAT64_WKP:
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    elif ip in _NAT64_LOCAL:
        b = ip.packed  # RFC 6052 /48 layout: IPv4 in bytes 6-7 and 9-10 (byte 8 is "u")
        out.append(ipaddress.IPv4Address(b[6:8] + b[9:11]))
    return out


def ip_blocked(ip, allow_loopback: bool = False) -> bool:
    """True if ip (or any IPv4 embedded in it) is not globally routable unicast.
    Loopback passes only with the explicit allow_loopback test switch."""
    for a in [ip, *_embedded_ipv4(ip)]:
        if allow_loopback and a.is_loopback:
            continue
        if not a.is_global or a.is_multicast:
            return True
    return False


class SSRFBlocked(Exception):
    """Raised by the pinned connection layer when a host resolves to a refused address.
    Deliberately not an OSError, so urllib3/requests do not wrap it into a retryable
    connection error."""


def resolve_validated(host: str, allow_loopback: bool = False) -> list:
    """Resolve host ONCE and validate EVERY address. Returns the validated IP strings or
    raises SSRFBlocked. Loopback is admitted only when host itself is a literal loopback
    IP and the explicit test switch is on."""
    h = (host or "").strip("[]").rstrip(".").lower()
    if not h:
        raise SSRFBlocked("no host")
    try:
        infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except OSError:
        raise SSRFBlocked("unresolvable host") from None
    addrs = []
    for *_, sockaddr in infos:
        a = sockaddr[0].split("%")[0]
        if a not in addrs:
            addrs.append(a)
    if not addrs:
        raise SSRFBlocked("unresolvable host")
    loop_ok = allow_loopback and is_loopback_host(h)
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            raise SSRFBlocked("invalid address") from None
        if ip_blocked(ip, allow_loopback=loop_ok):
            raise SSRFBlocked("non-public address (SSRF guard)")
    return addrs


def check_target(url: str, allow_loopback: bool = False):
    """Return None if url may be fetched, else a short reason string.

    Checks scheme, embedded credentials and the SSRF guard (one resolution, every
    address validated). This is the early, cheap check; the binding check happens again
    at connect time in PinnedAdapter, which connects only to the validated IP."""
    try:
        p = urlparse(url)
        host = p.hostname
    except ValueError:
        return "invalid URL"
    if p.scheme not in ("http", "https"):
        return "scheme not allowed"
    if p.username or p.password:
        return "credentials in URL"
    if not host:
        return "no host"
    try:
        resolve_validated(host, allow_loopback)
    except SSRFBlocked as e:
        return str(e)
    return None


# --- DNS-rebinding-safe connections ----------------------------------------------------
# The host is resolved once at connect time, every address is validated, the socket is
# opened to that validated IP (Host header and TLS SNI stay the original name), and the
# peer address is checked again after connecting. Each redirect hop is a new request and
# goes through the same path, so a resolver that changes its answer between the early
# check and the connection cannot steer Lustro into a private network.

def _pinned_classes(allow_loopback: bool):
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.util import connection as u3conn

    def _new_conn(self):
        addrs = resolve_validated(self._dns_host, allow_loopback)
        last = None
        for ip in addrs:
            try:
                sock = u3conn.create_connection((ip, self.port), self.timeout,
                                                source_address=self.source_address,
                                                socket_options=self.socket_options)
            except OSError as e:
                last = e
                continue
            peer = sock.getpeername()[0].split("%")[0]
            if peer != ip:
                sock.close()
                raise SSRFBlocked("peer address differs from validated address")
            return sock
        from urllib3.exceptions import NewConnectionError
        raise NewConnectionError(self, f"Failed to establish a new connection: {last}")

    class PinnedHTTPConnection(HTTPConnection):
        pass

    class PinnedHTTPSConnection(HTTPSConnection):
        pass

    PinnedHTTPConnection._new_conn = _new_conn
    PinnedHTTPSConnection._new_conn = _new_conn

    class PinnedHTTPPool(HTTPConnectionPool):
        ConnectionCls = PinnedHTTPConnection

    class PinnedHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = PinnedHTTPSConnection

    return PinnedHTTPPool, PinnedHTTPSPool


class PinnedAdapter(requests.adapters.HTTPAdapter):
    """requests adapter whose connections go only to resolved-and-validated IPs."""

    def __init__(self, allow_loopback: bool = False, **kw):
        self._pools = _pinned_classes(bool(allow_loopback))
        super().__init__(**kw)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": self._pools[0],
                                                   "https": self._pools[1]}


def pin_session(session, allow_loopback: bool = False):
    """Mount PinnedAdapter for http(s) and ignore environment proxies (a proxy would do
    its own DNS resolution and void the pinning). Returns the session."""
    adapter = PinnedAdapter(allow_loopback)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.trust_env = False
    return session


_robots_cache: dict = {}
_robots_lock = threading.Lock()


def robots_ok(url: str, user_agent: str = USER_AGENT) -> bool:
    """True only if the host's robots.txt allows user_agent to fetch url.

    If robots.txt cannot be read at all we allow (standard interpretation), but any
    explicit Disallow is honored. Callers that touch HTML pages must gate on this.
    (Kept for v0.1 compatibility; the crawler uses the stricter RobotsCache below.)
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    base = f"{p.scheme}://{p.netloc}"
    with _robots_lock:
        if base not in _robots_cache:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(base + "/robots.txt")
            try:
                rp.read()
            except Exception:
                rp = None
            _robots_cache[base] = rp
        rp = _robots_cache[base]
    if rp is None:
        return True
    return rp.can_fetch(user_agent, url)


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower()


class RobotsEntry:
    """Parsed robots.txt for one origin. status: ok | missing | denied | unreachable."""

    def __init__(self, status: str, parser=None, http_status: int = 0):
        self.status = status
        self.parser = parser
        self.http_status = http_status

    def allowed(self, url: str, user_agent: str = USER_AGENT) -> bool:
        if self.status == "missing":
            return True
        if self.status in ("denied", "unreachable") or self.parser is None:
            return False
        return self.parser.can_fetch(user_agent, url)

    @property
    def sitemaps(self) -> list:
        if self.parser is None:
            return []
        return list(self.parser.site_maps() or [])

    def crawl_delay(self, user_agent: str = ROBOTS_TOKEN):
        if self.parser is None:
            return None
        try:
            d = self.parser.crawl_delay(user_agent)
        except Exception:
            return None
        return float(d) if d is not None else None


class RobotsCache:
    """robots.txt per origin, fetched once with the honest Lustro UA through the given
    (polite) session. Every hop of the robots.txt fetch passes the SSRF check.

    Interpretation (conservative): 2xx -> rules apply; 401/403 -> everything disallowed;
    other 4xx -> no robots.txt, everything allowed; 5xx or network error -> host treated
    as unavailable (disallowed). When it is not clearly permitted, Lustro does not fetch.
    """

    def __init__(self, session, allow_loopback: bool = False, timeout: float = 15,
                 max_bytes: int = 512 * 1024, on_fetch=None):
        self.session = session
        self.allow_loopback = allow_loopback
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.on_fetch = on_fetch  # callback(origin) before the robots.txt request
        self._cache: dict = {}
        self._lock = threading.Lock()

    def get(self, url: str) -> RobotsEntry:
        origin = origin_of(url)
        with self._lock:
            entry = self._cache.get(origin)
        if entry is None:
            entry = self._fetch(origin)
            with self._lock:
                self._cache[origin] = entry
        return entry

    def allowed(self, url: str):
        """(allowed: bool, reason: str)."""
        entry = self.get(url)
        if entry.allowed(url):
            return True, ""
        if entry.status == "ok":
            return False, "disallowed by robots.txt"
        return False, f"robots.txt {entry.status} (HTTP {entry.http_status})"

    def _fetch(self, origin: str) -> RobotsEntry:
        current = origin + "/robots.txt"
        if self.on_fetch:
            self.on_fetch(origin)
        for _ in range(5):
            if check_target(current, self.allow_loopback):
                return RobotsEntry("unreachable")
            try:
                resp = self.session.get(current, stream=True, allow_redirects=False,
                                        timeout=self.timeout)
            except (requests.RequestException, SSRFBlocked):
                return RobotsEntry("unreachable")
            try:
                code = resp.status_code
                if code in _REDIRECT_CODES and resp.headers.get("Location"):
                    current = urljoin(current, resp.headers["Location"])
                    continue
                if code in (401, 403):
                    return RobotsEntry("denied", http_status=code)
                if 400 <= code < 500:
                    return RobotsEntry("missing", http_status=code)
                if code >= 500 or code < 200 or code >= 300:
                    return RobotsEntry("unreachable", http_status=code)
                raw = _read_capped(resp, self.max_bytes)
            finally:
                resp.close()
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(raw.decode("utf-8", "replace").splitlines())
            return RobotsEntry("ok", rp, code)
        return RobotsEntry("unreachable")


def _read_capped(resp, max_bytes: int) -> bytes:
    """Read a streamed body up to max_bytes (the rest is dropped)."""
    out = bytearray()
    for chunk in resp.iter_content(64 * 1024):
        out += chunk
        if len(out) >= max_bytes:
            return bytes(out[:max_bytes])
    return bytes(out)


def parse_retry_after(value):
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date), or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - time.time())


class RateLimiter:
    """Enforce a minimum interval between requests to the same host (politeness).

    The interval can be raised per host (e.g. from robots.txt Crawl-delay). Slots are
    reserved under the lock and the sleep happens outside it, so one slow host never
    blocks requests to another."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._last: dict = {}
        self._intervals: dict = {}
        self._lock = threading.Lock()

    def set_interval(self, host: str, seconds: float) -> None:
        with self._lock:
            self._intervals[host] = max(0.0, float(seconds))

    def interval(self, host: str) -> float:
        return self._intervals.get(host, self.min_interval)

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host)
            start = now if last is None else max(now, last + self.interval(host))
            self._last[host] = start
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class PoliteSession(requests.Session):
    """A requests.Session that enforces a per-host minimum interval between requests, so
    Lustro stays within polite rate limits and never hammers a source. This is how the
    'server stability' compliance rule is enforced in code, not just promised."""

    def __init__(self, min_interval: float = 1.0):
        super().__init__()
        self.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.5"})
        self._limiter = RateLimiter(min_interval)

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def request(self, method, url, *args, **kwargs):
        host = urlparse(url).hostname or ""
        self._limiter.wait(host)
        return super().request(method, url, *args, **kwargs)


# --- PII redaction --------------------------------------------------------------------
# Heuristic, on by default: e-mail addresses and phone numbers are replaced before data
# is stored. Disabling it is the operator's decision (and their legal basis under GDPR).

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<![\w/.-])"
    r"(?:\+\d{8,15}"
    r"|(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]\d{2,4}){2,4})"
    r"(?![\w/-])"
)
_IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
EMAIL_MASK = "[email]"
PHONE_MASK = "[phone]"


def _phone_sub(m):
    s = m.group(0)
    digits = sum(ch.isdigit() for ch in s)
    if digits < 9 or digits > 15 or _IPV4_RE.fullmatch(s):
        return s
    return PHONE_MASK


def redact_pii(text, phones: bool = True):
    """Replace e-mail addresses (and, unless phones=False, phone numbers) in text."""
    if not isinstance(text, str) or not text:
        return text
    text = _EMAIL_RE.sub(EMAIL_MASK, text)
    if phones:
        text = _PHONE_RE.sub(_phone_sub, text)
    return text


_URLISH_KEYS = re.compile(r"(url|urls|href|src|canonical|link|location)$", re.I)


def redact_obj(obj, _key: str = ""):
    """Recursively redact strings in dicts/lists. URL-like fields and values keep their
    digits (only e-mails are masked there) so links are not mangled."""
    if isinstance(obj, str):
        urlish = bool(_URLISH_KEYS.search(_key or "")) or obj.startswith(("http://", "https://"))
        return redact_pii(obj, phones=not urlish)
    if isinstance(obj, dict):
        return {k: redact_obj(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, _key) for v in obj]
    return obj
