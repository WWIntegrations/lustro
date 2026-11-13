# ww-scraper and hosted scraping platforms

This page states plainly what ww-scraper gives you compared with a hosted scraping
platform, and what it does not. It makes no claim about another product's internals; the
right-hand column lists only what such platforms publicly offer as their core service.

| | ww-scraper | Hosted platform |
|---|---|---|
| Where it runs | your machine, one `pip install` | their cloud, an account |
| Cost | none; MIT license | subscription or usage |
| Data location | a directory you own | their storage, then export |
| Export formats | JSON, JSONL, CSV, XLSX, XML, HTML, with field selection; formulas neutralised in CSV and never evaluated in XLSX | JSON, JSONL, CSV, XLSX, XML, RSS, HTML |
| Datasets, key-value stores, request queues, runs | yes, same layout and names | yes |
| HTTP API | local only, loopback, token required | public, per account |
| Web console | local: start runs from a schema form, live counters and log, items, downloads; strict CSP | hosted console |
| Webhooks | HMAC-signed, allowlisted hosts only | yes |
| Scheduling | prints a cron line or launchd plist for you to install | managed |
| Your own scrapers | one Python file (`ww-scraper init`) | SDK plus deployment |
| JavaScript rendering | local Chrome, behind an SSRF-filtering proxy | managed browsers |
| Concurrency | up to 16 pages in flight, per-host spacing kept | horizontal scaling |
| Pacing under load | per host: a 429 or 503 doubles the interval, a clean streak eases it back, never below Crawl-delay | autoscaling on the runner's CPU and memory |
| Re-crawls | ETag / Last-Modified revalidation in `change-monitor`: an unchanged page answers 304 and is not downloaded again | depends on the scraper you run |
| robots.txt | always obeyed, RFC 9309, no switch to turn it off; `robots-audit` explains each decision | the operator's choice |
| Proxies, IP rotation, CAPTCHA solving, anti-bot evasion | **no, by design** | offered as products |
| Private-network safety (SSRF) | every hop and every browser connection validated | not applicable to a cloud runner |
| Personal data | e-mail and phone numbers masked by default | the operator's choice |
| Ready scrapers | twenty built-in actors, including jobs (three ATS APIs plus any careers page), places (OpenStreetMap), product catalogs and contact details, each built on official endpoints or published structured data | thousands, many maintained by third parties |
| Scale | one machine | many machines |

## Measured, not claimed

[`bench/safety`](../bench/safety/README.md) runs ww-scraper and Crawlee (a widely used
open-source crawler library) against one hostile local site. In default configuration Crawlee
fetched a page robots.txt disallows, tried to connect to the cloud metadata address after a
redirect, retried a 429 within 10 ms of a `Retry-After: 4`, and exported a scraped formula
into CSV as a live formula. ww-scraper did none of the four.

[`bench/crawl`](../bench/crawl/README.md) crawls a generated 500-page site where every page
is also linked as a print view, with tracking parameters, with a trailing slash and through a
redirecting old path. With the same budget for both (10 requests in flight), ww-scraper
downloaded 510 pages and passed on 0 duplicates in 1.6 s and 35 MB; Crawlee downloaded 2,500,
passed on 2,000 duplicates, followed links from a `nofollow` page, and took 8.3 s and 389 MB.

### Against the hosted platform itself

On 2026-11-13 the same job ran on a hosted scraping platform's website content crawler (its
plain-HTTP mode, no browser) and on ww-scraper's `website-content-crawler`: start at
`https://books.toscrape.com/` (a site published for scraping practice, no robots.txt rules),
stop at 200 pages, save each page's main content as Markdown. ww-scraper ran on one laptop
(Apple M2, 16 GB) with up to 8 requests in flight and its per-host pacing; the hosted runs used
the platform's default settings at two memory sizes.

| | ww-scraper, local | Hosted, 1 GB | Hosted, 4 GB |
|---|---|---|---|
| Run time | 103 s | 478 s | 105 s |
| Pages saved (limit 200) | 200 | 212 | 206 |
| Peak memory | 42 MB | 976 MB | 1,862 MB |
| Cost of the run | none | $0.039 | $0.029 |

Content on the 73 URLs both saved: product pages match (583 against 597 words, 8.95 against
8.95 prices per page). Listing pages exposed a defect in ww-scraper, fixed in this release:
each book on a listing is its own `<article>`, and the extractor kept only the first one
(1.0 price per listing page against 10.75). With the fix, ww-scraper keeps the list the items
share: 10.89 prices per listing page against 10.75. The hosted crawler kept the page title and
breadcrumbs as well (658 against 579 words per listing page).

What this does and does not show: on a small, permissive site one laptop matches the hosted
crawler's speed at its 4 GB setting with about 2% of its memory, stops at the page limit, and
costs nothing per run. It says nothing about many machines, browser-rendered sites or sites
that need proxies, which ww-scraper does not attempt.

## When ww-scraper is the better tool

- You crawl sites you run, sites that allow it, or public data through official APIs.
- You must be able to show how the data was collected: which rule allowed each URL, what
  was skipped and why, with the run record on your own disk.
- You do not want an account, a bill or your data on someone else's servers.

## When it is not

- The target forbids automated access or sits behind anti-bot protection. ww-scraper
  stops there; it has no feature to get past a block and will not get one.
- You need millions of pages a day or many machines.
- You want a ready-made scraper for one specific commercial site.
