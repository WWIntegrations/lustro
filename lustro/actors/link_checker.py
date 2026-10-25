"""link-checker: crawl a site, collect its links (and image sources), and report broken
links and redirect chains. External links are checked too (robots.txt and rate limits
apply to every host), unless checkExternal is false."""
from __future__ import annotations

from urllib.parse import urldefrag

from ..actor import MIN_INTERVAL, REDACT, START_URLS, Actor
from ..compliance import origin_of


def _link_item(url, res, found_on, internal):
    chain = [{"url": r["url"], "status": r["status"]} for r in res.redirects]
    unchecked = res.blocked
    broken = (not unchecked) and (bool(res.error and not res.status) or res.status >= 400)
    return {
        "url": url,
        "status": res.status,
        "finalUrl": res.final_url,
        "redirectChain": chain,
        "redirectHops": len(chain),
        "broken": broken,
        "unchecked": unchecked,
        "error": res.error if (res.error and not res.error.startswith("content-type")) else "",
        "internal": internal,
        "foundOn": sorted(found_on)[:5],
    }


class LinkChecker(Actor):
    name = "link-checker"
    title = "Link Checker"
    description = "Find broken links and redirect chains across a site."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxPages": {"type": "integer", "default": 50, "minimum": 1, "maximum": 10000},
            "maxDepth": {"type": "integer", "default": 3, "minimum": 0, "maximum": 20},
            "maxLinks": {"type": "integer", "default": 1000, "minimum": 1, "maximum": 100000},
            "checkExternal": {"type": "boolean", "default": True},
            "includeImages": {"type": "boolean", "default": True},
            "onlyProblems": {"type": "boolean", "default": False},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        crawler = ctx.crawler()
        origins = {origin_of(u) for u in inp["startUrls"]}
        found: dict = {}       # url -> set(pages it appears on)
        results: dict = {}     # url -> FetchResult already obtained during the crawl

        def on_page(res, req, doc):
            results[urldefrag(req["url"])[0]] = res
            if doc is None:
                return
            if req.get("depth", 0) == 0 and res.final_url:
                origins.add(origin_of(res.final_url))
            targets = [l["url"] for l in doc.links]
            if inp["includeImages"]:
                targets += [im["src"] for im in doc.images]
            for t in targets:
                t = urldefrag(t)[0]
                found.setdefault(t, set()).add(res.final_url or req["url"])

        crawler.crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"],
                      max_depth=inp["maxDepth"])
        for u in inp["startUrls"]:
            found.setdefault(urldefrag(u)[0], set())

        summary = {"checked": 0, "broken": 0, "redirectChains": 0, "unchecked": 0}
        for url in list(found)[: inp["maxLinks"]]:
            ctx.check_abort()
            internal = origin_of(url) in origins
            if not internal and not inp["checkExternal"]:
                continue
            res = results.get(url)
            if res is None:
                res = crawler.fetch(url, accept_types=None, read_body=False)
            item = _link_item(url, res, found[url], internal)
            summary["checked"] += 1
            summary["broken"] += item["broken"]
            summary["unchecked"] += item["unchecked"]
            summary["redirectChains"] += item["redirectHops"] > 1
            if inp["onlyProblems"] and not (item["broken"] or item["redirectHops"] > 1):
                continue
            ctx.push_data(item)
        ctx.set_value("OUTPUT", summary)
