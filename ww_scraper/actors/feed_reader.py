"""feed-reader: find the RSS / Atom feeds a page advertises (<link rel="alternate">) or
read feed URLs given directly, and output their entries. Uses the same linear tag scanner
as the sitemap actor: no XML parser, DOCTYPE/ENTITY declarations refused."""
from __future__ import annotations

from ..actor import MIN_INTERVAL, REDACT, START_URLS, Actor
from ..extract import parse_html
from .sitemap import SitemapError, _tokens, _unescape

FEED_CONTENT_TYPES = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml",
                      "text/html", "application/xhtml+xml")
MAX_FEED_BYTES = 10 * 1024 * 1024
_ITEM_FIELDS = {"title": "title", "link": "link", "guid": "id", "id": "id", "pubdate": "published",
                "published": "published", "updated": "updated", "description": "summary",
                "summary": "summary", "date": "published"}


class FeedError(ValueError):
    pass


def parse_feed(data: bytes):
    """Return (kind, title, entries); kind is 'rss' or 'atom'. Each entry is a dict with
    title, link, id, published, updated, summary (those that are present)."""
    if len(data) > MAX_FEED_BYTES:
        raise FeedError("feed exceeds 10 MB")
    upper = data[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in data.upper():
        raise FeedError("DOCTYPE/ENTITY declarations are not allowed")
    kind, feed_title, entries = None, "", []
    entry, field, buf = None, None, []
    depth_in_entry = 0
    for typ, val in _tokens(data.decode("utf-8", "replace")):
        if typ != "tag":
            if field is not None:
                buf.append(val if typ == "cdata" else _unescape(val))
            continue
        if not val or val[0] in "?!":
            continue
        closing = val.startswith("/")
        body = val[1:] if closing else val
        parts = body.split(None, 1)
        if not parts:
            continue
        name = parts[0].rstrip("/").rsplit(":", 1)[-1].lower()
        selfclose = body.endswith("/")
        if kind is None:
            if closing:
                continue
            if name in ("rss", "rdf"):
                kind = "rss"
            elif name == "feed":
                kind = "atom"
            else:
                raise FeedError(f"not a feed (root element <{name}>)")
            continue
        item_tag = "item" if kind == "rss" else "entry"
        if name == item_tag:
            if closing:
                if entry:
                    entries.append(entry)
                entry, field = None, None
            elif not selfclose:
                entry, field, buf, depth_in_entry = {}, None, [], 0
            continue
        if closing:
            if field is not None and name == field[0]:
                text = " ".join("".join(buf).split())
                target = entry if entry is not None else None
                if target is not None:
                    target.setdefault(field[1], text[:2000])
                elif field[1] == "title" and not feed_title:
                    feed_title = text[:300]
                field, buf = None, []
            continue
        if name == "link" and kind == "atom" and entry is not None:
            attrs = parts[1] if len(parts) > 1 else ""
            href = _attr(attrs, "href")
            rel = _attr(attrs, "rel") or "alternate"
            if href and rel == "alternate":
                entry.setdefault("link", _unescape(href))
            continue
        if selfclose or field is not None:
            continue
        if name in _ITEM_FIELDS and (entry is not None or name == "title"):
            field, buf = (name, _ITEM_FIELDS[name]), []
    if kind is None:
        raise FeedError("not a feed (no root element)")
    return kind, feed_title, entries


def _attr(attrs: str, key: str) -> str:
    """Value of key="..." in a tag's attribute text (plain string search, no regex)."""
    for quote in ('"', "'"):
        marker = f"{key}={quote}"
        i = attrs.find(marker)
        while i > 0 and not attrs[i - 1].isspace():
            i = attrs.find(marker, i + 1)
        if i >= 0:
            end = attrs.find(quote, i + len(marker))
            if end > 0:
                return attrs[i + len(marker):end]
    return ""


class FeedReader(Actor):
    name = "feed-reader"
    title = "Feed Reader"
    description = "Discover RSS / Atom feeds and output their entries."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxFeeds": {"type": "integer", "default": 10, "minimum": 1, "maximum": 200},
            "maxEntriesPerFeed": {"type": "integer", "default": 100, "minimum": 1, "maximum": 5000},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        crawler = ctx.crawler()
        feeds: list = []
        summary = {"feeds": 0, "entries": 0, "invalid": 0}

        def read(url, body):
            try:
                kind, title, entries = parse_feed(body)
            except (FeedError, SitemapError) as e:
                summary["invalid"] += 1
                ctx.record_failure(url, f"feed: {e}")
                return
            summary["feeds"] += 1
            for e in entries[: inp["maxEntriesPerFeed"]]:
                summary["entries"] += 1
                ctx.push_data(dict(e, feedUrl=url, feedTitle=title, feedType=kind))

        for start in inp["startUrls"]:
            ctx.check_abort()
            res = crawler.fetch(start, accept_types=FEED_CONTENT_TYPES, max_bytes=MAX_FEED_BYTES)
            if not res.ok:
                ctx.record_failure(start, res.error or f"HTTP {res.status}")
                continue
            if res.is_html:
                for f in parse_html(res.text(), res.final_url or start).feeds:
                    if f["url"] not in feeds:
                        feeds.append(f["url"])
            else:
                read(res.final_url or start, res.body)
        for url in feeds[: inp["maxFeeds"]]:
            ctx.check_abort()
            res = crawler.fetch(url, accept_types=FEED_CONTENT_TYPES, max_bytes=MAX_FEED_BYTES)
            if not res.ok or res.is_html:
                ctx.record_failure(url, res.error or f"HTTP {res.status} ({res.content_type})")
                continue
            read(res.final_url or url, res.body)
        ctx.set_value("OUTPUT", summary)
