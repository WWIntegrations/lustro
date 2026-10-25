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
