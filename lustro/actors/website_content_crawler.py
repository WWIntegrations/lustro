"""website-content-crawler: crawl a site and store each page's main content as Markdown
(one dataset item per URL), ready for RAG pipelines. Pages marked noindex (meta robots
or X-Robots-Tag) are visited for links only; their content is not stored."""
from __future__ import annotations

import datetime

from ..actor import MIN_INTERVAL, REDACT, START_URLS, Actor
from ..storage import normalize_url

GLOBS = {"type": "array", "items": {"type": "string"}, "default": []}


class WebsiteContentCrawler(Actor):
    name = "website-content-crawler"
    title = "Website Content Crawler"
    description = "Crawl a website and save each page's main content as Markdown (for RAG)."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxPages": {"type": "integer", "default": 20, "minimum": 1, "maximum": 10000},
            "maxDepth": {"type": "integer", "default": 2, "minimum": 0, "maximum": 20},
            "includeGlobs": GLOBS,
            "excludeGlobs": GLOBS,
            "maxBytesPerPage": {"type": "integer", "default": 5_000_000, "minimum": 1024,
                                "maximum": 50_000_000},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        crawler = ctx.crawler(max_bytes=inp["maxBytesPerPage"])
        stored = set()  # final URLs already saved (redirects can land on a known page)

        def on_page(res, req, doc):
            if res.error or doc is None:
                if res.error:
                    ctx.record_failure(req["url"], res.error)
                return
            xrt = res.headers.get("x-robots-tag", "").lower()
            if doc.noindex or "noindex" in xrt or "none" in xrt:
                ctx.log.info(f"noindex, content not stored: {res.final_url}")
                return
            final = normalize_url(res.final_url or req["url"])
            if final in stored:
                return
            stored.add(final)
            ctx.push_data({
                "url": req["url"],
                "finalUrl": res.final_url,
                "status": res.status,
                "depth": req.get("depth", 0),
                "title": doc.title,
                "description": doc.description,
                "lang": doc.lang,
                "canonical": doc.canonical,
                "markdown": ctx.redact_text(doc.markdown),
                "wordCount": doc.word_count,
                "crawledAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            })

        crawler.crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"],
                      max_depth=inp["maxDepth"], include=inp["includeGlobs"] or None,
                      exclude=inp["excludeGlobs"] or None)
