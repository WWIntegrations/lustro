"""seo-audit: crawl pages and report on-page SEO signals per URL, plus site-level checks
(robots.txt, sitemap, /llms.txt). Issues carry a severity: error, warning or info."""
from __future__ import annotations

from ..actor import MIN_INTERVAL, REDACT, START_URLS, Actor
from ..compliance import origin_of

TITLE_RANGE = (10, 60)
DESC_RANGE = (50, 160)
_PENALTY = {"error": 10, "warning": 4, "info": 1}


def audit_page(res, doc) -> dict:
    """Pure function: SEO findings for one fetched page (doc may be None)."""
    issues = []

    def add(sev, code, msg):
        issues.append({"severity": sev, "code": code, "message": msg})

    item = {"url": res.url, "finalUrl": res.final_url, "status": res.status,
            "redirects": len(res.redirects)}
    if res.status != 200:
        add("error", "status", f"HTTP status {res.status or res.error}")
    if doc is None:
        item.update(issues=issues, score=max(0, 100 - sum(_PENALTY[i["severity"]] for i in issues)))
        return item
    tl, dl = len(doc.title), len(doc.description)
    h1 = len(doc.headings.get("h1", []))
    alt_missing = sum(1 for im in doc.images if im["alt"] is None)
    xrt = res.headers.get("x-robots-tag", "").lower()
    noindex = doc.noindex or "noindex" in xrt
    item.update({
        "title": doc.title, "titleLength": tl,
        "description": doc.description, "descriptionLength": dl,
        "h1Count": h1, "h1": doc.headings.get("h1", [])[:3],
        "canonical": doc.canonical,
        "canonicalIsSelf": bool(doc.canonical) and doc.canonical.rstrip("/") == res.final_url.rstrip("/"),
        "robots": doc.robots or None, "xRobotsTag": xrt or None, "noindex": noindex,
        "lang": doc.lang, "hreflang": doc.hreflang,
        "og": {k: doc.og.get(k, "") for k in ("og:title", "og:description", "og:image", "og:type")},
        "jsonldTypes": doc.jsonld_types,
        "imagesTotal": len(doc.images), "imagesMissingAlt": alt_missing,
        "wordCount": doc.word_count,
    })
    if not tl:
        add("error", "title-missing", "missing <title>")
    elif tl < TITLE_RANGE[0]:
        add("warning", "title-short", f"title is {tl} chars (< {TITLE_RANGE[0]})")
    elif tl > TITLE_RANGE[1]:
        add("warning", "title-long", f"title is {tl} chars (> {TITLE_RANGE[1]}, likely truncated)")
    if not dl:
        add("warning", "description-missing", "missing meta description")
    elif dl < DESC_RANGE[0]:
        add("info", "description-short", f"meta description is {dl} chars (< {DESC_RANGE[0]})")
    elif dl > DESC_RANGE[1]:
        add("info", "description-long", f"meta description is {dl} chars (> {DESC_RANGE[1]})")
    if h1 == 0:
        add("warning", "h1-missing", "no <h1>")
    elif h1 > 1:
        add("info", "h1-multiple", f"{h1} <h1> elements")
    if not doc.canonical:
        add("info", "canonical-missing", "no rel=canonical")
    elif not item["canonicalIsSelf"]:
        add("info", "canonical-other", f"canonical points to {doc.canonical}")
    if noindex:
        add("warning", "noindex", "page is noindex")
    if not doc.lang:
        add("warning", "lang-missing", "<html> has no lang attribute")
    for k in ("og:title", "og:description", "og:image"):
        if not doc.og.get(k):
            add("info", "og-missing", f"missing {k}")
    if not doc.jsonld_types:
        add("info", "jsonld-missing", "no JSON-LD structured data")
    if doc.jsonld_errors:
        add("warning", "jsonld-invalid", f"{doc.jsonld_errors} JSON-LD block(s) failed to parse")
    if alt_missing:
        add("warning", "img-alt-missing", f"{alt_missing} image(s) without alt attribute")
    if doc.hreflang and not any(h["href"].rstrip("/") == res.final_url.rstrip("/") for h in doc.hreflang):
        add("info", "hreflang-no-self", "hreflang set has no self-reference")
    item["issues"] = issues
    item["score"] = max(0, 100 - sum(_PENALTY[i["severity"]] for i in issues))
    return item


class SeoAudit(Actor):
    name = "seo-audit"
    title = "SEO Audit"
    description = "On-page SEO audit per URL plus robots.txt, sitemap and llms.txt presence."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxPages": {"type": "integer", "default": 25, "minimum": 1, "maximum": 5000},
            "maxDepth": {"type": "integer", "default": 2, "minimum": 0, "maximum": 20},
            "includeGlobs": {"type": "array", "items": {"type": "string"}, "default": []},
            "excludeGlobs": {"type": "array", "items": {"type": "string"}, "default": []},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        crawler = ctx.crawler()
        counts = {}

        def on_page(res, req, doc):
            if res.blocked:
                ctx.record_failure(req["url"], res.error)
                return
            item = audit_page(res, doc)
            for i in item["issues"]:
                counts[i["code"]] = counts.get(i["code"], 0) + 1
            ctx.push_data(item)

        crawler.crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"],
                      max_depth=inp["maxDepth"], include=inp["includeGlobs"] or None,
                      exclude=inp["excludeGlobs"] or None)
        sites = {}
        for origin in dict.fromkeys(origin_of(u) for u in inp["startUrls"]):
            ctx.check_abort()
            entry = crawler.robots.get(origin + "/")
            sm = crawler.fetch(origin + "/sitemap.xml", accept_types=None, read_body=False)
            llms = crawler.fetch(origin + "/llms.txt", accept_types=None, read_body=False)
            sites[origin] = {
                "robotsTxt": entry.status,
                "robotsSitemaps": entry.sitemaps,
                "sitemapXml": sm.status == 200,
                "llmsTxt": llms.status == 200 and not llms.content_type.startswith("text/html"),
            }
        ctx.set_value("OUTPUT", {"sites": sites, "issueCounts": counts,
                                 "pages": ctx.dataset.count()})
