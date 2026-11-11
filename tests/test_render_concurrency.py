"""JavaScript rendering through the filtering proxy, the proxy itself, and concurrent crawling."""
import socket
import threading
import time

import pytest

from ww_scraper.actor import run_actor
from ww_scraper.proxy import FilteringProxy
from ww_scraper.render import RenderError, find_browser, render_dom

needs_browser = pytest.mark.skipif(find_browser() is None, reason="no Chrome or Chromium installed")


def _through_proxy(proxy, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
        s.sendall(request)
        s.settimeout(5)
        out = b""
        try:
            while chunk := s.recv(65536):
                out += chunk
        except OSError:
            pass
        return out


def test_proxy_refuses_private_addresses_and_odd_ports(site):
    port = int(site.base.rsplit(":", 1)[1])
    with FilteringProxy(allow_loopback=False) as p:
        assert _through_proxy(p, b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert _through_proxy(p, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert _through_proxy(p, b"CONNECT 10.0.0.5:80 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert _through_proxy(p, b"CONNECT example.org:22 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert _through_proxy(p, b"GET ftp://example.org/x HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert _through_proxy(p, f"GET http://127.0.0.1:{port}/about HTTP/1.1\r\nHost: x\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")
        assert _through_proxy(p, b"garbage\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert len(p.denied) == 7 and p.allowed == []
    assert site.hits == []


def test_proxy_forwards_plain_http_to_the_one_test_port(site):
    port = int(site.base.rsplit(":", 1)[1])
    with FilteringProxy(allow_loopback=True, loopback_port=port) as p:
        body = _through_proxy(p, f"GET http://127.0.0.1:{port}/about HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
        assert b"200" in body.split(b"\r\n", 1)[0] and b"About us" in body
        assert _through_proxy(p, f"CONNECT 127.0.0.1:{port + 1} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")
        assert _through_proxy(p, b"GET http://169.254.169.254/latest/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


def test_render_refuses_non_http_urls():
    with pytest.raises(RenderError):
        render_dom("file:///etc/passwd")
    with pytest.raises(RenderError):
        render_dom("chrome://version")


def test_render_reports_a_missing_browser(monkeypatch):
    monkeypatch.setenv("WW_SCRAPER_CHROME", "/nonexistent/chrome")
    with pytest.raises(RenderError, match="no Chrome"):
        render_dom("https://example.org/")


@needs_browser
def test_rendered_page_has_script_content_and_the_browser_cannot_reach_private_addresses(site, storage):
    inp = {"startUrls": [site.base + "/js"], "maxDepth": 0, "minIntervalSecs": 0}
    raw = run_actor("website-content-crawler", inp, storage, allow_loopback=True)
    (item,) = storage.dataset(raw["id"]).items()
    assert "Loading" in item["markdown"] and "written by a script" not in item["markdown"]

    rec = run_actor("website-content-crawler", dict(inp, render=True), storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec["statusMessage"]
    (item,) = storage.dataset(rec["id"]).items()
    assert item["markdown"].startswith("# Rendered heading")
    assert "Text written by a script." in item["markdown"]
    assert "PROBE REACHED" not in item["markdown"]
    assert rec["stats"]["rendered"] == 1 and rec["stats"]["renderFailed"] == 0
    assert any(d["host"] == "169.254.169.254" for d in render_dom.last_denied)
    assert all(a.startswith("ww-scraper/") for a in site.agents)      # the browser says who it is


def test_render_failure_falls_back_to_raw_html(site, storage, monkeypatch):
    monkeypatch.setenv("WW_SCRAPER_CHROME", "/nonexistent/chrome")
    rec = run_actor("website-content-crawler", {"startUrls": [site.base + "/about"], "maxDepth": 0,
                                                "minIntervalSecs": 0, "render": True},
                    storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED" and rec["itemCount"] == 1
    assert rec["stats"]["renderFailed"] == 1 and rec["stats"]["rendered"] == 0


def test_concurrent_crawl_gives_the_same_items_as_sequential(site, storage):
    def urls(conc):
        rec = run_actor("website-content-crawler",
                        {"startUrls": [site.base + "/"], "minIntervalSecs": 0, "maxConcurrency": conc},
                        storage, allow_loopback=True)
        assert rec["status"] == "SUCCEEDED", rec["statusMessage"]
        return sorted(i["finalUrl"] for i in storage.dataset(rec["id"]).items()), rec

    seq, _ = urls(1)
    par, rec = urls(4)
    assert par == seq and len(seq) >= 3
    assert "/private/secret" not in site.hits
    assert site.hits.count("/robots.txt") == 2          # one robots.txt request per run


def test_concurrent_requests_to_one_host_stay_spaced(site, crawler):
    crawler.min_interval = 0.15
    crawler._hosts_seen.clear()
    stamps = []
    orig = crawler.session.send

    def send(req, **kw):
        if "robots.txt" not in req.url:
            stamps.append(time.monotonic())
        return orig(req, **kw)

    crawler.session.send = send
    seen = []
    crawler.crawl([site.base + "/", site.base + "/about", site.base + "/blog/post-1"],
                  lambda res, req, doc: seen.append(req["url"]), max_depth=0, concurrency=3)
    assert len(seen) == 3 and len(stamps) == 3
    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
    assert min(gaps) >= 0.13, gaps


def test_abort_during_concurrent_crawl_reclaims_requests(site, storage):
    ev = threading.Event()
    ev.set()
    rec = run_actor("website-content-crawler",
                    {"startUrls": [site.base + "/"], "minIntervalSecs": 0, "maxConcurrency": 4},
                    storage, allow_loopback=True, abort_event=ev)
    assert rec["status"] == "ABORTED"
    assert storage.queue(rec["id"]).pending_count >= 0
