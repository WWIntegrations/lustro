"""Compliance primitives: User-Agent, host allowlist, SSRF guard, robots.txt, rate limit.

Lustro never evades access controls. These helpers put that stance in code: a request
that is not clearly permitted is refused, never disguised.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

_CONTACT = os.environ.get("LUSTRO_CONTACT", "https://github.com/WWIntegrations/lustro")
USER_AGENT = f"Lustro/0.1 (+{_CONTACT}) python-requests"

TIMEOUT = 30
MAX_BYTES = 15 * 1024 * 1024


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.5"})
    return s


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
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


_robots_cache: dict = {}
_robots_lock = threading.Lock()


def robots_ok(url: str, user_agent: str = USER_AGENT) -> bool:
    """True only if the host's robots.txt allows user_agent to fetch url.

    If robots.txt cannot be read at all we allow (standard interpretation), but any
    explicit Disallow is honored. Callers that touch HTML pages must gate on this.
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


class RateLimiter:
    """Enforce a minimum interval between requests to the same host (politeness)."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._last: dict = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last.get(host, 0.0))
            if delay > 0:
                time.sleep(delay)
            self._last[host] = time.monotonic()
