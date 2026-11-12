"""Crawl the bench site with ww-scraper and print one RESULT line with what it extracted.
Usage: python ww_driver.py <base-url> <defaults|matched> <max-pages>"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ww_scraper.crawler import PoliteCrawler  # noqa: E402

base, mode, max_pages = sys.argv[1], sys.argv[2], int(sys.argv[3])
items = []


def on_page(res, req, doc):
    if doc is not None:
        items.append({"url": res.final_url or req["url"], "title": doc.title})


if mode == "defaults":
    # only allow_loopback differs from defaults: the bench site lives on 127.0.0.1
    PoliteCrawler(allow_loopback=True).crawl([base + "/"], on_page, max_pages=max_pages, max_depth=30)
else:
    # same load budget as the Crawlee run: 10 requests in flight, no per-host delay
    PoliteCrawler(allow_loopback=True, min_interval=0).crawl(
        [base + "/"], on_page, max_pages=max_pages, max_depth=30, concurrency=10)
print("RESULT " + json.dumps({"items": items}))
