"""A small local website for tests and demos (127.0.0.1 only).

It exercises every compliance path: robots.txt with a Disallow, a sitemap index with a
gzip child, redirect chains, a redirect into a private address (SSRF), a 429 with
Retry-After, a 404, a noindex page, security headers, cookies, llms.txt and PII.

    python3 -m ww_scraper testsite --port 8766
"""
from __future__ import annotations

import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = """<!doctype html>
<html lang="pl"><head>
<title>ww-scraper Test Site - home page</title>
<meta name="description" content="A tiny local website used to test ww-scraper actors offline, end to end.">
<link rel="canonical" href="{base}/">
<link rel="alternate" hreflang="pl" href="{base}/">
<link rel="alternate" hreflang="en" href="{base}/en">
<link rel="alternate" type="application/rss+xml" title="Blog feed" href="/feed.xml">
<meta name="twitter:card" content="summary">
<meta property="og:title" content="ww-scraper Test Site">
<meta property="og:description" content="Offline fixture">
<meta property="og:site_name" content="ww-scraper Test Site">
<script type="application/ld+json">{{"@context":"https://schema.org","@graph":[{{"@type":"Organization","name":"ww-scraper"}},{{"@type":"WebSite"}}]}}</script>
<style>body{{color:red}}</style>
</head><body>
<header><a href="/">Logo</a></header>
<nav><a href="/about">About</a> <a href="/blog/post-1">Blog</a></nav>
<main>
<h1>Welcome to the test site</h1>
<p>This is <strong>important</strong> text with a <a href="/about">link to about</a>.
Contact: kontakt@example.com or +48 600 700 800.</p>
<h2>Features</h2>
<ul><li>Fast<li>Polite<ul><li>robots.txt</li></ul></li></ul>
<ol><li>First</li><li>Second</li></ol>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>a</td><td>1</td></tr></table>
<pre><code class="language-python">print("hi")</code></pre>
<blockquote><p>Quoted text</p></blockquote>
<img src="/img/logo.png" alt="Logo"><img src="/img/no-alt.png">
<p><a href="/blog/post-1">Post 1</a> <a href="/private/secret">Secret</a>
<a href="/redirect-1">Old link</a> <a href="/missing">Missing</a>
<a href="/noindex">Hidden</a> <a href="/file.pdf">PDF</a>
<a href="mailto:kontakt@example.com">Mail</a> <a href="javascript:alert(1)">JS</a>
<a href="https://nonexistent.invalid/page">External</a></p>
</main>
<footer>Footer text that must not appear</footer>
<script>var x = 1;</script>
</body></html>"""

ABOUT = """<!doctype html><html><head><title>About</title></head><body>
<h1>About us</h1><h1>Second h1</h1><p>About page.</p><a href="/">Home</a>
</body></html>"""

POST = """<!doctype html><html lang="en"><head><title>First post on the ww-scraper test blog</title>
<meta name="description" content="Short."></head><body>
<article><h1>Post 1</h1><p>Post body. See <a href="/redirect-1">old page</a>.</p></article>
<aside>Sidebar must not appear</aside></body></html>"""

NOINDEX = """<!doctype html><html><head><title>Hidden</title>
<meta name="robots" content="noindex"></head><body><p>Do not index me.</p></body></html>"""

JS_PAGE = """<!doctype html><html lang="en"><head><title>Script page</title></head><body>
<main><h1>Loading</h1><p id="app">empty</p><p id="probe">not tried</p></main>
<script>
document.querySelector("h1").textContent = "Rendered heading";
document.getElementById("app").textContent = "Text written by a script.";
fetch("http://169.254.169.254/latest/meta-data/").then(
  function () { document.getElementById("probe").textContent = "PROBE REACHED"; },
  function () { document.getElementById("probe").textContent = "probe refused"; });
</script></body></html>"""

