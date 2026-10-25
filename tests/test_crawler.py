"""PoliteCrawler against the local test site: robots, SSRF per hop, retries, limits."""
import time

from lustro.crawler import PoliteCrawler
from lustro.storage import MemoryRequestQueue


def test_default_config_refuses_loopback(site):
    c = PoliteCrawler(min_interval=0)
    res = c.fetch(site.base + "/")
    assert res.blocked and res.error.startswith("ssrf:")
    assert site.hits == []


def test_robots_disallow_is_never_fetched(site, crawler):
    res = crawler.fetch(site.base + "/private/secret")
    assert res.blocked and res.error == "robots: disallowed by robots.txt"
    assert "/private/secret" not in site.hits


def test_redirect_chain_recorded(site, crawler):
    res = crawler.fetch(site.base + "/redirect-1")
    assert res.ok and res.final_url == site.base + "/about"
    assert [r["status"] for r in res.redirects] == [301, 302]


def test_ssrf_checked_on_every_redirect_hop(site, crawler):
    res = crawler.fetch(site.base + "/redirect-ssrf")
    assert res.blocked and res.error.startswith("ssrf:")
    assert res.final_url.startswith("http://169.254.169.254")
    assert crawler.stats["ssrfBlocked"] == 1


def test_redirect_loop_is_bounded(site, crawler):
    crawler.max_redirects = 3
    res = crawler.fetch(site.base + "/redirect-loop")
    assert res.error == "too-many-redirects"


def test_429_retry_after_is_honoured(site, crawler):
    t0 = time.monotonic()
    res = crawler.fetch(site.base + "/slow-429")
    assert res.ok and b"ok after 429" in res.body
    assert time.monotonic() - t0 >= 0.9            # waited the Retry-After second
    assert crawler.stats["retries"] == 1


def test_retry_after_longer_than_cap_gives_up_politely(site, crawler):
    res = crawler.fetch(site.base + "/too-late-429")
    assert res.status == 429 and site.hits.count("/too-late-429") == 1


def test_5xx_backoff_then_success(site, crawler):
    res = crawler.fetch(site.base + "/flaky-500")
    assert res.ok and crawler.stats["retries"] == 1


def test_content_type_filter_and_byte_limit(site, crawler):
    res = crawler.fetch(site.base + "/img/logo.png")
    assert res.status == 200 and res.error == "content-type: image/png" and res.body == b""
    res = crawler.fetch(site.base + "/big", max_bytes=10_000)
    assert res.error.startswith("too-large")


def test_crawl_scope_depth_and_max_pages(site, crawler):
    seen = []
    crawler.crawl([site.base + "/"], lambda res, req, doc: seen.append((req["url"], req["depth"])),
                  max_pages=50, max_depth=1, queue=MemoryRequestQueue())
    urls = {u for u, _ in seen}
    assert site.base + "/about" in urls and site.base + "/blog/post-1" in urls
    assert not any("nonexistent.invalid" in u for u in urls)     # same-origin only
    assert not any(u.endswith(".pdf") for u in urls)               # binary skipped
    assert max(d for _, d in seen) <= 1
    assert "/private/secret" not in site.hits

    site.reset()
    seen.clear()
    crawler.crawl([site.base + "/"], lambda res, req, doc: seen.append(req["url"]),
                  max_pages=2, queue=MemoryRequestQueue())
    assert len(seen) == 2


def test_include_exclude_globs():
    origins = {"https://ex.com"}
    assert PoliteCrawler.in_scope("https://ex.com/blog/a", origins, include=["*/blog/*"])
    assert not PoliteCrawler.in_scope("https://ex.com/shop", origins, include=["*/blog/*"])
    assert not PoliteCrawler.in_scope("https://ex.com/blog/a", origins, exclude=["*/blog/*"])
    assert not PoliteCrawler.in_scope("https://other.com/", origins)
    assert PoliteCrawler.in_scope("https://other.com/", origins, same_origin=False)


def test_crawl_resumes_from_durable_queue(site, storage):
    q = storage.queue("resume")
    c = PoliteCrawler(min_interval=0, allow_loopback=True)
    first = []
    c.crawl([site.base + "/"], lambda r, req, d: first.append(req["url"]), max_pages=2, queue=q)
    q2 = storage.queue("resume")
    second = []
    PoliteCrawler(min_interval=0, allow_loopback=True).crawl(
        [site.base + "/"], lambda r, req, d: second.append(req["url"]), max_pages=10, queue=q2)
    assert len(first) == 2 and second and not set(first) & set(second)


def test_politeness_floor_applies_to_non_loopback_hosts():
    c = PoliteCrawler(min_interval=0, allow_loopback=True)
    assert c._effective_interval("127.0.0.1") == 0
    assert c._effective_interval("example.com") >= 0.5
    c2 = PoliteCrawler(min_interval=0)
    assert c2._effective_interval("127.0.0.1") >= 0.5
