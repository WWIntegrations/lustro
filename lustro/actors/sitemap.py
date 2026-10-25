"""sitemap: discover sitemaps (robots.txt Sitemap: lines, then /sitemap.xml), follow
sitemap indexes, and output every listed URL with lastmod/changefreq/priority.
Supports XML, gzip-compressed XML and plain-text sitemaps. No XML parser is used and
documents with DOCTYPE/ENTITY declarations are refused (no XXE, no entity expansion)."""
from __future__ import annotations

import collections
import re
import zlib

from ..actor import MIN_INTERVAL, REDACT, START_URLS, Actor
from ..compliance import origin_of

SITEMAP_TYPES = ("application/xml", "text/xml", "application/x-gzip", "application/gzip",
                 "application/octet-stream", "text/plain", "application/rss+xml")
MAX_UNCOMPRESSED = 50 * 1024 * 1024  # sitemap protocol limit


class SitemapError(ValueError):
    pass


def _gunzip(data: bytes) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, MAX_UNCOMPRESSED)
    if d.unconsumed_tail:
        raise SitemapError("decompressed sitemap exceeds 50 MB")
    return out


_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_ENT_RE = re.compile(r"&(#x[0-9a-fA-F]{1,6}|#[0-9]{1,7}|amp|lt|gt|quot|apos);")
_FIELDS = ("loc", "lastmod", "changefreq", "priority")


def _unescape(s: str) -> str:
    def sub(m):
        e = m.group(1)
        try:
            if e.startswith("#x"):
                return chr(int(e[2:], 16))
            if e.startswith("#"):
                return chr(int(e[1:]))
        except (ValueError, OverflowError):
            return ""
        return _ENTITIES[e]
    return _ENT_RE.sub(sub, s)


def _tokens(xml: str):
    """Single forward pass over tag boundaries with str.find (no backtracking regex):
    yields ('text'|'cdata'|'tag', value). Linear in the document size."""
    i, n = 0, len(xml)
    while i < n:
        lt = xml.find("<", i)
        if lt < 0:
            yield "text", xml[i:]
            return
        if lt > i:
            yield "text", xml[i:lt]
        if xml.startswith("<![CDATA[", lt):
            end = xml.find("]]>", lt + 9)
            if end < 0:
                return
            yield "cdata", xml[lt + 9:end]
            i = end + 3
            continue
        if xml.startswith("<!--", lt):
            end = xml.find("-->", lt + 4)
            if end < 0:
                return
            i = end + 3
            continue
        gt = xml.find(">", lt + 1)
        if gt < 0:
            return
        yield "tag", xml[lt + 1:gt]
        i = gt + 1


def parse_sitemap(data: bytes):
    """Return (kind, entries): kind 'index' -> list of sitemap URLs,
    kind 'urlset' -> list of dicts {url, lastmod, changefreq, priority}.

    A deliberately small, non-validating reader for the fixed sitemap vocabulary: no
    XML parser, no DTD processing (XXE and entity expansion cannot happen), and a
    single linear pass, so hostile input cannot cause super-linear work. Documents
    with DOCTYPE/ENTITY declarations are refused outright."""
    if data[:2] == b"\x1f\x8b":
        data = _gunzip(data)
    if len(data) > MAX_UNCOMPRESSED:
        raise SitemapError("sitemap exceeds 50 MB")
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"<"):
        urls = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines()]
        return "urlset", [{"url": u} for u in urls if u.startswith(("http://", "https://"))]
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SitemapError("DOCTYPE/ENTITY declarations are not allowed")
    kind = None
    entries = []
    entry = None      # dict while inside <url>/<sitemap>
    field = None      # current field name inside the entry
    buf: list = []
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
            if name in ("urlset", "sitemapindex") and not closing:
                kind = "index" if name == "sitemapindex" else "urlset"
                continue
            if not closing:
                raise SitemapError(f"not a sitemap (root element <{name}>)")
            continue
        item_tag = "sitemap" if kind == "index" else "url"
        if name == item_tag:
            if closing:
                if entry and entry.get("loc"):
                    entries.append(entry)
                entry, field = None, None
            elif not selfclose:
                entry, field, buf = {}, None, []
            continue
        if entry is None or name not in _FIELDS:
            continue
        if closing:
            if field == name:
                entry[name] = "".join(buf).strip()
            field = None
        elif not selfclose:
            field, buf = name, []
    if kind is None:
        raise SitemapError("not a sitemap (no <urlset> or <sitemapindex>)")
    if kind == "index":
        return "index", [e["loc"] for e in entries]
    return "urlset", [{"url": e["loc"], "lastmod": e.get("lastmod") or None,
                       "changefreq": e.get("changefreq") or None,
                       "priority": e.get("priority") or None} for e in entries]


class Sitemap(Actor):
    name = "sitemap"
    title = "Sitemap Extractor"
    description = "Find a site's sitemaps (robots.txt, /sitemap.xml, indexes) and list their URLs."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxSitemaps": {"type": "integer", "default": 50, "minimum": 1, "maximum": 1000},
            "maxUrls": {"type": "integer", "default": 10000, "minimum": 1, "maximum": 1_000_000},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        crawler = ctx.crawler(max_bytes=MAX_UNCOMPRESSED)
        summary = {}
        total = 0
        fetched = 0
        for origin in dict.fromkeys(origin_of(u) for u in inp["startUrls"]):
            entry = crawler.robots.get(origin + "/")
            todo = collections.deque(entry.sitemaps or [])
            todo.append(origin + "/sitemap.xml")
            seen, found, count, seen_urls = set(), [], 0, set()
            while todo and fetched < inp["maxSitemaps"] and total < inp["maxUrls"]:
                ctx.check_abort()
                sm = todo.popleft()
                if sm in seen:
                    continue
                seen.add(sm)
                res = crawler.fetch(sm, accept_types=SITEMAP_TYPES)
                fetched += 1
                if not res.ok:
                    if res.status != 404:
                        ctx.record_failure(sm, res.error or f"HTTP {res.status}")
                    continue
                try:
                    kind, entries = parse_sitemap(res.body)
                except SitemapError as e:
                    ctx.record_failure(sm, str(e))
                    continue
                found.append({"url": sm, "kind": kind, "entries": len(entries)})
                if kind == "index":
                    todo.extend(e for e in entries if e not in seen)
                    continue
                batch = []
                for e in entries:
                    if total >= inp["maxUrls"]:
                        break
                    if e["url"] in seen_urls:
                        continue
                    seen_urls.add(e["url"])
                    batch.append(dict(e, sitemap=sm, origin=origin))
                    total += 1
                count += len(batch)
                ctx.push_data(batch)
            summary[origin] = {"robotsSitemaps": entry.sitemaps, "sitemaps": found, "urls": count}
        ctx.set_value("OUTPUT", summary)
