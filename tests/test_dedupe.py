"""Duplicate pages: normalised queue keys, the canonical-learning guard, and the crawl that uses both."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import pytest

from ww_scraper.crawler import PoliteCrawler
from ww_scraper.dedupe import DuplicateGuard
from ww_scraper.storage import MemoryRequestQueue, normalize_url

B = "https://shop.example"


# --- queue keys ------------------------------------------------------------------------
def test_query_sorted_and_tracking_parameters_dropped():
    assert normalize_url(B + "/p?b=2&a=1") == normalize_url(B + "/p?a=1&b=2") == B + "/p?a=1&b=2"
    for t in ("utm_source=x&utm_medium=y", "gclid=1", "fbclid=1", "msclkid=1", "mtm_campaign=z", "_ga=1"):
        assert normalize_url(f"{B}/p?{t}") == B + "/p", t
    assert normalize_url(B + "/p?id=5&utm_source=x") == B + "/p?id=5"


def test_repeated_parameters_keep_their_order_and_encoding():
    assert normalize_url(B + "/s?q=a%20b&a=2&a=1") == B + "/s?a=2&a=1&q=a%20b"
    assert normalize_url(B + "/s?q=a+b") == B + "/s?q=a+b"


def test_trailing_slash_is_not_dropped_by_the_queue():
    assert normalize_url(B + "/a/") != normalize_url(B + "/a")


# --- guard -----------------------------------------------------------------------------
def test_a_page_reached_again_is_seen():
    g = DuplicateGuard()
    assert not g.register(B + "/old/1", B + "/p/1")
    assert g.seen(B + "/p/1") and g.seen(B + "/old/1")
    assert g.register(B + "/p/1#x")                      # same final page: a duplicate


def test_canonical_with_the_same_path_marks_a_variant_as_duplicate():
    g = DuplicateGuard()
    assert not g.register(B + "/p/1")
    assert g.register(B + "/p/1?view=print", canonical=B + "/p/1")


def test_parameter_is_learned_only_after_enough_agreeing_pages():
    g = DuplicateGuard(learn_after=3)
    for n in range(2):
        g.register(f"{B}/p/{n}?view=print", canonical=f"{B}/p/{n}")
    assert not g.seen(B + "/p/9?view=print") and g.key(B + "/p/9?view=print").endswith("view=print")
    g.register(B + "/p/2?view=print", canonical=B + "/p/2")
    assert g.key(B + "/p/9?view=print") == B + "/p/9"
    assert g.stats() == {"shop.example": ["view"]}


def test_a_parameter_the_site_keeps_in_its_canonical_is_never_learned():
    g = DuplicateGuard(learn_after=1)
    g.register(B + "/item?id=5", canonical=B + "/item?id=5")     # self-canonical: id matters
    for n in range(5):
        g.register(f"{B}/item?id={n}&ref=x", canonical=f"{B}/item?id={n}")
    assert g.key(B + "/item?id=7&ref=x") == B + "/item?id=7"    # ref learned
    assert g.key(B + "/item?id=7") == B + "/item?id=7"          # id never dropped


def test_canonical_to_another_path_is_not_trusted():
    """A site that points every page at its home page must not make pages look the same."""
    g = DuplicateGuard(learn_after=1)
    g.register(B + "/", canonical=B + "/")
    assert not g.register(B + "/about", canonical=B + "/")
    assert not g.register(B + "/contact", canonical=B + "/")
    assert not g.seen(B + "/pricing")


def test_canonical_on_another_host_is_not_trusted():
    g = DuplicateGuard(learn_after=1)
    g.register("https://mirror.example/p/1")
    assert not g.register(B + "/p/1", canonical="https://mirror.example/p/1")


def test_trailing_slash_rule_is_learned_per_host():
    g = DuplicateGuard(learn_after=2)
    for n in range(2):
        g.register(f"{B}/p/{n}/", canonical=f"{B}/p/{n}")
    assert g.key(B + "/p/9/") == B + "/p/9"
    assert g.key("https://other.example/p/9/") == "https://other.example/p/9/"


# --- crawl -----------------------------------------------------------------------------
N = 12
VARIANTS = ("{}", "{}?view=print", "{}/", "{}?b=2&a=1", "{}?utm_source=x")


class _Shop:
    def __init__(self):
        self.pages = []
        shop = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                parts = [p for p in u.path.split("/") if p]
                if u.path == "/robots.txt":
                    body, code, loc = "User-agent: *\n", 200, None
                elif len(parts) == 2 and parts[0] == "old":
                    body, code, loc = "", 301, f"/p/{parts[1]}"
                elif len(parts) == 2 and parts[0] == "p" and int(parts[1]) < N:
                    n = int(parts[1])
                    shop.pages.append(n)
                    variant = u.path.endswith("/") or bool(dict(parse_qsl(u.query)))
                    canon = f'<link rel="canonical" href="/p/{n}">' if variant else ""
                    links = "".join(f'<a href="{v.format(f"/p/{(n + i) % N}")}">x</a>'
                                    for i, v in enumerate(VARIANTS, start=1))
                    body, code, loc = (f"<html><head><title>P{n}</title>{canon}</head><body>"
                                       f'{links}<a href="/old/{(n + 7) % N}">old</a></body></html>'), 200, None
                else:
                    body, code, loc = "nf", 404, None
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html" if code != 404 else "text/plain")
                self.send_header("Content-Length", str(len(data)))
                if loc:
                    self.send_header("Location", loc)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def shop():
    s = _Shop()
    yield s
    s.server.shutdown()


@pytest.mark.parametrize("concurrency", [1, 4])
def test_crawl_reports_each_page_once_and_fetches_few_duplicates(shop, concurrency):
    shop.pages.clear()
    seen = []
    c = PoliteCrawler(min_interval=0, allow_loopback=True)
    c.crawl([shop.base + "/p/0"], lambda res, req, doc: seen.append(doc.title if doc else None),
            max_pages=500, max_depth=10, queue=MemoryRequestQueue(), concurrency=concurrency)
    titles = [t for t in seen if t]
    assert sorted(titles) == sorted(f"P{n}" for n in range(N))       # every page, each once
    # 5 link forms per page: without the guard the crawl fetches 5 * N pages (next test);
    # with it only the few variants fetched while a rule is still being learned, or that
    # were already in flight, come on top of N
    assert len(shop.pages) <= 2 * N
    assert c.stats["duplicates"] > 0


def test_dedupe_can_be_switched_off(shop):
    shop.pages.clear()
    seen = []
    c = PoliteCrawler(min_interval=0, allow_loopback=True)
    c.crawl([shop.base + "/p/0"], lambda res, req, doc: seen.append(doc.title if doc else None),
            max_pages=500, max_depth=10, queue=MemoryRequestQueue(), dedupe=False)
    assert len([t for t in seen if t]) == len(shop.pages) == 5 * N


def test_redirect_to_a_crawled_page_stops_before_the_second_hop(shop):
    c = PoliteCrawler(min_interval=0, allow_loopback=True)
    g = DuplicateGuard()
    g.register(shop.base + "/p/3")
    shop.pages.clear()
    res = c.fetch(shop.base + "/old/3", skip_target=g.seen)
    assert res.duplicate and res.error.startswith("duplicate:") and shop.pages == []