# an app shell: nothing in the HTML until a script builds the page, and one more paragraph
# only after a click (for browserActions, waitForSelector and "render": "auto")
APP_PAGE = """<!doctype html><html lang="en"><head><title>App shell</title></head><body>
<div id="root"></div><noscript>You need to enable JavaScript to run this app.</noscript>
<script>
setTimeout(function () {
  var main = document.createElement("main");
  var h = document.createElement("h1"); h.textContent = "Shell app"; main.appendChild(h);
  var p = document.createElement("p");
  p.textContent = "This text was built by the app after the page loaded, so only a browser sees it.";
  main.appendChild(p);
  var b = document.createElement("button"); b.id = "more"; b.textContent = "More";
  b.onclick = function () {
    var x = document.createElement("p"); x.id = "extra"; x.textContent = "Loaded after a click."; main.appendChild(x);
  };
  main.appendChild(b);
  document.getElementById("root").appendChild(main);
}, 400);
</script></body></html>"""

ROBOTS = ("User-agent: *\nDisallow: /private/\nDisallow: /*.bak$\nAllow: /private/open\n"
          "Crawl-delay: 0\n\nUser-agent: badbot\nDisallow: /\n\nSitemap: {base}/sitemap_index.xml\n")

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test blog</title><link>{base}/</link>
<item><title>Post 1 &amp; more</title><link>{base}/blog/post-1</link>
<guid>post-1</guid><pubDate>Thu, 01 Oct 2026 08:00:00 GMT</pubDate>
<description><![CDATA[<p>First post.</p>]]></description></item>
<item><title>Post 2</title><link>{base}/blog/post-2</link><guid>post-2</guid></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom blog</title>
<entry><title>Atom entry</title><link rel="alternate" href="{base}/blog/post-1"/>
<id>urn:test:1</id><updated>2026-10-01T08:00:00Z</updated><summary>Short summary.</summary></entry>
</feed>"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{base}/sitemap-pages.xml</loc></sitemap>
<sitemap><loc>{base}/sitemap-blog.xml.gz</loc></sitemap>
</sitemapindex>"""

SITEMAP_PAGES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{base}/</loc><lastmod>2026-10-01</lastmod><priority>1.0</priority></url>
<url><loc>{base}/about</loc><changefreq>monthly</changefreq></url>
</urlset>"""

SITEMAP_BLOG = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{base}/blog/post-1</loc></url>
</urlset>"""

LLMS = """# ww-scraper Test Site

> Offline fixture for ww-scraper.

Some notes.

## Docs

- [Home]({base}/): the home page
- [About]({base}/about)

## Optional

