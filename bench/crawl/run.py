"""Crawl benchmark: ww-scraper against Crawlee (open-source Node.js crawler) on a generated
500-page local site full of duplicate URLs (see bench_site.py).

Two runs per tool:
  defaults  each tool as shipped, first 60 pages: how hard it hits one server
  matched   the same budget for both (10 requests in flight, no per-host delay, robots.txt
            on), the whole site: coverage, duplicate fetches, traps, time, CPU and memory
Crawl numbers come from the server's request log; time and memory from /usr/bin/time -l.

    python bench/crawl/run.py --crawlee-dir /path/with/node_modules/crawlee [--repeat 3]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from bench_site import N, Site  # noqa: E402
from ww_scraper import __version__ as WW_VERSION  # noqa: E402

DEFAULT_PAGES = 60
MATCHED_PAGES = 5000


def timed(cmd, cwd=None):
    """Run cmd under /usr/bin/time -l (macOS) or -v (GNU); return stdout and the measures."""
    flag = "-l" if sys.platform == "darwin" else "-v"
    env = dict(os.environ, LC_ALL="C")  # time prints "0,00" under a comma-decimal locale
    p = subprocess.run(["/usr/bin/time", flag, *cmd], capture_output=True, text=True, timeout=1800, cwd=cwd, env=env)
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
    if line is None:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-1500:]}")
    err = p.stderr
    if sys.platform == "darwin":
        m = re.search(r"([\d.]+) real\s+([\d.]+) user\s+([\d.]+) sys", err)
        rss = int(re.search(r"(\d+)\s+maximum resident set size", err).group(1)) / 2**20
        wall, cpu = float(m.group(1)), float(m.group(2)) + float(m.group(3))
    else:
        g = lambda k: re.search(k + r":\s*([\d.:]+)", err).group(1)  # noqa: E731
        rss = int(g("Maximum resident set size \\(kbytes\\)")) / 1024
        h, mnt, s = (["0", "0"] + g("Elapsed \\(wall clock\\) time \\(h:mm:ss or m:ss\\)").split(":"))[-3:]
        wall = int(h) * 3600 + int(mnt) * 60 + float(s)
        cpu = float(g("User time \\(seconds\\)")) + float(g("System time \\(seconds\\)"))
    return json.loads(line[7:]), {"wall_secs": round(wall, 2), "cpu_secs": round(cpu, 2), "peak_rss_mb": round(rss, 1)}


def output_quality(items):
    titles = [i["title"] for i in items]
    pages = [t for t in titles if re.fullmatch(r"Page \d+", t)]
    return {"items": len(items), "unique_page_items": len(set(pages)), "duplicate_items": len(pages) - len(set(pages))}


def run_tool(site, tool, mode, crawlee_dir):
    pages = DEFAULT_PAGES if mode == "defaults" else MATCHED_PAGES
    site.reset()
    if tool == "ww":
        out, cost = timed([sys.executable, str(HERE / "ww_driver.py"), site.base, mode, str(pages)])
    else:
        copy = Path(crawlee_dir) / "crawlee_crawl.mjs"   # ESM resolves packages next to the script
        copy.write_text((HERE / "crawlee_crawl.mjs").read_text())
        out, cost = timed(["node", str(copy), site.base, mode, str(pages)], cwd=crawlee_dir)
    return {**site.summary(), **output_quality(out["items"]), **cost}


def median_run(site, tool, mode, crawlee_dir, repeat):
    runs = [run_tool(site, tool, mode, crawlee_dir) for _ in range(repeat)]
    if repeat == 1:
        return runs[0]
    keys = ("wall_secs", "cpu_secs", "peak_rss_mb", "server_span_secs", "requests_per_sec")
    med = dict(runs[0])
    for k in keys:
        vals = [r[k] for r in runs if r[k] is not None]
        med[k] = round(statistics.median(vals), 2) if vals else None
    med["runs"] = repeat
    return med


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawlee-dir", required=True, help="directory where `npm install crawlee@3` was run")
    ap.add_argument("--repeat", type=int, default=3, help="matched runs per tool (median reported)")
    ap.add_argument("--out", default=str(HERE / "results.json"))
    a = ap.parse_args()
    site = Site()
    cv = json.loads((Path(a.crawlee_dir) / "node_modules/crawlee/package.json").read_text())["version"]
    results = {
        "environment": {"python": platform.python_version(),
                        "node": subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip(),
                        "os": platform.platform(terse=True), "cpu": platform.machine(),
                        "ww_scraper": WW_VERSION, "crawlee": cv, "site_pages": N, "server_latency_secs": site.latency},
        "defaults (first 60 pages)": {
            "ww-scraper": run_tool(site, "ww", "defaults", a.crawlee_dir),
            "Crawlee CheerioCrawler": run_tool(site, "crawlee", "defaults", a.crawlee_dir),
        },
        "matched (10 in flight, robots on, whole site)": {
            "ww-scraper": median_run(site, "ww", "matched", a.crawlee_dir, a.repeat),
            "Crawlee CheerioCrawler": median_run(site, "crawlee", "matched", a.crawlee_dir, a.repeat),
        },
    }
    Path(a.out).write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
