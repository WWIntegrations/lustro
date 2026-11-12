"""A generated 500-page site with the duplicate traps real sites have, and a request log.

Every canonical page is /p/<n>. The same page is also reachable as:
  /p/<n>#part                  fragment
  /p/<n>?utm_source=..&utm_medium=..   tracking parameters (utm_*)
  /p/<n>?gclid=..              ad click id
  /p/<n>?b=2&a=1 and ?a=1&b=2  the same parameters in another order (canonical points to /p/<n>)
  /p/<n>/                      trailing slash (canonical points to /p/<n>)
  /p/<n>?view=print            a print view of the same content (canonical points to /p/<n>)
  /old/<n>                     301 to /p/<n>
Plus traps a polite crawler must not walk into:
  /admin/<k>                   disallowed in robots.txt
  /secret/<k>                  linked only from a page with <meta name="robots" content="nofollow">
The server sleeps LATENCY seconds per response and records every request, so all numbers
come from the server, not from what a crawler reports about itself.
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

N = 500
LATENCY = 0.01
SECRETS = 10
ADMIN = 5
ROBOTS = "User-agent: *\nDisallow: /admin/\n"

VARIANTS = [
    lambda k: f"/p/{k}",
    lambda k: f"/p/{k}#part",
    lambda k: f"/p/{k}?utm_source=newsletter&utm_medium=email",
    lambda k: f"/p/{k}?gclid=Cj0KCQ{k}",
    lambda k: f"/p/{k}?b=2&a=1",
    lambda k: f"/p/{k}?a=1&b=2",
    lambda k: f"/p/{k}/",
    lambda k: f"/p/{k}?view=print",
    lambda k: f"/old/{k}",
]


def links_of(n: int) -> list:
    """Two tree children (so every page is reachable within ~9 hops) plus seven extra links,
    each in a different duplicate form, spread deterministically over the site."""
    out = [f"/p/{c}" for c in (2 * n + 1, 2 * n + 2) if c < N]
    for i, form in enumerate(VARIANTS[1:]):
        out.append(form((n * 37 + i * 101 + 13) % N))
    return out


def page(n: int, canonical: bool) -> str:
    links = "".join(f'<a href="{h}">link</a> ' for h in links_of(n))
    if n == 0:
        links += '<a href="/hub">hub</a> ' + "".join(f'<a href="/admin/{k}">admin</a> ' for k in range(ADMIN))
    head = f'<link rel="canonical" href="/p/{n}">' if canonical else ""
    return (f"<!doctype html><html><head><title>Page {n}</title>{head}</head>"
            f"<body><h1>Page {n}</h1><p class=price>{n * 3 + 0.99:.2f}</p>{links}</body></html>")


class Site:
    def __init__(self, latency: float = LATENCY):
        self.lock = threading.Lock()
        self.latency = latency
        self.reset()
        site = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send(self, code, body="", ctype="text/html; charset=utf-8", headers=None):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with site.lock:
                    site.inflight += 1
                    site.peak = max(site.peak, site.inflight)
                    site.log.append((time.monotonic(), self.path))
                try:
                    time.sleep(site.latency)
                    self.route()
                finally:
                    with site.lock:
                        site.inflight -= 1

            def route(self):
                u = urlsplit(self.path)
                parts = [p for p in u.path.split("/") if p]
                q = dict(parse_qsl(u.query))
                if u.path == "/robots.txt":
                    return self.send(200, ROBOTS, "text/plain")
                if u.path == "/":
                    return self.send(200, page(0, False).replace("Page 0", "Home"))
                if u.path == "/hub":
                    body = ('<html><head><meta name="robots" content="nofollow"><title>Hub</title></head><body>'
                            + "".join(f'<a href="/secret/{k}">s</a> ' for k in range(SECRETS)) + "</body></html>")
                    return self.send(200, body)
                if len(parts) == 2 and parts[0] in ("p", "old", "admin", "secret") and parts[1].isdigit():
                    n = int(parts[1])
                    if parts[0] == "old" and n < N:
                        return self.send(301, headers={"Location": f"/p/{n}"})
                    if parts[0] in ("admin", "secret"):
                        return self.send(200, f"<html><head><title>{parts[0]} {n}</title></head><body>x</body></html>")
                    if n < N:
                        variant = u.path.endswith("/") or bool(set(q) & {"a", "b", "view"})
                        with site.lock:
                            site.served.append(n)
                        return self.send(200, page(n, canonical=variant))
                return self.send(404, "not found", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.log = []
            self.served = []      # canonical page number of every 200 page response, in order
            self.inflight = 0
            self.peak = 0

    def summary(self) -> dict:
        with self.lock:
            log = list(self.log)
            served = list(self.served)
            peak = self.peak
        paths = [p for _, p in log]
        unique = set(served)
        span = (log[-1][0] - log[0][0]) if len(log) > 1 else 0.0
        return {
            "requests": len(paths),
            "page_responses": len(served),
            "unique_pages": len(unique),
            "coverage": round(len(unique) / N, 4),
            "duplicate_page_fetches": len(served) - len(unique),
            "redirect_hops": sum(p.startswith("/old/") for p in paths),
            "robots_disallowed_fetched": sum(p.startswith("/admin/") for p in paths),
            "nofollow_only_fetched": sum(p.startswith("/secret/") for p in paths),
            "peak_concurrent_requests": peak,
            "server_span_secs": round(span, 2),
            "requests_per_sec": round(len(paths) / span, 1) if span else None,
        }
