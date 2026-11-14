# Extraction benchmark: product data against known answers

[`corpus.py`](corpus.py) generates product pages and writes, for each, the answer: name, price
(or low and high price), currency, availability, SKU, GTIN, brand and image URL.
[`run.py`](run.py) runs the parser `product-catalog` runs on every page (`parse_html` and
`products_on_page`) and compares each field with the answer. A price is right within half a
cent; every other field must match exactly.

The 12 encodings are ways shops publish the same product, valid or read past by browsers
and search engines:

| Encoding | What is on the page |
|---|---|
| `jsonld` | one JSON-LD Product with an Offer, next to a BreadcrumbList |
| `jsonld-graph` | `@graph` with the Offer and the Brand as separate nodes joined by `@id` |
| `price-locale` | price as a string: `1.299,00`, `1 299,00`, `1,299.00` |
| `price-symbol` | price with a currency sign or code: `49,99 zł`, `$49.99`, `EUR 49,99` |
| `jsonld-raw-text` | a raw newline or tab inside a JSON string, which strict JSON rejects |
| `jsonld-entities` | HTML entities inside JSON-LD text: `Kawa &amp; herbata` |
| `relative-image` | image as a path relative to the page |
| `microdata` | `itemscope`/`itemprop` only, price in a `content` attribute |
| `microdata-text` | microdata with the price only in the visible text: `1 299,00 zł` |
| `opengraph` | Open Graph product tags only (`product:price:amount`, `product:availability`) |
| `product-group` | ProductGroup with three variants, each with its own SKU and price |
| `aggregate-offer` | AggregateOffer with `lowPrice` and `highPrice` |

## Result

300 pages, 25 per encoding, seed 2026. Raw counts: [`results.json`](results.json).

| Encoding | 0.9.0: fields right | 0.9.0: products found | now: fields right | now: pages fully right |
|---|---|---|---|---|
| jsonld | 100% | 25/25 | 100% | 25/25 |
| jsonld-graph | 60% | 25/25 | 100% | 25/25 |
| price-locale | 98% | 25/25 | 100% | 25/25 |
| price-symbol | 90% | 25/25 | 100% | 25/25 |
| jsonld-raw-text | 0% | 0/25 | 100% | 25/25 |
| jsonld-entities | 90% | 25/25 | 100% | 25/25 |
| relative-image | 90% | 25/25 | 100% | 25/25 |
| microdata | 0% | 0/25 | 100% | 25/25 |
| microdata-text | 0% | 0/25 | 100% | 25/25 |
| opengraph | 70% | 25/25 | 100% | 25/25 |
| product-group | 100% | 75/75 | 100% | 25/25 |
| aggregate-offer | 100% | 25/25 | 100% | 25/25 |
| **all** | **71%** | **275/350** | **100%** | **300/300** |

What 0.9.0 got wrong: a price of `36.266,22` came out as 36.266 (the dot read as the
decimal mark); `49,99 zł` and `$49.99` gave no price; `@graph` products lost price,
currency, availability and brand; one raw newline in a description lost the whole JSON-LD
block; microdata was not read; Open Graph availability stayed `in stock` and SKU and brand
were left out; relative image paths and `&amp;` were passed through as written.

With seeds 1, 7 and 99 and 100 pages per encoding (3,600 more pages, different prices,
spellings and variant orders) every field is right as well.

## What it does not say

- The corpus and the fixes were written together, so 100% here shows that each pattern is
  handled and stays handled. It is not an estimate of accuracy on shops in the wild, which
  publish patterns this corpus does not have.
- It measures the parser, not the crawl: fetching, rendering and dedupe are measured in
  [`bench/crawl`](../crawl/README.md) and [`bench/safety`](../safety/README.md).
- Prices are read as published. A shop whose markup shows a different price from its page
  (a stale JSON-LD block) gets the markup's price.
- `1.299` is read as 1.299, the way schema.org writes a decimal price; a shop that means one
  thousand two hundred ninety-nine writes it wrongly for every consumer of its markup.

## Run it

```bash
python bench/extract/run.py                      # the table above, seed 2026
python bench/extract/run.py --seed 7 --per-encoding 100
python bench/extract/run.py --json bench/extract/results.json
```

No network, no browser: under a second.
