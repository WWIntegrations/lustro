"""Product extraction on the encodings real shops publish (found by bench/extract)."""
import json

import pytest

from ww_scraper.actors.product_catalog import _enum, _num, products_on_page
from ww_scraper.extract import parse_html

URL = "https://shop.example/p/1"


def page(head="", body=""):
    return parse_html(f"<html><head>{head}</head><body>{body}</body></html>", URL)


def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


@pytest.mark.parametrize("raw, want", [
    ("1.299,00", 1299.0), ("1 299,00", 1299.0), ("1 299,00", 1299.0), ("1,299.00", 1299.0),
    ("12.345.678,90", 12345678.9), ("1'299.50", 1299.5), ("49,99", 49.99), ("49.99", 49.99),
    ("1,299", 1299.0), ("1.299", 1.299), ("49,99 zł", 49.99), ("$49.99", 49.99), ("US$ 49.99", 49.99),
    ("€1.299,00", 1299.0), ("EUR 1 299,00", 1299.0), ("PLN 49.99", 49.99), ("49.99 USD", 49.99),
    ("-5,00", -5.0), (".99", 0.99), (12, 12.0), (12.5, 12.5),
])
def test_price_strings_in_local_formats(raw, want):
    assert _num(raw) == pytest.approx(want)


@pytest.mark.parametrize("raw", ["", "free", "call us", "1,2,3.4.5", "..", "1.2.3", True, None, [], "1e400"])
def test_unreadable_prices_are_none(raw):
    assert _num(raw) is None


@pytest.mark.parametrize("raw, want", [
    ("https://schema.org/InStock", "InStock"), ("http://schema.org/OutOfStock", "OutOfStock"),
    ("InStock", "InStock"), ("in stock", "InStock"), ("in_stock", "InStock"), ("out of stock", "OutOfStock"),
    ("preorder", "PreOrder"), ("available for order", "BackOrder"), ("backorder", "BackOrder"),
    ("Discontinued", "Discontinued"), ("SomethingNew", "SomethingNew"),
])
def test_availability_is_one_schema_org_name(raw, want):
    assert _enum(raw) == want


def test_graph_nodes_joined_by_id():
    doc = page(ld({"@context": "https://schema.org", "@graph": [
        {"@type": "Product", "name": "Kubek", "sku": "K1", "offers": {"@id": "#offer"}, "brand": {"@id": "#brand"}},
        {"@type": "Offer", "@id": "#offer", "price": "19.90", "priceCurrency": "PLN",
         "availability": "https://schema.org/InStock"},
        {"@type": "Brand", "@id": "#brand", "name": "Tatra"}]}))
    [p] = products_on_page(doc, URL)
    assert (p["price"], p["currency"], p["availability"], p["brand"]) == (19.9, "PLN", "InStock", "Tatra")


def test_id_references_cannot_loop():
    doc = page(ld({"@graph": [{"@type": "Product", "@id": "#p", "name": "A", "isRelatedTo": {"@id": "#p"},
                               "offers": {"@id": "#o"}},
                              {"@type": "Offer", "@id": "#o", "itemOffered": {"@id": "#p"}, "price": 1}]}))
    [p] = products_on_page(doc, URL)
    assert p["price"] == 1.0


def test_raw_newline_inside_a_json_ld_string_still_parses():
    doc = page('<script type="application/ld+json">{"@type": "Product", "name": "Lampa",'
               ' "description": "line one\nline two\tend", "offers": {"price": 5, "priceCurrency": "EUR"}}</script>')
    assert doc.jsonld_errors == 0
    [p] = products_on_page(doc, URL)
    assert p["name"] == "Lampa" and p["price"] == 5.0


def test_entities_in_json_ld_text_are_decoded():
    doc = page(ld({"@type": "Product", "name": "Kawa &amp; herbata", "brand": {"name": "A&amp;B"}}))
    [p] = products_on_page(doc, URL)
    assert (p["name"], p["brand"]) == ("Kawa & herbata", "A&B")


def test_relative_image_and_url_are_made_absolute():
    doc = page(ld({"@type": "Product", "name": "X", "image": "/img/1.jpg", "url": "/p/1?c=red"}))
    [p] = products_on_page(doc, URL)
    assert p["image"] == "https://shop.example/img/1.jpg"
    assert p["url"] == "https://shop.example/p/1?c=red"


