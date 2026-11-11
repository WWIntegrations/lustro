# Changelog

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

Local, compliant mirror of Apify-style platform primitives, plus seven actors.

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
