# Changelog

## Unreleased

- Hostile markup can no longer stall or crash extraction: tables nested in table cells
  used to be rendered again at every level above them (20 levels took 7 s, each further
  level doubled it), and pages nested thousands of elements deep hit Python's recursion
  limit. Tables now walk only their own rows, the tree stops nesting at 160 levels, and
  the share-bar check walks at most 200 elements per candidate.
- Markdown leaves out share bars (elements labelled as share tools, or made of links that
  only post the page to a social network) and Cloudflare's obfuscated e-mail links
  (`/cdn-cgi/l/email-protection`), which carry an encoded address. A class name alone
  ("market-share") does not drop content.
- `title` and `description` are decoded once more when the site double-escapes them
  (`Q&amp;amp;A` in the source used to read `Q&amp;A`).
- robots.txt that cannot be read because of the network (timeout, refused connection, DNS)
  is retried twice with backoff. If it still fails, the page is a recorded failure
  (`network: robots.txt not read: ConnectTimeout ...`, `stats.robotsUnread`), not a
  robots.txt block, and the origin is asked again after 60 s. Before, one passing network
  error was reported as "robots.txt unreachable (HTTP 0)", counted in `robotsBlocked` and
  cut the whole origin off for the rest of the run (18 of 60 sites in one research run).
- A 4xx/5xx answer with an unexpected content-type is reported by its status first
  (`HTTP 402 (content-type: none)`), not by the content-type alone.
- String-array inputs such as `startUrls` also take `{"url": "..."}` objects, the shape
  other scraping platforms use, so their inputs can be reused as they are.
- Input validation enforces `pattern`; `acceptLanguage` rejects anything but language
  tags, so it cannot carry a second header.
- A start URL that answers 4xx or 5xx is now a recorded failure (`stats.failed`,
  `FAILED_REQUESTS`, a warning in the run log) in `website-content-crawler`,
  `contact-details`, `product-catalog` and the careers-page path of `job-postings`. Before,
  a wrong address ended as an empty run with status SUCCEEDED and nothing in the log.
- `website-content-crawler` counts pages that return no text in `stats.emptyPages` and logs
  a warning with a `render:true` hint; such pages are usually built by scripts.
- New input `acceptLanguage` on `website-content-crawler`: sent as `Accept-Language` with
  every request and passed to headless Chrome, so a site that picks its language from the
  visitor's location returns the same version on every run.
- Main content of listing pages: when a page holds several top-level `<article>` elements and
  none dominates, the Markdown keeps the list they share instead of the first item only (a
  200-page crawl of a book catalog: 1.0 price per listing page before, 10.89 after). One long
  article next to short teasers still wins on its own.
- docs/comparison.md: the same 200-page crawl measured on a hosted scraping platform at two
  memory sizes and on ww-scraper (time, pages, memory, cost, content on shared URLs).

## 0.9.0 (2026-11-12)

- Four ready actors for the categories people most often buy ready-made scrapers for, each
  built on official endpoints or published structured data:
  - `job-postings`: Greenhouse and Lever public job-board APIs, the posting list an Ashby
    board page publishes in its own HTML (the Ashby API host answers robots.txt with 401,
    which this crawler reads as a refusal) and schema.org `JobPosting` on any careers page; one normalised record (title, company, location,
    remote, salary, department, employment type, posted date, apply link); keyword,
    location and remote filters; `watchName` reports only postings new since the last run.
  - `places`: OpenStreetMap through public Overpass instances whose robots.txt allows API
    use, the next one tried when one does not answer; 32 ready categories or raw tags, in a city, a radius or a box; ODbL attribution in
    every record; tag values are validated before they reach the query.
  - `product-catalog`: schema.org Product, ProductGroup variants, AggregateOffer and Open
    Graph product tags; prices parsed in both `49,99` and `1,299.00` styles; duplicates
    removed by GTIN or SKU; merchant-feed problems flagged per record.
  - `contact-details`: e-mails, phones, social profiles, address and VAT id from the home,
    contact, about and imprint pages; role addresses kept, personal addresses and phones
    found only in running text masked unless `redactPii` is off.
- Page parsing keeps `mailto:` and `tel:` links (`Page.contact_links`), the text of
  `<footer>` and `<address>` (`Page.contact_text`) and `product:*` Open Graph properties.
- JSON endpoints are read through the same crawler (robots.txt, pacing, SSRF guard); DNS
  failures are tried again, while timeouts and resets are left to the crawler's own retries
  so attempts do not multiply. One failing board no longer ends a run.
- The crawler now bounds the whole response body, not only each read: a server that
  keeps trickling a few kilobytes at a time can no longer hold a fetch for minutes
  (`body_timeout`, default three times the read timeout and at least 60 s). A missed body
  deadline is reported, not retried.
- 19 new tests against a local server imitating the APIs, an Ashby board page and three
  kinds of websites, plus a server that drips its body (191 in total). Checked live on
  2026-11-12 against Greenhouse, Lever and Ashby boards (2,000 postings).

