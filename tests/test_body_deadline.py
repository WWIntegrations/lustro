"""A server that keeps sending a little at a time must not hold a fetch forever."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ww_scraper.crawler import PoliteCrawler


class _Drip(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(4 * 1024 * 1024))
        self.end_headers()
        try:
            for _ in range(1000):                       # 4 KB every 50 ms: each read succeeds
                self.wfile.write(b" " * 4096)
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass


def test_slow_body_hits_the_body_deadline_not_the_read_timeout():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = PoliteCrawler(min_interval=0, allow_loopback=True, timeout=5, body_timeout=1, max_retries=0)
        t0 = time.monotonic()
        res = c.fetch(f"http://127.0.0.1:{srv.server_address[1]}/big.json", accept_types=None)
        took = time.monotonic() - t0
        assert res.error.startswith("timeout: body not complete"), res.error
        assert took < 4, took
    finally:
        srv.shutdown()


def test_body_deadline_default_scales_with_timeout():
    assert PoliteCrawler(min_interval=0, timeout=10).body_timeout == 60
    assert PoliteCrawler(min_interval=0, timeout=90).body_timeout == 270
