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
| Marketplace of ready scrapers | eleven built-in actors | thousands |
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