## 0.8.0 (2026-11-12)

- Web console at `/console` on the local API (`ww-scraper serve` prints the link).
  - Runs list with live status; a run page with counters, log, items table, abort and
    downloads in every export format; a start form generated from each actor's input
    schema (strings, numbers with limits, enums, booleans, string lists, JSON).
  - Three static files, no dependencies and no build step, shipped as package data.
  - Served with `script-src 'self'`, no inline script or style, `frame-ancestors 'none'`.
    The page holds no data and needs no token; the `/v2` routes it calls still do. The
    Host guard applies to the console too.
  - The token is passed in the URL fragment and kept in `sessionStorage`; scraped values
    are only ever written as text, and only `http(s)` values become links.
- `GET /v2/actor-runs/<id>/log` returns the last 256 KB of a run log (`CSP: sandbox`).
- A running run reports the items stored so far in `itemCount`.
- `GET /` redirects to `/console`.
- 9 new tests, one of them in headless Chrome with a hostile item (172 in total).

## 0.7.0 (2026-11-12)

- Duplicate pages are skipped (`ww_scraper/dedupe.py`, on by default in `crawl()`).
  - Queue keys sort query parameters and drop tracking parameters: `utm_*`, `mtm_*`,
    `gclid`, `fbclid`, `msclkid` and 20 other click ids. Values keep their encoding;
    repeated names keep their order.
  - Every fetched page registers its request URL, final URL and `rel=canonical`. A URL that
    leads to a registered page is not fetched, and a redirect to one stops after the
    first hop.
  - From canonicals the crawler learns, per host, which parameters and whether the
    trailing slash do not change the page: after 3 agreeing pages, never after one
    disagreeing page. A canonical is trusted only when it keeps the host and the path (up
    to the slash), so a site that points every page at its home page loses nothing.
  - Duplicates are counted in `stats["duplicates"]` and do not reach `on_page`.
    `crawl(..., dedupe=False)` or `crawler.dedupe = False` restores 0.6 behaviour.
  - A durable queue written by 0.6 resumes, but URLs whose key changed (parameter order,
    tracking parameters) can be fetched once more.
- `bench/crawl`: ww-scraper and Crawlee 3.18.2 on a generated 500-page site with the usual
  duplicate URLs, same budget (10 in flight). ww-scraper: 510 page downloads, 0 duplicate
  items, 1.6 s, 35 MB. Crawlee: 2,500 downloads, 2,000 duplicate items, 8.3 s, 389 MB.
  Method and limits in `bench/crawl/README.md`.
- Tests: 149 to 163.

## 0.6.0 (2026-11-12)

- Dataset export in six formats: `json`, `jsonl`, `csv`, `xlsx`, `xml` and `html`, from the
  CLI (`--format`, `--fields`, `--out`) and the local API (`format=`, `fields=`).
  - `xlsx` is written with the standard library. Text goes in inline-string cells, which
    a spreadsheet never evaluates, so `=HYPERLINK(...)` from a scraped page stays text.
    Checked by reading the file back with LibreOffice. Same items give the same bytes.
  - `xml` puts keys in a `name` attribute (scraped keys are rarely valid element names)
    and drops the characters XML 1.0 cannot hold.
  - `html` is one self-contained table, every value escaped, with a CSP that forbids
    scripts; the API also serves it with `Content-Security-Policy: sandbox`.
  - `fields` keeps only the named keys, in that order, in every format.
- Tests: 145 to 149.
- `bench/safety`: ww-scraper and Crawlee 3.18.2 against one hostile local site, measured
  from the server's request log (robots.txt, redirect to the metadata address, Retry-After,
  CSV formulas). Results and method in `bench/safety/README.md`.

## 0.5.0 (2026-11-12)

- Adaptive pacing per host. A 429 or 503 slows the whole host down, not only the request
  that got it: its interval doubles (first step 0.5 s, cap 60 s) and the run counts it as
  `throttled`. Every ten plain successes in a row ease the interval back by a quarter, and
  it never goes below the configured interval or the robots.txt Crawl-delay. The HTML run
  report shows the count. `crawler.adaptive = False` keeps the old fixed pacing.
- Tests: 140 to 145.

## 0.4.0 (2026-11-11)

- JavaScript rendering (`"render": true`): pages are re-read through a local headless
  Chrome. The page URL passes the crawler's gates first, and every connection the browser
  opens goes through a filtering proxy (`ww_scraper/proxy.py`) that refuses private,
  loopback and link-local addresses. If no browser is installed the raw HTML is used and
  the run records `renderFailed`.
- Concurrent crawling (`"maxConcurrency"`, up to 16): several pages in flight, never two
  requests to one host closer than `minIntervalSecs`. One robots.txt request per origin.
