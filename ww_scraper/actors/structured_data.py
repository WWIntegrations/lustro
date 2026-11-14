"""structured-data: per page, the machine-readable data a site publishes about itself:
JSON-LD objects (flattened out of @graph), Open Graph and Twitter card tags. Reports the
JSON-LD blocks that failed to parse and the schema.org types missing a name."""
from __future__ import annotations

from ..actor import (AUTOSCALE, BROWSER_ACTIONS, CONCURRENCY, MIN_INTERVAL, REDACT, RENDER,
                     START_URLS, WAIT_FOR, Actor)

MAX_OBJECTS_PER_PAGE = 100


def flatten_jsonld(blocks) -> list:
    """Top-level objects of every block, with @graph members lifted out."""
    out = []

    def walk(obj):
        if len(out) >= MAX_OBJECTS_PER_PAGE:
            return
        if isinstance(obj, list):
            for o in obj:
                walk(o)
        elif isinstance(obj, dict):
            graph = obj.get("@graph")
            if isinstance(graph, list):
                walk(graph)
                rest = {k: v for k, v in obj.items() if k not in ("@graph", "@context")}
                if rest.get("@type"):
                    out.append(rest)
            else:
                out.append({k: v for k, v in obj.items() if k != "@context"})

    walk(blocks)
    return out


def _types(obj) -> list:
    t = obj.get("@type")
    return [x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


class StructuredData(Actor):
    name = "structured-data"
    title = "Structured Data Extractor"
    description = "Extract JSON-LD, microdata, Open Graph and Twitter card data from each page."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxPages": {"type": "integer", "default": 50, "minimum": 1, "maximum": 10000},
            "maxDepth": {"type": "integer", "default": 2, "minimum": 0, "maximum": 20},
            "onlyWithData": {"type": "boolean", "default": False,
                             "description": "Skip pages that publish no structured data"},
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
        summary = {"pages": 0, "withJsonLd": 0, "withMicrodata": 0, "jsonLdErrors": 0, "types": {}}

        def on_page(res, req, doc):
            if doc is None:
                if res.error or res.status >= 400:
                    ctx.record_failure(req["url"], res.error or f"HTTP {res.status}")
                return
            objects = flatten_jsonld(doc.jsonld)
            issues = []
            for o in objects:
                for t in _types(o):
                    summary["types"][t] = summary["types"].get(t, 0) + 1
                if _types(o) and not (o.get("name") or o.get("headline") or o.get("@id")):
                    issues.append(f"{'/'.join(_types(o))}: no name, headline or @id")
            if doc.jsonld_errors:
                issues.append(f"{doc.jsonld_errors} JSON-LD block(s) are not valid JSON")
            summary["pages"] += 1
            summary["withJsonLd"] += bool(objects)
            summary["withMicrodata"] += bool(doc.microdata)
            summary["jsonLdErrors"] += doc.jsonld_errors
            if inp["onlyWithData"] and not (objects or doc.microdata or doc.og or doc.twitter):
                return
            ctx.push_data({"url": req["url"], "finalUrl": res.final_url, "title": doc.title,
                           "jsonLd": objects, "jsonLdTypes": doc.jsonld_types, "microdata": doc.microdata,
                           "openGraph": doc.og, "twitter": doc.twitter, "issues": issues})

        ctx.crawler().crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"],
                            max_depth=inp["maxDepth"])
        ctx.set_value("OUTPUT", summary)
