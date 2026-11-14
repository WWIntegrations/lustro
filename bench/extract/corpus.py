"""A generated corpus of product pages with known answers, for measuring extraction.

Every page carries one product (or, for a ProductGroup, its variants) in one of the
encodings below. The generator writes the answer next to the HTML, so a score is the
extractor's output compared with what the page says, field by field. All the encodings are
valid ways to publish schema.org or Open Graph product data, or are mistakes that browsers
and search engines read past (raw newlines inside a JSON string, double-escaped entities).

    jsonld            one clean JSON-LD Product with an Offer
    jsonld-graph      @graph with the Offer and Brand as separate nodes joined by @id
    price-locale      price as a string in a local format: "1.299,00", "1 299,00", "1,299.00"
    price-symbol      price string with a currency sign or code: "49,99 zł", "$49.99"
    price-spec-list   Offer.priceSpecification as a list (as WooCommerce writes it), half of
                      them with a crossed-out StrikethroughPrice before the selling price
    jsonld-raw-text   a raw newline or tab inside a JSON string (strict JSON rejects it)
    jsonld-entities   HTML entities inside JSON-LD text ("Kawa &amp; herbata")
    relative-image    image given as a path relative to the page
    microdata         itemscope/itemprop only, no JSON-LD
    microdata-text    microdata with the price only in the visible text ("1 299,00 zł")
    microdata-listing a listing page: three microdata products, no SKU or GTIN, each name
                      written on its link (<a href="/p/1" itemprop="name">), as listing cards do
    opengraph         Open Graph product tags only (og:type product, product:price:amount)
    product-group     ProductGroup with hasVariant, each variant its own SKU and price
    aggregate-offer   AggregateOffer with lowPrice and highPrice
"""
from __future__ import annotations

import html
import json
import random

BASE = "https://shop.example"
ENCODINGS = ["jsonld", "jsonld-graph", "price-locale", "price-symbol", "price-spec-list", "jsonld-raw-text",
             "jsonld-entities", "relative-image", "microdata", "microdata-text", "microdata-listing", "opengraph",
             "product-group", "aggregate-offer"]
AVAIL = ["InStock", "OutOfStock", "PreOrder", "BackOrder", "LimitedAvailability"]
AVAIL_FORMS = [lambda a: f"https://schema.org/{a}", lambda a: f"http://schema.org/{a}", lambda a: a]
OG_AVAIL = {"InStock": "in stock", "OutOfStock": "out of stock", "PreOrder": "preorder",
            "BackOrder": "available for order", "LimitedAvailability": "in stock"}
OG_AVAIL_TRUTH = {"in stock": "InStock", "out of stock": "OutOfStock", "preorder": "PreOrder",
                  "available for order": "BackOrder"}
NOUNS = ["Kawa", "Herbata", "Kubek", "Plecak", "Lampa", "Czajnik", "Bluza", "Rower", "Narty",
         "Kij", "Buty", "Kurtka", "Termos", "Namiot", "Lina", "Kask"]
ADJ = ["czarny", "zielona", "lekki", "zimowe", "trekkingowy", "classic", "pro", "mini"]
BRANDS = ["Nordwind", "Tatra", "Bialka", "Kestrel", "Orla", "Vistula"]
CURRENCIES = ["PLN", "EUR", "USD"]
SIGN = {"PLN": "zł", "EUR": "€", "USD": "$"}

CONTROL = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}</script>'


def _gtin(rng):
    d = [rng.randrange(10) for _ in range(12)]
    s = sum(x * (3 if i % 2 else 1) for i, x in enumerate(d))
    return "".join(map(str, d)) + str((10 - s % 10) % 10)


def _locale(value, rng):
    """1299.0 -> one of '1.299,00', '1 299,00', '1,299.00' (for values of 1000 or more)."""
    whole, cents = f"{value:.2f}".split(".")
    groups = f"{int(whole):,}"
    return rng.choice([groups.replace(",", ".") + "," + cents,
                       groups.replace(",", " ") + "," + cents,
                       groups.replace(",", " ") + "," + cents,
                       groups + "." + cents])


def _with_sign(value, cur, rng):
    s = f"{value:.2f}"
    comma = s.replace(".", ",")
    return rng.choice({"PLN": [f"{comma} zł", f"{comma} zł", f"PLN {s}"],
                       "EUR": [f"{comma} €", f"€{s}", f"EUR {comma}"],
                       "USD": [f"${s}", f"US$ {s}", f"{s} USD"]}[cur])