- [Blog]({base}/blog/post-1)
"""

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")


class _Handler(BaseHTTPRequestHandler):
    site = None
    server_version = "TestSite"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            if isinstance(v, list):
                for item in v:
                    self.send_header(k, item)
            else:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        site = self.site
        path = self.path.split("?")[0]
        with site.lock:
            site.hits.append(path)
            site.agents.append(self.headers.get("User-Agent", ""))
            site.langs.append(self.headers.get("Accept-Language", ""))
        b = site.base
        sec = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
               "Set-Cookie": ["sid=SECRETVALUE; HttpOnly; SameSite=Lax", "pref=1"],
               "Server": "TestSite/1.2"}
        routes = {
            "/": lambda: self._send(200, HOME.format(base=b), headers=sec),
            "/about": lambda: self._send(200, ABOUT),
            "/en": lambda: self._send(200, ABOUT),
            "/blog/post-1": lambda: self._send(200, POST),
            "/noindex": lambda: self._send(200, NOINDEX),
            "/private/secret": lambda: self._send(200, "<p>secret</p>"),
            "/robots.txt": lambda: self._send(200, ROBOTS.format(base=b), "text/plain"),
            "/sitemap_index.xml": lambda: self._send(200, SITEMAP_INDEX.format(base=b), "application/xml"),
            "/sitemap-pages.xml": lambda: self._send(200, SITEMAP_PAGES.format(base=b), "application/xml"),
            "/sitemap-blog.xml.gz": lambda: self._send(
                200, gzip.compress(SITEMAP_BLOG.format(base=b).encode()), "application/x-gzip"),
            "/sitemap.xml": lambda: self._send(200, SITEMAP_PAGES.format(base=b), "application/xml"),
            "/feed.xml": lambda: self._send(200, FEED.format(base=b), "application/rss+xml"),
            "/atom.xml": lambda: self._send(200, ATOM.format(base=b), "application/atom+xml"),
            "/private/open": lambda: self._send(200, "<html><head><title>Open</title></head><body><p>Open page.</p></body></html>"),
            "/old.bak": lambda: self._send(200, "<p>backup</p>"),
            "/changing": lambda: self._changing(),
            "/cached": lambda: self._cached(),
            "/js": lambda: self._send(200, JS_PAGE),
            "/paywall": lambda: self._send(402, "", ""),
            "/shell": lambda: self._send(200, '<!doctype html><html><head><title>App</title>'
                                              '</head><body><div id="root"></div></body></html>'),
            "/app": lambda: self._send(200, APP_PAGE),
            "/llms.txt": lambda: self._send(200, LLMS.format(base=b), "text/plain; charset=utf-8"),
            "/llms-full.txt": lambda: self._send(200, "# Full\n\nAll the docs in one file.\n", "text/plain"),
            "/redirect-1": lambda: self._send(301, headers={"Location": "/redirect-2"}),
            "/redirect-2": lambda: self._send(302, headers={"Location": b + "/about"}),
            "/redirect-ssrf": lambda: self._send(302, headers={"Location": "http://169.254.169.254/latest/"}),
            "/redirect-loop": lambda: self._send(302, headers={"Location": "/redirect-loop"}),
            "/img/logo.png": lambda: self._send(200, PNG, "image/png"),
            "/file.pdf": lambda: self._send(200, b"%PDF-1.4", "application/pdf"),
            "/big": lambda: self._send(200, "<p>" + "x" * 200_000 + "</p>"),
            "/flaky-500": lambda: self._flaky(500),
        }
        if path == "/slow-429":
            with site.lock:
                site.counter[path] = site.counter.get(path, 0) + 1
                n = site.counter[path]
            if n == 1:
                return self._send(429, "slow down", "text/plain", {"Retry-After": "1"})
            return self._send(200, "<html><head><title>ok after 429</title></head><body><p>ok</p></body></html>")
        if path == "/too-late-429":
            return self._send(429, "later", "text/plain", {"Retry-After": "3600"})
        handler = routes.get(path)
        if handler is None:
            return self._send(404, "<h1>Not found</h1>")
        handler()

    def _changing(self):
        site = self.site
        with site.lock:
            state = site.changing
        if state == "gone":
            return self._send(404, "<h1>404</h1>")
        return self._send(200, f"<html><head><title>Changing</title></head><body><main><p>Version {state}.</p></main></body></html>")

    def _cached(self):
        """A page with an ETag and Last-Modified that answers 304 when the validator matches."""
        site = self.site
        with site.lock:
            version = site.cached
            site.conditional.append({"ifNoneMatch": self.headers.get("If-None-Match"),
                                     "ifModifiedSince": self.headers.get("If-Modified-Since")})
        etag = f'"v{version}"'
        headers = {"ETag": etag, "Last-Modified": "Tue, 10 Nov 2026 08:00:00 GMT"}
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, headers=headers)
        return self._send(200, f'<html><head><title>Cached</title></head><body><main><p>Cached version {version}.</p>'
                               f'<a href="/about">About</a></main></body></html>', headers=headers)

    def _flaky(self, code):
        site = self.site
        with site.lock:
            site.counter["flaky"] = site.counter.get("flaky", 0) + 1
            n = site.counter["flaky"]
        if n == 1:
            return self._send(code, "error", "text/plain")
        return self._send(200, "<p>recovered</p>")


class TestSite:
    """Start with TestSite().start(); base URL in .base; requested paths in .hits."""

    __test__ = False  # not a pytest test class

    def __init__(self, port: int = 0):
        self.hits: list = []
        self.agents: list = []
        self.langs: list = []
        self.counter: dict = {}
        self.changing = "1"      # body version of /changing, or "gone" for a 404
        self.cached = "1"        # body version of /cached (its ETag is "v<version>")
        self.conditional: list = []   # validators each /cached request carried
        self.lock = threading.Lock()
        site = self

        class H(_Handler):
            pass

        H.site = site
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def reset(self):
        with self.lock:
            self.hits.clear()
            self.agents.clear()
            self.langs.clear()
            self.counter.clear()
            self.changing = "1"
            self.cached = "1"
            self.conditional.clear()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
