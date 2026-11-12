"""product-catalog: products, prices and availability from any online shop that publishes
schema.org Product data (most do, because search engines show it as rich results).

No per-shop selectors to write or to break after a redesign: the actor reads the shop's own
machine-readable data (JSON-LD, including ProductGroup variants and AggregateOffer price
ranges, with Open Graph product tags as a fallback), normalises it, removes duplicates by
GTIN, SKU or URL, and flags records a merchant feed would reject (no price, no currency,
no availability, no image).
"""
from __future__ import annotations

from ..actor import CONCURRENCY, MIN_INTERVAL, REDACT, RENDER, START_URLS, Actor
from ._api import first, html_to_text
from .structured_data import flatten_jsonld

GTIN_KEYS = ("gtin", "gtin13", "gtin12", "gtin14", "gtin8", "isbn")


def _types(o):
    t = o.get("@type")
    return [x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def _name(v):
    if isinstance(v, dict):
        return v.get("name")
    if isinstance(v, list):
        return _name(v[0]) if v else None
    return v if isinstance(v, str) else None


def _url(v):
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl") or v.get("@id")
    return v if isinstance(v, str) else None


def _enum(v):
    """https://schema.org/InStock -> InStock"""
    v = _name(v) if not isinstance(v, str) else v
    return v.rstrip("/").rsplit("/", 1)[-1] if isinstance(v, str) and v else None


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(" ", "").replace(" ", "")
        if s.count(",") == 1 and "." not in s:
            s = s.replace(",", ".")                      # 49,99
        elif s.count(",") >= 1 and "." in s:
            s = s.replace(",", "")                       # 1,299.00
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _offer(offers):
    """(price, lowPrice, highPrice, currency, availability, condition, priceValidUntil, sellers)"""
    if isinstance(offers, dict) and "AggregateOffer" in _types(offers):
        return (None, _num(offers.get("lowPrice")), _num(offers.get("highPrice")),
                offers.get("priceCurrency"), _enum(offers.get("availability")), None, None,
                offers.get("offerCount"))
    items = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
    items = [o for o in items if isinstance(o, dict)]
    if not items:
        return (None,) * 8
    prices = []
    for o in items:
        spec = o.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        p = _num(first(o.get("price"), (spec or {}).get("price") if isinstance(spec, dict) else None))
        prices.append((p, o))
    known = [x for x in prices if x[0] is not None]
    p, o = min(known, key=lambda x: x[0]) if known else prices[0]
    spec = o.get("priceSpecification") if isinstance(o.get("priceSpecification"), dict) else {}
    lo = min(x[0] for x in known) if len(known) > 1 else None
    hi = max(x[0] for x in known) if len(known) > 1 else None
    return (p, lo, hi, first(o.get("priceCurrency"), spec.get("priceCurrency")), _enum(o.get("availability")),
            _enum(o.get("itemCondition")), o.get("priceValidUntil"), len(items))


def product(obj: dict, page_url: str, parent: dict | None = None) -> dict:
    base = dict(parent or {}, **obj)
    price, lo, hi, cur, avail, cond, valid, offers = _offer(obj.get("offers") or (parent or {}).get("offers"))
    rating = base.get("aggregateRating") if isinstance(base.get("aggregateRating"), dict) else {}
    gtin = next((str(base[k]) for k in GTIN_KEYS if base.get(k)), None)
    rec = {
        "name": _name(base.get("name")), "url": _url(obj.get("url")) or page_url, "pageUrl": page_url,
        "sku": base.get("sku") if isinstance(base.get("sku"), (str, int)) else None, "gtin": gtin,
        "mpn": base.get("mpn") if isinstance(base.get("mpn"), str) else None,
        "brand": _name(base.get("brand")), "category": _name(base.get("category")),
        "price": price, "lowPrice": lo, "highPrice": hi, "currency": cur, "availability": avail,
        "condition": cond, "priceValidUntil": valid, "offerCount": offers,
        "image": _url(base.get("image")),
        "rating": _num(rating.get("ratingValue")), "reviewCount": _num(first(rating.get("reviewCount"),
                                                                          rating.get("ratingCount"))),
        "color": base.get("color") if isinstance(base.get("color"), str) else None,
        "size": _name(base.get("size")),
        "description": html_to_text(base.get("description") or "", limit=1000) or None,
        "variantOf": _name(parent.get("name")) if parent else None,
    }
    if rec["sku"] is not None:
        rec["sku"] = str(rec["sku"])
    issues = []
    if rec["price"] is None and rec["lowPrice"] is None:
        issues.append("no price")
    if (rec["price"] is not None or rec["lowPrice"] is not None) and not rec["currency"]:
        issues.append("price without currency")
    if not rec["availability"]:
        issues.append("no availability")
    if not rec["image"]:
        issues.append("no image")
    if not (rec["gtin"] or rec["mpn"]) and rec["brand"]:
        issues.append("brand without GTIN or MPN")
    rec["issues"] = issues
    return rec


def products_on_page(doc, page_url: str) -> list:
    out = []
    for obj in flatten_jsonld(doc.jsonld):
        types = _types(obj)
        if "ProductGroup" in types:
            variants = obj.get("hasVariant")
            variants = variants if isinstance(variants, list) else [variants] if isinstance(variants, dict) else []
            parent = {k: v for k, v in obj.items() if k not in ("hasVariant", "@type", "@id")}
            out += [product(v, page_url, parent) for v in variants if isinstance(v, dict)] or [product(obj, page_url)]
        elif "Product" in types or "IndividualProduct" in types:
            out.append(product(obj, page_url))
    if not out and doc.og.get("og:type", "").startswith("product"):
        og = doc.og
        out.append(product({"name": og.get("og:title"), "image": og.get("og:image"), "offers": {
            "price": first(og.get("product:price:amount"), og.get("og:price:amount")),
            "priceCurrency": first(og.get("product:price:currency"), og.get("og:price:currency")),
            "availability": og.get("product:availability")}}, page_url))
    return out


class ProductCatalog(Actor):
    name = "product-catalog"
    title = "Product Catalog"
    description = ("Products, prices and availability from any shop that publishes schema.org data: "
                   "no per-shop selectors, duplicates removed, feed problems flagged.")
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxPages": {"type": "integer", "default": 200, "minimum": 1, "maximum": 50000},
            "maxDepth": {"type": "integer", "default": 3, "minimum": 0, "maximum": 20},
            "includeGlobs": {"type": "array", "items": {"type": "string"}, "default": [],
                             "description": "Follow only links matching one of these globs (e.g. */produkt/*)"},
            "excludeGlobs": {"type": "array", "items": {"type": "string"},
                             "default": ["*/cart*", "*/koszyk*", "*/checkout*", "*/login*", "*/konto*",
                                         "*/account*", "*?*sort=*", "*?*filter*"]},
            "minIntervalSecs": MIN_INTERVAL,
            "maxConcurrency": CONCURRENCY,
            "render": RENDER,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        seen, summary = set(), {"pages": 0, "pagesWithProducts": 0, "products": 0, "duplicates": 0,
                                "withIssues": 0, "currencies": {}, "availability": {}}

        def on_page(res, req, doc):
            if doc is None:
                if res.error and not res.duplicate:
                    ctx.record_failure(req["url"], res.error)
                return
            summary["pages"] += 1
            items = []
            for p in products_on_page(doc, res.final_url or req["url"]):
                key = p["gtin"] or (f"sku:{p['sku']}:{p['color']}:{p['size']}" if p["sku"] else None) or p["url"]
                if key in seen:
                    summary["duplicates"] += 1
                    continue
                seen.add(key)
                items.append(p)
                if p["currency"]:
                    summary["currencies"][p["currency"]] = summary["currencies"].get(p["currency"], 0) + 1
                if p["availability"]:
                    summary["availability"][p["availability"]] = summary["availability"].get(p["availability"], 0) + 1
                summary["withIssues"] += bool(p["issues"])
            if items:
                summary["pagesWithProducts"] += 1
                summary["products"] += len(items)
                ctx.push_data(items)

        ctx.crawler().crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"], max_depth=inp["maxDepth"],
                            include=inp["includeGlobs"] or None, exclude=inp["excludeGlobs"] or None)
        ctx.set_value("OUTPUT", summary)