def _product(rng, k):
    cur = rng.choice(CURRENCIES)
    price = round(rng.choice([rng.uniform(5, 999), rng.uniform(1000, 9999)]), 2)
    return {"name": f"{rng.choice(NOUNS)} {rng.choice(ADJ)} {k}", "sku": f"SKU-{k:04d}",
            "gtin": _gtin(rng), "brand": rng.choice(BRANDS), "price": price, "lowPrice": None,
            "highPrice": None, "currency": cur, "availability": rng.choice(AVAIL),
            "image": f"{BASE}/img/{k}.jpg"}


def _offer(p, rng, price=None):
    return {"@type": "Offer", "price": p["price"] if price is None else price,
            "priceCurrency": p["currency"], "availability": rng.choice(AVAIL_FORMS)(p["availability"]),
            "url": f"{BASE}/p/{p['sku']}"}


def _jsonld_product(p, rng, **over):
    obj = {"@context": "https://schema.org", "@type": "Product", "name": p["name"], "sku": p["sku"],
           "gtin13": p["gtin"], "brand": {"@type": "Brand", "name": p["brand"]}, "image": p["image"],
           "description": "Opis produktu.", "offers": _offer(p, rng)}
    obj.update(over)
    return obj


def _script(obj):
    return f'<script type="application/ld+json">{json.dumps(obj, ensure_ascii=False)}</script>'


def _page(head, body=""):
    return (f"<!doctype html><html lang=pl><head><meta charset=utf-8><title>Sklep</title>{head}</head>"
            f"<body><nav><a href='/'>Start</a></nav><main>{body}</main><footer>Sklep</footer></body></html>")


def _microdata(p, rng, price_in_text):
    if price_in_text:
        whole, cents = f"{p['price']:.2f}".split(".")
        shown = f"{int(whole):,}".replace(",", " ") + f",{cents} {SIGN[p['currency']]}"
        price = f'<span itemprop="price">{shown}</span>'
    else:
        price = f'<span itemprop="price" content="{p["price"]:.2f}">{p["price"]:.2f}</span>'
    brand = rng.choice([f'<span itemprop="brand">{p["brand"]}</span>',
                        f'<div itemprop="brand" itemscope itemtype="https://schema.org/Brand">'
                        f'<span itemprop="name">{p["brand"]}</span></div>'])
    image = rng.choice([f'<img itemprop="image" src="{p["image"]}" alt="">',
                        f'<link itemprop="image" href="{p["image"]}">'])
    return (f'<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">{p["name"]}</h1>'
            f'{image}<p>SKU: <span itemprop="sku">{p["sku"]}</span></p>'
            f'<meta itemprop="gtin13" content="{p["gtin"]}">{brand}'
            f'<div itemprop="offers" itemscope itemtype="http://schema.org/Offer">{price}'
            f'<meta itemprop="priceCurrency" content="{p["currency"]}">'
            f'<link itemprop="availability" href="{rng.choice(AVAIL_FORMS[:2])(p["availability"])}">'
            f'</div></div>')


