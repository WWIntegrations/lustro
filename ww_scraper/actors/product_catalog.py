"""product-catalog: products, prices and availability from any online shop that publishes
schema.org Product data (most do, because search engines show it as rich results).

No per-shop selectors to write or to break after a redesign: the actor reads the shop's own
machine-readable data (JSON-LD, including @graph nodes joined by @id, ProductGroup variants
and AggregateOffer price ranges; microdata when a page has no JSON-LD product; Open Graph
product tags as the last fallback), normalises it (prices in local formats such as
"1.299,00 zł", one schema.org name per availability, absolute image URLs), removes duplicates
by GTIN, SKU or URL, and flags records a merchant feed would reject (no price, no currency,
no availability, no image).
"""
from __future__ import annotations

import math
import re
from urllib.parse import urljoin, urlparse

from ..actor import (AUTOSCALE, BROWSER_ACTIONS, CONCURRENCY, MIN_INTERVAL, REDACT, RENDER,
                     START_URLS, WAIT_FOR, Actor)
from ..extract import _abs, _unescape_left
from ..schema import STR_OR_NULL
from ._api import first, html_to_text
from .structured_data import flatten_jsonld

GTIN_KEYS = ("gtin", "gtin13", "gtin12", "gtin14", "gtin8", "isbn")


