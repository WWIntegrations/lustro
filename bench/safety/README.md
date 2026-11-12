# Safety benchmark: ww-scraper and Crawlee on one hostile site

[Crawlee](https://crawlee.dev) is a widely used open-source crawler library for Node.js. This
benchmark points it and ww-scraper at the same small local site and records what each one
does in four situations where a crawler can cause harm. Each result comes from the bench
server's own request log or from the tool's output, so neither tool grades itself.

Measured on 2026-11-12: ww-scraper 0.6.0, Crawlee 3.18.2 (`CheerioCrawler`), Node 22.23,
Python 3.12, macOS. Raw output: [`results.json`](results.json).

| Situation | ww-scraper, defaults | Crawlee, defaults | Crawlee, `respectRobotsTxtFile: true` |
|---|---|---|---|
| robots.txt says `Disallow: /private/`; the home page links to `/private/secret` | not requested | **requested** | not requested |
| A page redirects to `http://169.254.169.254/latest/meta-data/` (the cloud metadata address) | refused before connecting | **connection attempted** | **connection attempted** |
| The server answers 429 with `Retry-After: 4` | retried after 4.01 s | **retried after 0.01 s** | **retried after 0.01 s** |
| A scraped value `=HYPERLINK("http://evil.example","click")` exported to CSV | `'=HYPERLINK(...)`, opens as text | **`=HYPERLINK(...)`, a live formula** | **`=HYPERLINK(...)`, a live formula** |

## What it means

- **robots.txt**: Crawlee can obey it, but the option is off by default
  (`respectRobotsTxtFile = false` in `@crawlee/basic`). ww-scraper has no switch to turn it off.
- **Redirect into a private address**: on a cloud runner, `169.254.169.254` serves instance
  credentials. Crawlee followed the redirect and tried to connect (here it failed only
  because a laptop has no metadata service). ww-scraper checks every redirect hop and
  refuses non-public addresses before opening a socket.
- **Retry-After**: Crawlee retried the 429 within 10 ms, ignoring the four seconds the server
  asked for. ww-scraper waited the full four seconds and, since 0.5.0, also slows every later
  request to that host.
- **CSV formulas**: a cell starting with `=` runs as a formula when the CSV is opened in a
  spreadsheet. ww-scraper prefixes such cells with `'`; its XLSX export stores them as
  text cells that are never evaluated.

## What it does not say

Crawlee and the hosted platforms built on it do much that ww-scraper does not: many machines, browser pools, proxy
rotation and thousands of ready-made scrapers. Most of the behaviours above can be
configured in Crawlee by someone who knows to do it. The benchmark measures defaults,
because defaults are what runs when nobody checks.

## Changes from defaults

- ww-scraper: `allow_loopback=True`, the test switch that lets it reach a site on
  127.0.0.1. Link-local and private addresses stay blocked.
- Crawlee: `navigationTimeoutSecs: 5`, so the unreachable metadata host does not stall the run,
  and `maxRequestsPerCrawl: 30`.

## Reproduce

```bash
mkdir /tmp/crawlee-bench && (cd /tmp/crawlee-bench && npm init -y && npm install crawlee@3)
python bench/safety/run.py --crawlee-dir /tmp/crawlee-bench
```

Error texts are passed through a redaction step so the machine's own LAN address never
lands in `results.json`.
