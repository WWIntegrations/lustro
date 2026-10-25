"""v0.2 compliance helpers: loopback switch, Retry-After, PII redaction, robots cache."""
import email.utils
import time

from lustro.compliance import (USER_AGENT, RateLimiter, RobotsCache, check_target,
                               is_loopback_host, make_session, parse_retry_after,
                               redact_obj, redact_pii)


def test_user_agent_has_version_and_contact():
    from lustro import __version__
    assert USER_AGENT.startswith(f"Lustro/{__version__} (+http")


def test_loopback_only_with_explicit_switch():
    assert check_target("http://127.0.0.1:8000/") == "non-public address (SSRF guard)"
    assert check_target("http://127.0.0.1:8000/", allow_loopback=True) is None
    assert check_target("http://[::1]:8000/", allow_loopback=True) is None
    # the switch never opens private ranges, metadata IPs, or names
    assert check_target("http://10.0.0.1/", allow_loopback=True)
    assert check_target("http://169.254.169.254/", allow_loopback=True)
    assert check_target("http://localhost/", allow_loopback=True)
    assert not is_loopback_host("localhost")
    assert check_target("ftp://127.0.0.1/", allow_loopback=True) == "scheme not allowed"
    assert check_target("http://u:p@127.0.0.1/", allow_loopback=True) == "credentials in URL"
    assert check_target("https://nonexistent.invalid/") == "unresolvable host"


def test_parse_retry_after():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("") is None and parse_retry_after("soon") is None
    future = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert 25 <= parse_retry_after(future) <= 31


def test_redact_pii():
    s = redact_pii("Mail jan.kowalski@firma.pl, tel. +48 600 700 800 lub (22) 123 45 67.")
    assert s == "Mail [email], tel. [phone] lub [phone]."
    # dates, IPs, versions, short numbers survive
    keep = "2026-10-25, 192.168.100.200, v1.2.3, 12 345 zł, ISBN 978"
    assert redact_pii(keep) == keep
    obj = redact_obj({"url": "https://ex.com/p/600-700-800-900", "text": "+48600700800",
                      "list": ["a@b.co"]})
    assert obj == {"url": "https://ex.com/p/600-700-800-900", "text": "[phone]",
                   "list": ["[email]"]}


def test_rate_limiter_per_host_interval():
    rl = RateLimiter(0.0)
    rl.set_interval("slow", 0.1)
    rl.wait("slow")
    t0 = time.monotonic()
    rl.wait("slow")
    assert time.monotonic() - t0 >= 0.08
    t1 = time.monotonic()
    rl.wait("fast")
    rl.wait("fast")
    assert time.monotonic() - t1 < 0.05


def test_robots_cache_against_site(site):
    rc = RobotsCache(make_session(0), allow_loopback=True)
    assert rc.allowed(site.base + "/about") == (True, "")
    assert rc.allowed(site.base + "/private/secret") == (False, "disallowed by robots.txt")
    assert rc.get(site.base + "/").sitemaps == [site.base + "/sitemap_index.xml"]
    assert site.hits.count("/robots.txt") == 1          # cached per origin
    assert all(a.startswith("Lustro/") for a in site.agents)


def test_robots_cache_refuses_loopback_by_default(site):
    rc = RobotsCache(make_session(0))
    ok, why = rc.allowed(site.base + "/about")
    assert not ok and "unreachable" in why
    assert site.hits == []


def test_dns_rebinding_cannot_reach_loopback(site, monkeypatch):
    """The early check sees a public answer, the connection-time resolution gets
    127.0.0.1: the pinned adapter must refuse, and nothing may reach the test site."""
    import socket

    import pytest
    import requests

    from lustro import compliance
    from lustro.compliance import SSRFBlocked, pin_session

    port = int(site.base.rsplit(":", 1)[1])
    real = socket.getaddrinfo
    calls = []

    def fake(host, *a, **kw):
        if host == "rebind.example":
            calls.append(host)
            ip = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return real(host, *a, **kw)

    monkeypatch.setattr(compliance.socket, "getaddrinfo", fake)
    url = f"http://rebind.example:{port}/"
    assert check_target(url, allow_loopback=True) is None      # first answer: public
    sess = pin_session(requests.Session(), allow_loopback=True)
    with pytest.raises(SSRFBlocked):
        sess.get(url, timeout=5)                                # second answer: loopback
    assert site.hits == []

    # end to end through the crawler: blocked, never retried, never connected
    from lustro.crawler import PoliteCrawler
    calls.clear()
    c = PoliteCrawler(min_interval=0, allow_loopback=True)
    res = c.fetch(url)
    assert res.blocked and res.error.startswith(("ssrf:", "robots:"))
    assert site.hits == []


def test_pinned_adapter_connects_to_validated_ip(site):
    import requests

    from lustro.compliance import SSRFBlocked, pin_session
    ok = pin_session(requests.Session(), allow_loopback=True).get(site.base + "/about", timeout=5)
    assert ok.status_code == 200
    import pytest
    with pytest.raises(SSRFBlocked):
        pin_session(requests.Session()).get(site.base + "/about", timeout=5)


import ipaddress  # noqa: E402

import pytest  # noqa: E402

from lustro.compliance import ip_blocked  # noqa: E402


@pytest.mark.parametrize("addr", [
    "100.64.0.1", "192.0.0.1", "198.18.0.1", "240.0.0.1", "::ffff:127.0.0.1",
    "2002:7f00:0001::", "64:ff9b::7f00:1", "64:ff9b:1:7f00:0:100::", "169.254.169.254",
    "fd00::1", "2001:0:4136:e378:8000:63bf:80ff:fffe", "224.0.0.1", "ff02::1", "0.0.0.0",
    "10.0.0.1", "::1", "127.0.0.1",
])
def test_non_global_addresses_blocked(addr):
    assert ip_blocked(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:4700:4700::1111", "1.1.1.1"])
def test_global_addresses_allowed(addr):
    assert not ip_blocked(ipaddress.ip_address(addr))


def test_loopback_exception_only_with_switch():
    assert not ip_blocked(ipaddress.ip_address("127.0.0.1"), allow_loopback=True)
    assert not ip_blocked(ipaddress.ip_address("::1"), allow_loopback=True)
    assert ip_blocked(ipaddress.ip_address("10.0.0.1"), allow_loopback=True)
    # embedded IPv4 follows the same policy, and the switch never covers names
    assert check_target("http://[::ffff:127.0.0.1]/", allow_loopback=True)
    assert check_target("http://[64:ff9b::a9fe:a9fe]/")  # NAT64 of 169.254.169.254
