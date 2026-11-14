# Live check: product-catalog on public shops

2026-11-14, ww-scraper `main` plus the fixes in this change, default pace (one request per
second per host, slowed down on 429), robots.txt read on every host. 25 shops tried: large
Polish and international retailers on their own platforms, Shopify, WooCommerce, Magento and
two sites published for scraping practice. Each run started from a home, category or product
page with at most 6 pages per shop.

## What answered

| Shop | Platform / markup | Products | Price matches the page |
|---|---|---|---|
| webscraper.io test shop | microdata on listing cards | 30 | 30 of 30 |
| woocommerce.com | WooCommerce, JSON-LD `@graph`, `priceSpecification` list | 5 | 5 of 5 (3 free) |
| ikea.com/pl | JSON-LD | 6 | 5 of 5, plus 1 category page marked up as a Product without price |
| thomann.de/pl | JSON-LD | 4 | 4 of 4 |
| smyk.com | microdata | 5 | 3 of 3; 2 not checkable (the second fetch got a bot page) |
| rossmann.pl | JSON-LD | 1 | 1 of 1 |

"Price matches the page" means the extracted price appears in the text a person sees on the
page (scripts and markup removed), checked by a script and then by hand where the script
missed: `$372.7` shown without the trailing zero, and a price split over two elements
(`62` and `,99 zł`). No extracted price was wrong. Brand and name are what the shop
publishes: one shop writes `brand: Mega` for Mega Bloks.

## What did not

| Answer | Shops |
|---|---|
| HTTP 403 (bot filter) | 5, among them a large Polish electronics retailer and a DIY retailer |
| HTTP 429 on product pages (three Shopify stores) | the interval between requests doubled after each 429, up to 32 s; the run then ended at its page budget |
| robots.txt answered 403 or 526 | 2: read as no permission, nothing fetched |
| Pages over 5 MB (the default body cap) | 1 |
| A maintenance page with HTTP 200 | 1 |
| Start page with no product links in the first 6 pages | 3 (pagination or articles first; not a parser result) |
| robots.txt timed out | 1 (a network error, reported as such, not as a refusal) |
| Not reached before the run's page budget | 2 (the 429 slowdown above used it up) |
| HTTP 404 | 1 (a product URL given by hand that no longer exists) |

A crawler that respects these answers got product data from 6 of the 25 shops, without a
browser or a proxy. That is a property of the shops, not of this parser.

## What it found

Three patterns the generated corpus did not have, each now fixed and in the bench or tests:

1. `priceSpecification` written as a list (WooCommerce): price read, currency not; with a
   crossed-out `StrikethroughPrice` first, the crossed-out price won. Encoding
   `price-spec-list`.
2. `<a href="/p/1" itemprop="name">Laptop</a>` on listing cards: the HTML standard makes the
   href the value, the shop means the text. Link text is now used for properties that do not
   hold a URL, and a name the card cuts (`Asus VivoBook...`) is completed from the link's
   `title`. Encoding `microdata-listing`.
3. Products without SKU, GTIN or a URL of their own shared one dedupe key, so a listing page
   kept only its first product. They are now told apart by name on the page (unit test).
