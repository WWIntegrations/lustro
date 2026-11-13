"""change-monitor: crawl a site, hash the main content of each page and compare with the
snapshot kept from the previous run under the same monitorName. Reports pages that are
new, changed or removed (404/410). The snapshot lives in the key-value store
`monitor-<monitorName>`; only hashes, titles, word counts, HTTP validators (ETag,
Last-Modified) and the URLs a page links to are kept, never page text.

With conditionalRequests (the default) a page seen before is asked for with
If-None-Match / If-Modified-Since. A server that answers 304 sends no body, so the page
counts as unchanged without being downloaded again, and its links from the last run are
still followed, so pages reachable only through it are not missed."""
from __future__ import annotations

import hashlib
from urllib.parse import urldefrag

from ..actor import (AUTOSCALE, BROWSER_ACTIONS, CONCURRENCY, MIN_INTERVAL, REDACT, RENDER,
                     START_URLS, WAIT_FOR, Actor, _now)
from ..crawler import validator_of

SNAPSHOT_KEY = "SNAPSHOT"
MAX_LINKS = 500


def content_hash(markdown: str) -> str:
    return hashlib.sha256(" ".join(markdown.split()).encode("utf-8")).hexdigest()


class ChangeMonitor(Actor):
    name = "change-monitor"
    title = "Change Monitor"
    description = "Report pages that are new, changed or removed since the previous run."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "monitorName": {"type": "string", "default": "default", "minLength": 1, "maxLength": 40,
                            "description": "Name of the snapshot to compare with (letters, digits, - and _)"},
            "maxPages": {"type": "integer", "default": 50, "minimum": 1, "maximum": 10000},
            "maxDepth": {"type": "integer", "default": 2, "minimum": 0, "maximum": 20},
            "includeUnchanged": {"type": "boolean", "default": False},
            "conditionalRequests": {"type": "boolean", "default": True,
                                    "description": "Ask with ETag / Last-Modified; a 304 means unchanged, no download"},
            "minIntervalSecs": MIN_INTERVAL,
            "maxConcurrency": CONCURRENCY,
            "autoscaleConcurrency": AUTOSCALE,
            "render": RENDER,
            "waitForSelector": WAIT_FOR,
            "browserActions": BROWSER_ACTIONS,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        name = inp["monitorName"]
        if not all(c.isalnum() and c.isascii() or c in "-_" for c in name):
            raise ValueError("monitorName: use letters, digits, - and _ only")
        store = ctx.storage.kv(f"monitor-{name}")
        previous = store.get(SNAPSHOT_KEY, {}) or {}
        pages = previous.get("pages", {})
        current: dict = {}
        gone: set = set()
        crawler = ctx.crawler()
        not_modified = 0
        if inp["conditionalRequests"]:
            for url, page in pages.items():
                if isinstance(page, dict) and (page.get("etag") or page.get("lastModified")):
                    crawler.validators[url] = {"etag": page.get("etag"), "lastModified": page.get("lastModified")}
                if isinstance(page, dict) and isinstance(page.get("links"), list):
                    crawler.known_links[url] = [u for u in page["links"] if isinstance(u, str)][:MAX_LINKS]

        def record(url, res, doc):
            nonlocal not_modified
            if res.not_modified and isinstance(pages.get(url), dict) and "hash" in pages[url]:
                current[url] = dict(pages[url])
                not_modified += 1
            elif doc is not None and not doc.noindex:
                entry = {"hash": content_hash(doc.markdown), "title": doc.title[:300],
                         "wordCount": doc.word_count, **validator_of(res.headers)}
                if not doc.nofollow:
                    links = sorted({urldefrag(link["url"])[0] for link in doc.links
                                    if link["url"].startswith(("http://", "https://"))})[:MAX_LINKS]
                    if links:
                        entry["links"] = links
                current[url] = entry
            elif res.status in (404, 410):
                gone.add(url)
            elif res.error or res.status >= 400:
                ctx.record_failure(url, res.error or f"HTTP {res.status}")

        def on_page(res, req, doc):
            record(urldefrag(res.final_url or req["url"])[0], res, doc)

        crawler.crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"],
                      max_depth=inp["maxDepth"])
        # pages known from the last run that this crawl did not reach: ask for them directly
        budget = inp["maxPages"]
        for url in sorted(set(pages) - set(current) - gone):
            if budget <= 0:
                break
            ctx.check_abort()
            budget -= 1
            res = crawler.fetch(url)
            doc = None
            if res.ok and res.is_html:
                from ..extract import parse_html
                doc = parse_html(res.text(), res.final_url or url)
            record(url, res, doc)

        summary = {"monitorName": name, "firstRun": not previous, "new": 0, "changed": 0,
                   "removed": 0, "unchanged": 0, "notModified": not_modified,
                   "previousRunAt": previous.get("savedAt")}
        for url, now in sorted(current.items()):
            before = pages.get(url)
            state = "new" if before is None else ("changed" if before["hash"] != now["hash"] else "unchanged")
            summary[state] += 1
            if state == "unchanged" and not inp["includeUnchanged"]:
                continue
            item = {"url": url, "change": state, "title": now["title"], "wordCount": now["wordCount"]}
            if state == "changed":
                item["previousTitle"] = before.get("title", "")
                item["wordCountDelta"] = now["wordCount"] - before.get("wordCount", 0)
            ctx.push_data(item)
        for url in sorted(gone & set(pages)):
            summary["removed"] += 1
            ctx.push_data({"url": url, "change": "removed", "title": pages[url].get("title", ""),
                           "wordCount": 0})
        # keep pages we could not check this time, so one failed fetch does not read as "new" later
        kept = {u: p for u, p in pages.items() if u not in current and u not in gone}
        store.set(SNAPSHOT_KEY, {"savedAt": _now(), "runId": ctx.run_id,
                                 "pages": dict(kept, **current)})
        ctx.set_value("OUTPUT", summary)
