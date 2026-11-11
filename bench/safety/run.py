"""Safety benchmark: ww-scraper against Crawlee (Apify's open-source crawler) on one local site.

Every number comes from the bench server's own request log or from the tool's output, so
nothing depends on what either tool says about itself. Each tool runs in its default
configuration; Crawlee also runs once with respectRobotsTxtFile switched on, since that
option exists. The only change from defaults: ww-scraper's allow_loopback test switch (the
bench site lives on 127.0.0.1) and Crawlee's navigationTimeoutSecs=5 (an unreachable host
would otherwise stall the run). Neither changes the behaviour measured.

    python bench/safety/run.py --crawlee-dir /path/with/node_modules/crawlee
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ww_scraper import __version__ as WW_VERSION  # noqa: E402
from ww_scraper.crawler import PoliteCrawler  # noqa: E402
from ww_scraper.storage import Storage  # noqa: E402

RETRY_AFTER = 4
PAGES = {
    "/": '<html><body><a href="/about">About</a> <a href="/private/secret">Secret</a></body></html>',
    "/about": "<html><body><p>About</p></body></html>",
    "/private/secret": "<html><body><p>disallowed by robots.txt</p></body></html>",
}
ROBOTS = "User-agent: *\nDisallow: /private/\n"


class Site:
    def __init__(self):
        self.hits: list = []
        self.lock = threading.Lock()
        self.count_429 = 0
        site = self

        class H(BaseHTTPRequestHandler):
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
                    site.hits.append((time.monotonic(), self.path))
                if self.path == "/robots.txt":
                    return self.send(200, ROBOTS, "text/plain")
                if self.path == "/redirect-ssrf":
                    return self.send(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
                if self.path == "/slow-429":
                    with site.lock:
                        site.count_429 += 1
                        first = site.count_429 == 1
                    if first:
                        return self.send(429, "slow down", "text/plain", {"Retry-After": str(RETRY_AFTER)})
                    return self.send(200, "<html><body>ok</body></html>")
                if self.path in PAGES:
                    return self.send(200, PAGES[self.path])
                return self.send(404, "not found", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.hits.clear()
            self.count_429 = 0

    def paths(self):
        with self.lock:
            return [p for _, p in self.hits]

    def gap(self, path):
        with self.lock:
            t = [ts for ts, p in self.hits if p == path]
        return round(t[1] - t[0], 2) if len(t) >= 2 else None


def ww(site, scenario, storage_dir):
    c = PoliteCrawler(allow_loopback=True)
    if scenario == "robots":
        c.crawl([site.base + "/"], lambda *a: None, max_pages=30, max_depth=3)
        return {}
    if scenario == "ssrf":
        res = c.fetch(site.base + "/redirect-ssrf")
        return {"error": res.error}
    if scenario == "retry":
        res = c.fetch(site.base + "/slow-429")
        return {"status": res.status}
    if scenario == "csv":
        ds = Storage(storage_dir).dataset("bench")
        ds.push({"name": '=HYPERLINK("http://evil.example","click")', "n": 1})
        return {"csv": ds.export("csv")[0].decode()}


def crawlee(site, scenario, mode, crawlee_dir):
    driver = Path(__file__).with_name("crawlee_driver.mjs")
    env = dict(os.environ, NODE_PATH=str(Path(crawlee_dir) / "node_modules"))
    # ESM resolves packages from the script's location, so run a copy inside crawlee_dir
    copy = Path(crawlee_dir) / "crawlee_driver.mjs"
    copy.write_text(driver.read_text())
    p = subprocess.run(["node", str(copy), site.base, scenario, mode], capture_output=True,
                       text=True, timeout=300, env=env, cwd=crawlee_dir)
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
    if line is None:
        raise RuntimeError(f"crawlee driver failed: {p.stderr[-800:]}")
    return json.loads(line[7:])


def first_cell(csv_text):
    rows = list(csv.reader(io.StringIO(csv_text)))
    return rows[1][0] if len(rows) > 1 and rows[1] else ""


def redact(text):
    """Error texts can name this machine's own addresses; they never leave the bench."""
    return re.sub(r"Local \([^)]*\)", "Local (redacted)", str(text))


def measure(site, run):
    rows = {}
    site.reset()
    run("robots")
    rows["fetched_page_disallowed_by_robots"] = "/private/secret" in site.paths()
    site.reset()
    out = run("ssrf")
    errs = out.get("error") or " ".join(e["error"] for e in out.get("errors", []))
    rows["redirect_to_cloud_metadata"] = {"refused_before_connecting": str(errs).startswith("ssrf:"), "detail": redact(errs)[:120]}
    site.reset()
    run("retry")
    rows["secs_before_retry_after_429"] = site.gap("/slow-429")
    site.reset()
    out = run("csv")
    rows["csv_cell_for_formula_value"] = first_cell(out.get("csv", "")) if out else ""
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawlee-dir", required=True, help="directory where `npm install crawlee@3` was run")
    ap.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    a = ap.parse_args()
    import tempfile
    site = Site()
    cv = json.loads((Path(a.crawlee_dir) / "node_modules/crawlee/package.json").read_text())["version"]
    with tempfile.TemporaryDirectory() as tmp:
        results = {
            "environment": {"python": platform.python_version(), "node": subprocess.run(
                ["node", "--version"], capture_output=True, text=True).stdout.strip(),
                "os": platform.platform(terse=True), "ww_scraper": WW_VERSION, "crawlee": cv,
                "retry_after_secs": RETRY_AFTER},
            "ww-scraper (defaults)": measure(site, lambda s: ww(site, s, tmp)),
            "Crawlee CheerioCrawler (defaults)": measure(site, lambda s: crawlee(site, s, "default", a.crawlee_dir)),
            "Crawlee CheerioCrawler (respectRobotsTxtFile: true)": measure(
                site, lambda s: crawlee(site, s, "robots-on", a.crawlee_dir)),
        }
    Path(a.out).write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