def test_non_http_image_is_dropped():
    doc = page(ld({"@type": "Product", "name": "X", "image": "javascript:alert(1)"}))
    [p] = products_on_page(doc, URL)
    assert p["image"] is None


MICRODATA = """
<div itemscope itemtype="https://schema.org/Product">
  <h1 itemprop="name">Plecak  trekkingowy</h1>
  <img itemprop="image" src="/img/p.jpg">
  <span itemprop="sku">PL-1</span><meta itemprop="gtin13" content="5901234123457">
  <div itemprop="brand" itemscope itemtype="https://schema.org/Brand"><span itemprop="name">Orla</span></div>
  <div itemprop="offers" itemscope itemtype="http://schema.org/Offer">
    <span itemprop="price">1 299,00 zł</span>
    <meta itemprop="priceCurrency" content="PLN">
    <link itemprop="availability" href="https://schema.org/InStock">
  </div>
  <div itemprop="review" itemscope itemtype="https://schema.org/Review">
    <span itemprop="name">not the product name</span>
  </div>
</div>"""


def test_microdata_product():
    doc = page(body=MICRODATA)
    [item] = doc.microdata
    assert item["@type"] == "Product" and item["offers"]["@type"] == "Offer"
    [p] = products_on_page(doc, URL)
    assert p["name"] == "Plecak trekkingowy"
    assert (p["sku"], p["gtin"], p["brand"]) == ("PL-1", "5901234123457", "Orla")
    assert (p["price"], p["currency"], p["availability"]) == (1299.0, "PLN", "InStock")
    assert p["image"] == "https://shop.example/img/p.jpg"


def test_microdata_content_attribute_wins_over_text():
    doc = page(body='<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">N</span>'
                    '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
                    '<span itemprop="price" content="10.50">10,50 zł</span></div></div>')
    [p] = products_on_page(doc, URL)
    assert p["price"] == 10.5


def test_json_ld_wins_over_microdata_for_the_same_page():
    doc = page(ld({"@type": "Product", "name": "From JSON-LD", "sku": "J"}), MICRODATA)
    assert [p["name"] for p in products_on_page(doc, URL)] == ["From JSON-LD"]


def test_microdata_is_bounded():
    many = "".join('<div itemscope itemtype="https://schema.org/Thing"><span itemprop="name">x</span></div>'
                   for _ in range(500))
    assert len(page(body=many).microdata) <= 50
    long = page(body='<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">'
                     + "y" * 50000 + "</span></div>")
    assert len(long.microdata[0]["name"]) == 2000
    # past the tree's depth limit elements are flattened into their ancestor (see
    # MAX_TREE_DEPTH): the item survives, the property that lost its element does not
    deep = '<div itemscope itemtype="https://schema.org/Product">' + "<div>" * 400 + (
        '<span itemprop="name">y</span>') + "</div>" * 400 + "</div>"
    assert page(body=deep).microdata == [{"@type": "Product"}]


def test_open_graph_product_tags():
    doc = page("".join(f'<meta property="{k}" content="{v}">' for k, v in {
        "og:type": "product", "og:title": "Kask", "og:image": "https://shop.example/k.jpg",
        "product:price:amount": "199.00", "product:price:currency": "PLN",
        "product:availability": "out of stock", "product:retailer_item_id": "KS-9",
        "product:brand": "Kestrel"}.items()))
    [p] = products_on_page(doc, URL)
    assert (p["sku"], p["brand"], p["availability"], p["price"]) == ("KS-9", "Kestrel", "OutOfStock", 199.0)


def test_seo_audit_counts_microdata_as_structured_data():
    from types import SimpleNamespace

    from ww_scraper.actors.seo_audit import audit_page
    res = SimpleNamespace(url=URL, final_url=URL, status=200, redirects=[], headers={}, error=None)
    codes = lambda doc: {i["code"] for i in audit_page(res, doc)["issues"]}  # noqa: E731
    assert "jsonld-missing" in codes(page(body="<p>plain</p>"))
    item = audit_page(res, page(body=MICRODATA))
    assert "jsonld-missing" not in {i["code"] for i in item["issues"]}
    assert item["microdataTypes"] == ["Product"]


def test_ids_no_url_parser_accepts_do_not_break_the_page():
    doc = page(ld({"@graph": [{"@type": "Product", "name": "A", "offers": {"@id": "http://[::1"}},
                              {"@type": "Offer", "@id": "http://[::1", "price": 3}]}))
    [p] = products_on_page(doc, URL)
    assert p["price"] == 3.0
