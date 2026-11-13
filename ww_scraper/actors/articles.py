"""articles: news and blog articles a site publishes as schema.org data (Article,
NewsArticle, BlogPosting and their subtypes, the markup news search and readers rely on),
with Open Graph article tags as a fallback. One item per article: headline, authors as
the byline names them, publisher, dates, section, keywords, language and image; the body
as Markdown only when asked for. Records missing what article rich results need are
flagged in `issues`.
"""
from __future__ import annotations

from ..actor import (AUTOSCALE, BROWSER_ACTIONS, CONCURRENCY, MIN_INTERVAL, REDACT, RENDER,
                     START_URLS, WAIT_FOR, Actor)
from ..schema import STR_OR_NULL
from ._api import first, html_to_text
from .product_catalog import _name, _types, _url
from .structured_data import flatten_jsonld

ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "Report", "ScholarlyArticle", "TechArticle",
                 "AnalysisNewsArticle", "OpinionNewsArticle", "ReportageNewsArticle", "ReviewNewsArticle",
                 "BackgroundNewsArticle", "AskPublicNewsArticle", "LiveBlogPosting", "SatiricalArticle"}
DATE = {"type": "string", "editor": "datepicker", "default": "",
        "description": "Only articles published on or after this day (YYYY-MM-DD)"}


def _names(v) -> list:
    items = v if isinstance(v, list) else [v] if v else []
    out = []
    for x in items:
        n = _name(x)
        if isinstance(n, str) and n.strip() and n.strip() not in out:
            out.append(n.strip())
    return out


def _keywords(v) -> list:
    if isinstance(v, str):
        return [k.strip() for k in v.split(",") if k.strip()]
    if isinstance(v, list):
        return [k.strip() for k in v if isinstance(k, str) and k.strip()]
    return []


def _int(v):
    try:
        return int(v) if v not in (None, "") and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def article(obj: dict, page_url: str, lang=None) -> dict:
    types = [t for t in _types(obj) if t in ARTICLE_TYPES]
    section = obj.get("articleSection")
    rec = {
        "url": first(_url(obj.get("url")), _url(obj.get("mainEntityOfPage")), page_url),
        "pageUrl": page_url,
        "type": types[0] if types else "Article",
        "headline": first(obj.get("headline"), _name(obj.get("name"))),
        "description": html_to_text(obj.get("description") or "", limit=1000) or None,
        "authors": _names(obj.get("author")),
        "publisher": _name(obj.get("publisher")),
        "datePublished": obj.get("datePublished") if isinstance(obj.get("datePublished"), str) else None,
        "dateModified": obj.get("dateModified") if isinstance(obj.get("dateModified"), str) else None,
        "section": ", ".join(s for s in section if isinstance(s, str)) if isinstance(section, list)
        else section if isinstance(section, str) else None,
        "keywords": _keywords(obj.get("keywords")),
        "language": first(obj.get("inLanguage") if isinstance(obj.get("inLanguage"), str) else None, lang),
        "image": _url(obj.get("image")),
        "wordCount": _int(obj.get("wordCount")),
        "isAccessibleForFree": obj.get("isAccessibleForFree") if isinstance(obj.get("isAccessibleForFree"), bool)
        else None,
        "source": "json-ld",
    }
    return rec


def _from_og(doc, page_url) -> dict | None:
    og = doc.og or {}
    if og.get("og:type") != "article":
        return None
    author = og.get("article:author")
    return {"url": first(og.get("og:url"), page_url), "pageUrl": page_url, "type": "Article",
            "headline": first(og.get("og:title"), doc.title), "description": og.get("og:description"),
            "authors": [author] if isinstance(author, str) and not author.startswith("http") else [],
            "publisher": og.get("og:site_name"), "datePublished": og.get("article:published_time"),
            "dateModified": og.get("article:modified_time"), "section": og.get("article:section"),
            "keywords": _keywords(og.get("article:tag")), "language": doc.lang, "image": og.get("og:image"),
            "wordCount": None, "isAccessibleForFree": None, "source": "open-graph"}


