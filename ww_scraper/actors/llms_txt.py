"""llms-txt: check /llms.txt and /llms-full.txt per origin against the llmstxt.org format,
or (mode=generate) crawl the site and draft an llms.txt, saved in the run's key-value
store under 'llms.txt'.

Format (llmstxt.org): an H1 with the project name (required), an optional blockquote
summary, optional free text, then H2 sections holding Markdown link lists
"- [name](url): notes". A section named "Optional" marks skippable links.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..actor import MIN_INTERVAL, REDACT, START_URLS, Actor
from ..compliance import origin_of
from ..storage import MemoryRequestQueue

TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown")
_LINK = re.compile(r"^[-*+]\s+\[([^\]]+)\]\(([^)\s]+)\)(?::\s*(.*))?$")


def validate_llms_txt(text: str) -> dict:
    """Pure function: parse + validate llms.txt content."""
    issues = []

    def add(sev, msg):
        issues.append({"severity": sev, "message": msg})

    out = {"title": "", "summary": "", "sections": [], "linkCount": 0, "issues": issues}
    if text.lstrip()[:1] == "<":
        add("error", "content looks like HTML, not Markdown (soft 404 / SPA fallback?)")
        out["valid"] = False
        return out
    lines = text.splitlines()
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty or not re.match(r"^#\s+\S", nonempty[0]):
        add("error", "first line must be an H1 ('# Project name')")
    h1s = [ln for ln in lines if re.match(r"^#\s+\S", ln)]
    if h1s:
        out["title"] = h1s[0][1:].strip()
    if len(h1s) > 1:
        add("warning", f"{len(h1s)} H1 headings (expected exactly one)")
    quote = []
    seen_h1 = False
    for ln in lines:
        if re.match(r"^#\s", ln):
            seen_h1 = True
            continue
        if not seen_h1:
            continue
        if ln.startswith(">"):
            quote.append(ln.lstrip("> ").strip())
        elif quote or ln.startswith("#"):
            break
    out["summary"] = " ".join(quote)
    if not out["summary"]:
        add("info", "no blockquote summary after the H1")
    current = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("## "):
            current = {"name": s[3:].strip(), "links": [], "optional": s[3:].strip().lower() == "optional"}
            out["sections"].append(current)
            continue
        if current is None or not s:
            continue
        if s[:1] in "-*+":
            m = _LINK.match(s)
            if not m:
                add("warning", f"list item is not a link entry: {s[:80]}")
                continue
            name, url, notes = m.group(1), m.group(2), (m.group(3) or "").strip()
            if urlparse(url).scheme not in ("http", "https") and not url.startswith("/"):
                add("warning", f"link URL is not absolute http(s): {url[:80]}")
            current["links"].append({"name": name, "url": url, "notes": notes})
    for sec in out["sections"]:
        if not sec["links"]:
            add("warning", f"section '{sec['name']}' has no links")
    out["linkCount"] = sum(len(s["links"]) for s in out["sections"])
    if not out["sections"]:
        add("info", "no H2 sections with link lists")
    out["valid"] = not any(i["severity"] == "error" for i in issues)
    return out


def build_llms_txt(site_title: str, summary: str, pages: list) -> str:
    """Draft llms.txt from crawled pages [{url, title, description}], grouped by the first
    path segment."""
    groups: dict = {}
    for p in pages:
        seg = urlparse(p["url"]).path.strip("/").split("/")[0]
        groups.setdefault(seg, []).append(p)
    lines = [f"# {site_title or 'Site'}", ""]
    if summary:
        lines += [f"> {summary}", ""]
    for seg in sorted(groups, key=lambda s: (s != "", s)):
        lines.append(f"## {seg.replace('-', ' ').title() if seg else 'Main'}")
        lines.append("")
        for p in groups[seg]:
            entry = f"- [{(p['title'] or p['url']).replace(']', ')')}]({p['url']})"
            if p.get("description"):
                entry += f": {p['description']}"
            lines.append(entry)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class LlmsTxt(Actor):
    name = "llms-txt"
    title = "llms.txt Checker / Generator"
    description = "Validate /llms.txt and /llms-full.txt (llmstxt.org) or draft one from a crawl."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "mode": {"type": "string", "enum": ["check", "generate"], "default": "check"},
            "maxPages": {"type": "integer", "default": 30, "minimum": 1, "maximum": 1000},
            "maxDepth": {"type": "integer", "default": 2, "minimum": 0, "maximum": 10},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        crawler = ctx.crawler(max_bytes=10 * 1024 * 1024)
        origins = list(dict.fromkeys(origin_of(u) for u in ctx.input["startUrls"]))
        if ctx.input["mode"] == "generate":
            return self._generate(ctx, crawler, origins)
        for origin in origins:
            ctx.check_abort()
            item = {"origin": origin}
            res = crawler.fetch(origin + "/llms.txt", accept_types=None)
            html_soft404 = res.content_type.startswith("text/html")
            item["llmsTxt"] = {"url": origin + "/llms.txt", "status": res.status,
                               "contentType": res.content_type,
                               "present": res.ok and not html_soft404}
            if res.blocked:
                item["llmsTxt"]["error"] = res.error
            if res.ok:
                item["llmsTxt"].update(validate_llms_txt(res.text()))
            full = crawler.fetch(origin + "/llms-full.txt", accept_types=None)
            item["llmsFullTxt"] = {"url": origin + "/llms-full.txt", "status": full.status,
                                   "present": full.ok and not full.content_type.startswith("text/html"),
                                   "bytes": len(full.body) if full.ok else 0,
                                   "words": len(full.text().split()) if full.ok else 0}
            ctx.push_data(item)

    def _generate(self, ctx, crawler, origins):
        for origin in origins:
            pages, home = [], {}

            def on_page(res, req, doc):
                if doc is None or doc.noindex:
                    return
                entry = {"url": res.final_url, "title": doc.title, "description": doc.description}
                if req.get("depth", 0) == 0 and not home:
                    home.update(entry, site=doc.site_name)
                pages.append(entry)

            starts = [u for u in ctx.input["startUrls"] if origin_of(u) == origin]
            crawler.crawl(starts, on_page, max_pages=ctx.input["maxPages"],
                          max_depth=ctx.input["maxDepth"], queue=MemoryRequestQueue())
            text = build_llms_txt(home.get("site") or home.get("title", ""),
                                  home.get("description", ""), pages)
            text = ctx.redact_text(text)
            key = "llms.txt" if len(origins) == 1 else f"llms-{urlparse(origin).hostname}.txt"
            ctx.set_value(key, text, "text/markdown; charset=utf-8")
            ctx.push_data({"origin": origin, "mode": "generate", "pages": len(pages),
                           "key": key, "llmsTxt": text, "check": validate_llms_txt(text)})
