# ww-scraper and hosted scraping platforms

This page states plainly what ww-scraper gives you compared with a hosted platform such as
hosted scraping platforms, and what it does not. It makes no claim about another product's internals; the
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