def _issues(rec) -> list:
    out = []
    for key, what in (("headline", "headline"), ("datePublished", "datePublished"), ("image", "image")):
        if not rec[key]:
            out.append(f"no {what}")
    if not rec["authors"]:
        out.append("no author")
    if rec["headline"] and len(rec["headline"]) > 110:
        out.append("headline longer than 110 characters")
    return out


def articles_on_page(doc, page_url) -> list:
    found = [article(o, page_url, doc.lang) for o in flatten_jsonld(doc.jsonld)
             if ARTICLE_TYPES.intersection(_types(o))]
    if not found:
        og = _from_og(doc, page_url)
        found = [og] if og else []
    for rec in found:
        rec["issues"] = _issues(rec)
    return found


class Articles(Actor):
    name = "articles"
    version = "1.0.0"
    title = "Articles"
    description = ("News and blog articles from the schema.org data a site publishes: headline, byline, "
                   "dates, section, keywords; optional body as Markdown.")
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxPages": {"type": "integer", "default": 100, "minimum": 1, "maximum": 20000},
            "maxDepth": {"type": "integer", "default": 2, "minimum": 0, "maximum": 20},
            "publishedAfter": DATE,
            "includeText": {"type": "boolean", "default": False,
                            "description": "Also store the page's main content as Markdown"},
            "includeGlobs": {"type": "array", "items": {"type": "string"}, "default": []},
            "excludeGlobs": {"type": "array", "items": {"type": "string"},
                             "default": ["*/tag/*", "*/author/*", "*/login*", "*/account*", "*?*replytocom=*"]},
            "minIntervalSecs": MIN_INTERVAL,
            "maxConcurrency": CONCURRENCY,
            "autoscaleConcurrency": AUTOSCALE,
            "render": RENDER,
            "waitForSelector": WAIT_FOR,
            "browserActions": BROWSER_ACTIONS,
            "redactPii": REDACT,
        },
    }
    dataset_schema = {
        "fields": {"type": "object", "required": ["url", "headline", "authors", "issues"],
                   "properties": {"url": {"type": "string"}, "headline": STR_OR_NULL,
                                  "authors": {"type": "array", "items": {"type": "string"}},
                                  "datePublished": STR_OR_NULL, "issues": {"type": "array", "items": {"type": "string"}}}},
        "views": {"overview": {"title": "Overview", "fields": ["datePublished", "headline", "authors", "section", "url"],
                               "desc": True}},
    }

    def run(self, ctx):
        inp = ctx.input
        after = inp["publishedAfter"]
        seen = set()
        summary = {"pages": 0, "articles": 0, "skippedOlder": 0, "duplicates": 0, "withIssues": 0, "types": {}}

        def on_page(res, req, doc):
            if doc is None:
                if res.error and not res.duplicate:
                    ctx.record_failure(req["url"], res.error)
                return
            summary["pages"] += 1
            page = res.final_url or req["url"]
            out = []
            for rec in articles_on_page(doc, page):
                # the same article reached under another URL (tracking parameters, AMP copy)
                keys = {("url", rec["url"]), ("url", doc.canonical or rec["url"])}
                if rec["headline"] and rec["datePublished"]:
                    keys.add(("text", rec["headline"], rec["datePublished"]))
                if keys & seen:
                    summary["duplicates"] += 1
                    continue
                seen.update(keys)
                if after and (rec["datePublished"] or "")[:10] < after:
                    summary["skippedOlder"] += 1
                    continue
                if rec["description"]:
                    rec["description"] = ctx.redact_text(rec["description"])
                if inp["includeText"]:
                    rec["markdown"] = ctx.redact_text(doc.markdown)
                summary["types"][rec["type"]] = summary["types"].get(rec["type"], 0) + 1
                summary["withIssues"] += bool(rec["issues"])
                out.append(rec)
            if out:
                summary["articles"] += len(out)
                ctx.push_data(out)
            ctx.set_status_message(f"{summary['articles']} articles from {summary['pages']} pages")

        ctx.crawler().crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"], max_depth=inp["maxDepth"],
                            include=inp["includeGlobs"] or None, exclude=inp["excludeGlobs"] or None)
        ctx.set_value("OUTPUT", summary)
