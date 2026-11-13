"""screenshot: a screenshot (and optionally a PDF) of each page, taken in a local headless
Chrome behind the filtering proxy. Images go to the run's key-value store; each item names
the page, its title and the keys and API paths of its files. robots.txt, the SSRF guard and
per-host pacing apply to every page as for any other actor.
"""
from __future__ import annotations

from ..actor import (AUTOSCALE, BROWSER_ACTIONS, CONCURRENCY, MIN_INTERVAL, PDF, REDACT,
                     SCREENSHOT_FORMAT, START_URLS, WAIT_FOR, Actor)
from .website_content_crawler import _artifacts


class Screenshot(Actor):
    name = "screenshot"
    version = "1.0.0"
    title = "Page Screenshots"
    description = "Screenshots and PDFs of pages, rendered in a local headless Chrome, saved to the key-value store."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "fullPage": {"type": "boolean", "default": False, "description": "The whole page, not just the first screen"},
            "screenshotFormat": SCREENSHOT_FORMAT,
            "pdf": PDF,
            "maxPages": {"type": "integer", "default": 10, "minimum": 1, "maximum": 1000},
            "maxDepth": {"type": "integer", "default": 0, "minimum": 0, "maximum": 5},
            "waitMillis": {"type": "integer", "default": 1500, "minimum": 0, "maximum": 15000,
                           "description": "Time for the page's scripts before the screenshot"},
            "waitForSelector": WAIT_FOR,
            "browserActions": BROWSER_ACTIONS,
            "minIntervalSecs": MIN_INTERVAL,
            "maxConcurrency": CONCURRENCY,
            "autoscaleConcurrency": AUTOSCALE,
            "redactPii": REDACT,
        },
    }
    output_schema = {"items": {"title": "Pages with their files", "template": "/v2/datasets/{defaultDatasetId}/items"},
                     "files": {"title": "Screenshots and PDFs (key-value store keys)",
                               "template": "/v2/key-value-stores/{defaultKeyValueStoreId}/keys"}}

    def run(self, ctx):
        inp = ctx.input
        crawler = ctx.crawler()
        crawler.render = True
        crawler.render_wait_ms = inp["waitMillis"]
        crawler.browser["screenshot"] = "fullPage" if inp["fullPage"] else "viewport"
        summary = {"pages": 0, "screenshots": 0, "pdfs": 0, "failed": 0}

        def on_page(res, req, doc):
            if doc is None:
                if res.error and not res.duplicate:
                    ctx.record_failure(req["url"], res.error)
                    summary["failed"] += 1
                return
            summary["pages"] += 1
            files = _artifacts(ctx, res)
            if not files.get("screenshotKey"):
                ctx.record_failure(req["url"], "no screenshot (the browser could not render the page)")
                summary["failed"] += 1
                return
            summary["screenshots"] += 1
            summary["pdfs"] += bool(files.get("pdfKey"))
            ctx.push_data({"url": req["url"], "finalUrl": res.final_url, "title": ctx.redact_text(doc.title),
                           **files})
            ctx.set_status_message(f"{summary['screenshots']} screenshots")

        crawler.crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"], max_depth=inp["maxDepth"])
        ctx.set_value("OUTPUT", summary)