def make(rng, k, enc):
    """-> (html, url, [expected products])"""
    p = _product(rng, k)
    url = f"{BASE}/p/{p['sku']}"
    truth = [p]
    if enc == "jsonld":
        doc = _page(CONTROL + _script(_jsonld_product(p, rng)))
    elif enc == "jsonld-graph":
        prod = _jsonld_product(p, rng, offers={"@id": f"{url}#offer"}, brand={"@id": f"{BASE}/#brand"})
        prod.pop("@context")
        doc = _page(_script({"@context": "https://schema.org", "@graph": [
            {"@type": "WebPage", "@id": url, "name": p["name"]}, prod,
            dict(_offer(p, rng), **{"@id": f"{url}#offer"}),
            {"@type": "Brand", "@id": f"{BASE}/#brand", "name": p["brand"]}]}))
    elif enc == "price-locale":
        p["price"] = round(rng.uniform(1000, 99999), 2)
        doc = _page(_script(_jsonld_product(p, rng, offers=_offer(p, rng, price=_locale(p["price"], rng)))))
    elif enc == "price-symbol":
        doc = _page(_script(_jsonld_product(p, rng, offers=_offer(p, rng, price=_with_sign(p["price"], p["currency"], rng)))))
    elif enc == "price-spec-list":
        specs = [{"@type": "UnitPriceSpecification", "price": f"{p['price']:.2f}", "priceCurrency": p["currency"]}]
        if rng.random() < 0.5:
            specs.insert(0, {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/StrikethroughPrice",
                             "price": f"{p['price'] * 1.25:.2f}", "priceCurrency": p["currency"]})
        offer = {"@type": "Offer", "priceSpecification": specs,
                 "availability": rng.choice(AVAIL_FORMS)(p["availability"]), "url": url}
        doc = _page(_script({"@context": "https://schema.org", "@graph": [
            dict(_jsonld_product(p, rng, offers=[offer]), **{"@id": f"{url}#product"})]}))
    elif enc == "jsonld-raw-text":
        raw = _script(_jsonld_product(p, rng, description="Pierwsza linia.\nDruga linia.\tKoniec."))
        doc = _page(raw.replace("\\n", "\n").replace("\\t", "\t"))
    elif enc == "jsonld-entities":
        p["name"] = f"{rng.choice(NOUNS)} &amp; {rng.choice(NOUNS).lower()} {k}"
        doc = _page(_script(_jsonld_product(p, rng)))
        p["name"] = html.unescape(p["name"])
    elif enc == "relative-image":
        doc = _page(_script(_jsonld_product(p, rng, image=f"/img/{k}.jpg")))
    elif enc in ("microdata", "microdata-text"):
        doc = _page("", _microdata(p, rng, enc == "microdata-text"))
    elif enc == "microdata-listing":
        cards, truth = [], []
        for i in range(3):
            q = dict(_product(rng, k * 10 + i), sku=None, gtin=None, brand=None)
            q["image"] = f"{BASE}/img/l{k}-{i}.png"
            cards.append(f'<div class="card" itemscope itemtype="https://schema.org/Product">'
                         f'<img itemprop="image" src="/img/l{k}-{i}.png" alt="">'
                         f'<h4 itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
                         f'<span itemprop="price">{SIGN[q["currency"]]}{q["price"]:.2f}</span>'
                         f'<meta itemprop="priceCurrency" content="{q["currency"]}">'
                         f'<link itemprop="availability" href="https://schema.org/{q["availability"]}"></h4>'
                         f'<h4><a href="/p/l{k}-{i}" class="title" itemprop="name">\n  {q["name"]}\n</a></h4></div>')
            truth.append(q)
        doc = _page("", "".join(cards))
    elif enc == "opengraph":
        og = OG_AVAIL[p["availability"]]
        p["availability"] = OG_AVAIL_TRUTH[og]
        p["gtin"] = None
        tags = {"og:type": "product", "og:title": p["name"], "og:image": p["image"], "og:url": url,
                "product:price:amount": f"{p['price']:.2f}", "product:price:currency": p["currency"],
                "product:availability": og, "product:retailer_item_id": p["sku"], "product:brand": p["brand"]}
        doc = _page("".join(f'<meta property="{k_}" content="{html.escape(v)}">' for k_, v in tags.items()))
    elif enc == "product-group":
        variants, truth = [], []
        for i, size in enumerate(["S", "M", "L"]):
            v = dict(p, sku=f"{p['sku']}-{size}", gtin=_gtin(rng), price=round(p["price"] + i * 10, 2),
                     availability=rng.choice(AVAIL))
            variants.append({"@type": "Product", "sku": v["sku"], "gtin13": v["gtin"], "size": size,
                             "name": f"{p['name']} {size}", "offers": _offer(v, rng)})
            v["name"] = f"{p['name']} {size}"
            truth.append(v)
        doc = _page(_script({"@context": "https://schema.org", "@type": "ProductGroup", "name": p["name"],
                             "productGroupID": p["sku"], "brand": {"@type": "Brand", "name": p["brand"]},
                             "image": p["image"], "variesBy": "https://schema.org/size", "hasVariant": variants}))
    elif enc == "aggregate-offer":
        lo, hi = p["price"], round(p["price"] * 1.6, 2)
        p.update(price=None, lowPrice=lo, highPrice=hi)
        doc = _page(_script(_jsonld_product(p, rng, offers={
            "@type": "AggregateOffer", "lowPrice": lo, "highPrice": hi, "priceCurrency": p["currency"],
            "offerCount": 4, "availability": rng.choice(AVAIL_FORMS)(p["availability"])})))
    else:
        raise ValueError(enc)
    return doc, url, truth


def corpus(per_encoding=25, seed=2026):
    rng = random.Random(seed)
    out, k = [], 0
    for enc in ENCODINGS:
        for _ in range(per_encoding):
            k += 1
            doc, url, truth = make(rng, k, enc)
            out.append({"encoding": enc, "url": url, "html": doc, "truth": truth})
    return out