def _types(o):
    t = o.get("@type")
    return [x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def _name(v):
    if isinstance(v, dict):
        v = v.get("name")
    elif isinstance(v, list):
        return _name(v[0]) if v else None
    return _text(v)


def _text(v):
    return (_unescape_left(v.strip()) or None) if isinstance(v, str) else None


def _category(v):
    """Category as text; some shops write it as HTML links to their category pages."""
    v = _name(v)
    return (html_to_text(v, limit=500) or None) if v and "<" in v else v


def _url(v, base=""):
    """First URL of a value (string, list, ImageObject), absolute against base; http(s) only."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl") or v.get("@id")
    if not isinstance(v, str) or not v.strip():
        return None
    v = _abs(base, v) if base else v.strip()
    return v if urlparse(v).scheme in ("http", "https") else None


# Availability as shops write it (schema.org URLs, bare names, Open Graph and merchant-feed
# words) -> one schema.org ItemAvailability name. Keyed on lower-case letters only.
_AVAILABILITY = {
    "instock": "InStock", "outofstock": "OutOfStock", "soldout": "SoldOut", "preorder": "PreOrder",
    "presale": "PreSale", "backorder": "BackOrder", "availablefororder": "BackOrder",
    "limitedavailability": "LimitedAvailability", "onlineonly": "OnlineOnly",
    "instoreonly": "InStoreOnly", "discontinued": "Discontinued", "madetoorder": "MadeToOrder",
    "reserved": "Reserved",
}


def _enum(v):
    """https://schema.org/InStock, InStock, "in stock", in_stock -> InStock; unknown names kept"""
    v = _name(v) if not isinstance(v, str) else v
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip().rstrip("/").rsplit("/", 1)[-1]
    return _AVAILABILITY.get(re.sub(r"[^a-z]", "", v.lower()), v)


_CURRENCY_EDGE = re.compile(r"^[^\d.,\-]{0,8}|[^\d.,]{0,8}$")    # "US$ ", " zł", " PLN", "€"
_GROUPING = re.compile(r"[\s  ']")                     # 1 299 / 1'299


def _num(v):
    """A price as a float. Strings may carry a currency sign or code and local separators:
    "1.299,00", "1 299,00 zł", "1,299.00", "$49.99". With both separators the last one is the
    decimal mark; a lone comma before exactly three digits groups thousands ("1,299");
    a lone dot is a decimal mark, as schema.org writes prices ("1.299" is 1.299)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if not isinstance(v, str) or len(v) > 64:
        return None
    s = _CURRENCY_EDGE.sub("", _GROUPING.sub("", v.strip()))
    if not re.fullmatch(r"-?[\d.,]*\d[\d.,]*", s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        sep = "." if dec == "," else ","
        if s.count(dec) != 1:
            return None
        whole, frac = s.split(dec)
        whole = _ungroup(whole, sep)
        s = None if whole is None else f"{whole}.{frac}"
    elif s.count(",") + s.count(".") > 1:
        s = _ungroup(s, "," if "," in s else ".")
    elif "," in s:
        whole, frac = s.split(",")
        s = whole + frac if len(frac) == 3 and whole.lstrip("-") else f"{whole}.{frac}"
    if s is None:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _ungroup(s, sep):
    """'12.345.678' -> '12345678' when every group after the first has three digits."""
    if sep not in s:
        return s
    head, *groups = s.split(sep)
    if not head.lstrip("-") or not all(len(g) == 3 and g.isdigit() for g in groups):
        return None
    return head + "".join(groups)


# priceType values that name a reference price, not what the buyer pays (schema.org
# PriceTypeEnumeration; Google reads StrikethroughPrice as the crossed-out "was" price)
_REFERENCE_PRICE = {"strikethroughprice", "listprice", "srp", "msrp", "minimumadvertisedprice",
                    "invoiceprice"}


def _spec(offer: dict) -> dict:
    """The selling price specification of an Offer: priceSpecification may be one object or a
    list (WooCommerce writes a list), and a list may carry a crossed-out reference price."""
    spec = offer.get("priceSpecification")
    specs = [x for x in (spec if isinstance(spec, list) else [spec]) if isinstance(x, dict)]
    selling = [x for x in specs
               if re.sub(r"[^a-z]", "", (_enum(x.get("priceType")) or "").lower()) not in _REFERENCE_PRICE]
    return (selling or specs or [{}])[0]


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
        prices.append((_num(first(o.get("price"), _spec(o).get("price"))), o))
    known = [x for x in prices if x[0] is not None]
    p, o = min(known, key=lambda x: x[0]) if known else prices[0]
    spec = _spec(o)
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
        "name": _name(base.get("name")), "url": _url(obj.get("url"), page_url) or page_url, "pageUrl": page_url,
        "sku": base.get("sku") if isinstance(base.get("sku"), (str, int)) else None, "gtin": gtin,
        "mpn": base.get("mpn") if isinstance(base.get("mpn"), str) else None,
        "brand": _name(base.get("brand")), "category": _category(base.get("category")),
        "price": price, "lowPrice": lo, "highPrice": hi, "currency": cur, "availability": avail,
        "condition": cond, "priceValidUntil": valid, "offerCount": offers,
        "image": _url(base.get("image"), page_url),
        "rating": _num(rating.get("ratingValue")), "reviewCount": _num(first(rating.get("reviewCount"),
                                                                          rating.get("ratingCount"))),
        "color": _text(base.get("color")),
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


def _joined(objects: list, page_url: str):
    """-> resolve(obj): obj with every {"@id": ...} reference among its own values replaced by
    the node of that @id elsewhere on the page (@graph markup keeps Offer and Brand apart).
    One level only, so a reference cycle cannot recurse."""
    def absolute(i):
        try:
            return urljoin(page_url, i)
        except ValueError:            # "http://[" and other ids no URL parser accepts
            return i

    index = {}
    for o in objects:
        i = o.get("@id")
        if isinstance(i, str) and i:
            index.setdefault(i, o)
            index.setdefault(absolute(i), o)

    def ref(v):
        if isinstance(v, dict) and isinstance(v.get("@id"), str) and not set(v) - {"@id", "@type"}:
            return index.get(v["@id"]) or index.get(absolute(v["@id"])) or v
        return v

    def resolve(obj):
        return {k: ([ref(x) for x in v] if isinstance(v, list) else ref(v)) for k, v in obj.items()}
    return resolve


def _products(objects: list, page_url: str) -> list:
    out, resolve = [], _joined(objects, page_url)
    for obj in objects:
        types = _types(obj)
        if "ProductGroup" in types:
            obj = resolve(obj)
            variants = obj.get("hasVariant")
            variants = variants if isinstance(variants, list) else [variants] if isinstance(variants, dict) else []
            parent = {k: v for k, v in obj.items() if k not in ("hasVariant", "@type", "@id")}
            out += ([product(resolve(v), page_url, parent) for v in variants if isinstance(v, dict)]
                    or [product(obj, page_url)])
        elif "Product" in types or "IndividualProduct" in types:
            out.append(product(resolve(obj), page_url))
    return out


def products_on_page(doc, page_url: str) -> list:
    out = _products(flatten_jsonld(doc.jsonld), page_url) or _products(doc.microdata, page_url)
    if not out and doc.og.get("og:type", "").startswith("product"):
        og = doc.og
        out.append(product({
            "name": og.get("og:title"), "image": og.get("og:image"),
            "sku": og.get("product:retailer_item_id"), "brand": og.get("product:brand"),
            "offers": {"price": first(og.get("product:price:amount"), og.get("og:price:amount")),
                       "priceCurrency": first(og.get("product:price:currency"), og.get("og:price:currency")),
                       "availability": og.get("product:availability")}}, page_url))
    return out


def dedupe_key(p: dict) -> str:
    """GTIN, then SKU with colour and size, then the product's own URL. A product without
    any of them (a listing page in microdata) is told apart by its name on that page."""
    if p["gtin"]:
        return p["gtin"]
    if p["sku"]:
        return f"sku:{p['sku']}:{p['color']}:{p['size']}"
    if p["url"] != p["pageUrl"]:
        return p["url"]
    return f"{p['pageUrl']}#name:{p['name']}"


class ProductCatalog(Actor):
    name = "product-catalog"
    version = "1.3.0"
    dataset_schema = {
        "fields": {"type": "object", "required": ["name", "url", "pageUrl", "price", "currency", "availability", "issues"],
                   "properties": {
                       "name": STR_OR_NULL, "url": STR_OR_NULL, "pageUrl": {"type": "string"},
                       "price": {"type": ["number", "null"]}, "lowPrice": {"type": ["number", "null"]},
                       "highPrice": {"type": ["number", "null"]}, "currency": STR_OR_NULL,
                       "availability": STR_OR_NULL, "issues": {"type": "array", "items": {"type": "string"}}}},
        "views": {"overview": {"title": "Overview", "fields": ["name", "sku", "gtin", "brand", "price", "currency",
                                                                "availability", "url"]}},
    }
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
            "autoscaleConcurrency": AUTOSCALE,
            "render": RENDER,
            "waitForSelector": WAIT_FOR,
            "browserActions": BROWSER_ACTIONS,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        seen, summary = set(), {"pages": 0, "pagesWithProducts": 0, "products": 0, "duplicates": 0,
                                "withIssues": 0, "currencies": {}, "availability": {}}

        def on_page(res, req, doc):
            if doc is None:
                if not res.duplicate and (res.error or (res.status and res.status >= 400)):
                    ctx.record_failure(req["url"], res.error or f"HTTP {res.status}")
                return
            summary["pages"] += 1
            items = []
            for p in products_on_page(doc, res.final_url or req["url"]):
                key = dedupe_key(p)
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
