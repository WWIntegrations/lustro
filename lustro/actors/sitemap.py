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
_ENT_RE = re.compile(r"&(#x[0-9a-fA-F]+|#\d+|amp|lt|gt|quot|apos);")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_P = r"(?:[\w.-]+:)?"  # optional namespace prefix


def _unescape(s: str) -> str:
    def sub(m):
        e = m.group(1)
        if e.startswith("#x"):
            return chr(int(e[2:], 16))
        if e.startswith("#"):
            return chr(int(e[1:]))
        return _ENTITIES[e]
    s = _CDATA_RE.sub(lambda m: m.group(1), s)
    return _ENT_RE.sub(sub, s).strip()


def _blocks(xml: str, tag: str):
    return re.findall(rf"<{_P}{tag}(?:\s[^>]*)?>(.*?)</{_P}{tag}\s*>", xml, re.S)


def _field(block: str, tag: str) -> str:
    m = re.search(rf"<{_P}{tag}(?:\s[^>]*)?>(.*?)</{_P}{tag}\s*>", block, re.S)
    return _unescape(m.group(1)) if m else ""


def parse_sitemap(data: bytes):
    """Return (kind, entries): kind 'index' -> list of sitemap URLs,
    kind 'urlset' -> list of dicts {url, lastmod, changefreq, priority}.

    A deliberately small, non-validating reader for the fixed sitemap vocabulary: no
    XML parser, no DTD processing, so XXE and entity expansion cannot happen. Documents
    with DOCTYPE/ENTITY declarations are refused outright."""
    if data[:2] == b"\x1f\x8b":
        data = _gunzip(data)
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"<"):
        urls = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines()]
        return "urlset", [{"url": u} for u in urls if u.startswith(("http://", "https://"))]
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SitemapError("DOCTYPE/ENTITY declarations are not allowed")
    xml = _COMMENT_RE.sub("", data.decode("utf-8", "replace"))
    if re.search(rf"<{_P}sitemapindex[\s>]", xml):
        locs = [_field(b, "loc") for b in _blocks(xml, "sitemap")]
        return "index", [u for u in locs if u]
    if re.search(rf"<{_P}urlset[\s>]", xml):
        out = []
        for b in _blocks(xml, "url"):
            loc = _field(b, "loc")
            if loc:
                out.append({"url": loc, "lastmod": _field(b, "lastmod") or None,
                            "changefreq": _field(b, "changefreq") or None,
                            "priority": _field(b, "priority") or None})
        return "urlset", out
    raise SitemapError("not a sitemap (no <urlset> or <sitemapindex>)")


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