- Your own actors: `ww-scraper init <name>` writes a working one-file actor,
  `ww-scraper run ./file.py` runs it with the same gates as the built-in ones. The local
  API never loads a path.
- `ww-scraper runs` lists runs; `ww-scraper report <run-id>` writes a self-contained
  HTML report (no scripts, no external references, all values escaped).
- `change-monitor` revalidates instead of re-downloading: a page seen before is asked
  for with `If-None-Match` / `If-Modified-Since`, a 304 counts as unchanged (`notModified`
  in OUTPUT) and the links it had last time are still followed. Validators echoed back to a
  server are checked first (one printable line, at most 200 characters, a real HTTP date).
  `"conditionalRequests": false` turns it off.
- Tests: 122 to 140.

## 0.3.0 (2026-11-11)

- Renamed from `lustro` to `ww-scraper`: package `ww_scraper`, command `ww-scraper`,
  environment variables `WW_SCRAPER_*`, webhook headers `X-WW-Scraper-*`, User-Agent and
  robots token `ww-scraper`. There are no compatibility aliases for the old names.
- robots.txt is now parsed by `ww_scraper/robots.py` following RFC 9309: `*` and `$`
  wildcards, longest match, `Allow` wins a tie, most specific group. Matching is linear.
- New actors: `robots-audit`, `structured-data`, `change-monitor`, `feed-reader`
  (eleven in total).
- Pages now expose JSON-LD objects, Twitter card tags and advertised feeds.
- Tests: 102 to 122.

## 0.2.0

Local, compliant scraping platform primitives (datasets, key-value stores, request queues, runs), plus seven actors.

- Storage under `./storage/` (`WW_SCRAPER_STORAGE_DIR`): `Dataset` (JSONL, export
  json/jsonl/csv with formula-injection guard), `KeyValueStore` (files + content-type),
  durable `RequestQueue` (dedup by `unique_key`, resumable), run records
  `storage/runs/<id>.json` with READY/RUNNING/SUCCEEDED/FAILED/ABORTED, times, counters.
- `Actor` with a dict `input_schema` and a small validator (errors in English or Polish),
  `RunContext` (dataset, kv, queue, log, polite session, stats), `run_actor()`, resume
  via `--resume <run-id>`.
- `PoliteCrawler`: robots.txt per origin (cached, Crawl-delay), per-host interval with a
  0.5 s floor, retry/backoff on 429/5xx honouring Retry-After, max pages/depth,
  same-origin + include/exclude globs, SSRF check on every redirect hop, content-type
  filter, byte limit.
- Stdlib HTML extraction: main content to Markdown, metadata (title, description,
  canonical, og:*, lang, h1-h3, JSON-LD types, robots meta, hreflang), links, images.
- Actors: `website-content-crawler`, `sitemap`, `seo-audit`, `security-headers`,
  `llms-txt`, `link-checker`, `media-collector`.
- PII (e-mail, phone) redacted by default in stored output.
- Local API on 127.0.0.1 (`ww-scraper serve`, optional Bearer token, Host-header guard):
  `/v2/acts`, `/v2/acts/<name>/runs`, `/v2/actor-runs/<id>`, dataset items,
  key-value records. Signed (HMAC-SHA256) webhooks to allowlisted hosts only.
- CLI: `run`, `actors`, `export`, `serve`, `schedule` (prints cron + launchd, installs
  nothing), `testsite`; `ww-scraper "<query>"` media search unchanged.
- Offline test suite against a bundled local test site (robots Disallow, sitemaps,
  redirects, SSRF redirect, 429 Retry-After, security headers). 127.0.0.1 is reachable
  only through the explicit `allow_loopback` test switch.
- No new core dependencies (standard library + requests).
- Security hardening: API token required by default (random token printed once,
  `--no-auth` to opt out), JSON-only POST (415), foreign `Origin` refused (403);
  DNS-rebinding-safe connections (resolve once, validate all addresses, connect to the
  validated IP, re-check the peer, on every hop); address policy = globally routable
  unicast only, also for IPv4 embedded in IPv6 (mapped, 6to4, Teredo, NAT64);
  linear-time sitemap tokenizer.

## 0.1.0

First release.

- Compliant collector core: `search(query, sources=..., limit=..., verify=...)` returning
  live-verified `Resource` items with provenance and license.
- Sources: Openverse, Wikimedia Commons, Pexels (`PEXELS_API_KEY`), Met Museum (CC0).
- Compliance spine: per-host rate limiting (`PoliteSession`), `robots_ok()` gate for
  page-based sources, SSRF host allowlist with per-redirect checks, decompression-bomb
  guard, honest self-identifying User-Agent. No proxies, no evasion. See `COMPLIANCE.md`.
- Live verification: HTTP 200 + `image/*` + real decoded dimensions, byte-capped.
- CLI: `python -m ww_scraper "<query>" [--sources ...] [--limit N] [--no-verify] [--json]`.
- Tested: offline unit + structural tests; CI on every push/PR.
