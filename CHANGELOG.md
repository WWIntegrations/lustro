# Changelog

## Unreleased

- `places` checks each Overpass instance's `/api/status` (10 s) before the query. A server
  whose web front answers while the backend hangs used to cost two 75 s timeouts and five
  minutes in all; it now fails in about 10 s with a message saying what to do.
  `WW_SCRAPER_OVERPASS` (comma-separated https URLs) sets instances of your own or of an
  operator you trust. No other public instance is added: the main one disallows the API in
  robots.txt, one is for listed users only, one covers a single country, and the choice of
  a third-party operator is left to whoever runs the scraper.
- `places` output says `phonesMasked`: business phones come from open data, but a sole
  trader's number is personal data, so they stay masked unless `redactPii` is off.
Defects found while measuring 150 local business sites; seven of them changed results.

- `contact-details` read nothing from the third or fourth start URL on: each site's crawl
  used the run's durable queue, which already counted the pages of the sites before it.
  Every site now gets its own page budget.
- `contact-details` takes address and phone from every schema.org business subtype
  (Dentist, BeautySalon, HairSalon, CafeOrCoffeeShop, FoodEstablishment and the rest of
  the LocalBusiness tree), not only from a short list.
- `sitemap`: `maxSitemaps` and `maxUrls` apply per origin, and each origin reports
  `truncated`. One site with 300,000 URLs used to leave every site after it at `urls: 0`.
- `places`: `onlyWithWebsite` is part of the Overpass query, so `maxResults` counts places
  with a website (before, it cut the list first, inside one map tile). The second default
  server was the same machine as the first and is gone; a failure suggests `endpoint`. A
  busy answer (429/504) is tried once more after 30 s.
- `llms-txt`: a UTF-8 BOM (written by Yoast and All in One SEO) no longer fails the H1 check,
  an empty file is not `present`, and a file that could not be checked is `present: null`.
- `seo-audit`: a page with no document (dead host, error without HTML) scores 0, not 90; a
  start URL that could not be read is a dataset item with `measured: false`; `sitemapXml`
  and `llmsTxt` are `null` when they could not be checked.
- robots.txt messages: a 401/403 names the server and says it is usually a bot filter; a
  block after a redirect names the other site's robots.txt; TLS errors give the cause
  (certificate for another host, expired, self-signed, incomplete chain).
- The CLI no longer prints a dataset path for a run that wrote no items.
- Hostile markup can no longer stall or crash extraction: tables nested in table cells
  used to be rendered again at every level above them (20 levels took 7 s, each further
  level doubled it), and pages nested thousands of elements deep hit Python's recursion
  limit. Tables now walk only their own rows, the tree stops nesting at 160 levels, and
  the share-bar check walks at most 200 elements per candidate.
- Python client: a base URL with `\`, `@`, whitespace or control characters is refused, and
  the host must be the same literal loopback IP for `urlsplit` and for urllib3, which is
  what requests connects to. Before, `http://evil.com\@127.0.0.1:8765` passed the loopback
  check and the Bearer token went to evil.com. The JavaScript client refuses the same
  characters and checks that every request stays on the base URL's origin.
- API: `Content-Length` must be plain digits. `-1` used to read the body to the end of the
  connection, past the 1 MB cap; `+5`, `1_0` and the like are refused too. A refused body
  now also stops the handler: a PUT over 1 MB used to answer 413 and then store an empty
  record.
- NaN and infinity are refused everywhere a number comes in: JSON bodies, `checks` and
  webhooks on the API, MCP messages and CLI `--input` are parsed without `NaN`/`Infinity`
  (and `1e999`), `"type": "number"` fields reject non-finite values, and `?timeout=nan` is a
  400. Before, `timeoutSecs: NaN` passed `minimum: 0`, its timer never fired and a
  synchronous call lost its 300 s cap.
- MCP: every `call_actor` run has caps on time, stored items, HTTP requests and bytes
  (defaults 300 s, 1000, 2000, 200 MB; ceilings 3600 s, 10000, 20000, 1 GB), set with new
  arguments `maxStoredItems`, `maxRequests`, `maxBytes` or as run options in `input`. New
  `ww-scraper mcp --allow-host HOST` (repeatable) limits MCP runs to those hosts: start URLs
  on other hosts are refused, and the crawler checks every request and redirect.
- Runs started with `ctx.call_actor()` (pipeline and lead-list steps) are held to the
  caller's budgets: at most what is left of its `maxRequests` and `maxBytes`, at most its
  `maxItems`, and what they use counts against the caller (`stats.calledRequests`,
  `stats.calledBytes`). Before, a step under a capped run had no cap of its own.
- MCP: `statusMessage` and check names in `call_actor`, `get_run` and `list_runs` come back
  as `{"untrusted": "..."}`, capped at 2000 characters, since they can quote a web page.
- With `--allow-host`, rendered pages are held to the allowlist as well: the browser's
  filtering proxy refuses every other host, subresources included. Before, only the
  crawler's own requests were checked and the browser could load any public host.
- Stored and logged URLs lose their userinfo (`user:password@`), and the values of
  credential-like query parameters (`key`, `token`, `access_token`, `signature`, `password`,
  `code` and similar) become `[secret]`. Secret input values are masked in every
  URL-encoded form (any safe set, upper or lower hex) and JSON-escaped, not only as is.
- Secret input values are masked as `[secret]` (also URL-encoded) in the run record's
  `statusMessage`, in `FAILED_REQUESTS` and in `ctx.set_status_message()`, not only in the
  log, so the API, MCP and webhook payloads no longer carry them. Actors can use
  `ctx.mask(text)`.
- Webhooks: an entry that is not a URL string or `{requestUrl, eventTypes?}` is skipped with
  a log line instead of raising before the run started, which left it RUNNING; the API
  answers 400 for such a list.
- Record keys are at most 200 characters (with the `.json` sidecar and the temporary name,
  a 256-character key went over the file-name limit). `delete_run` takes only run ids, so a
  named storage cannot be removed through it. Secret values of a run that is aborted while
  waiting for a slot are dropped, and at most 1024 prepared runs hold secrets in memory.
- The browser's filtering proxy sends one request per plain-HTTP connection
  (`Connection: close`, nothing after the request body is relayed). Before, a kept-alive
  browser connection sent later requests for other hosts to the first host. Chunked request
  bodies through it are refused.

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
- `"autoscaleConcurrency": true` on every actor with `maxConcurrency`: workers start at two
  and move between one and `maxConcurrency` with the machine's 1-minute load per CPU
  (down above 0.9, up below 0.6) and, on Linux, available memory (down under 10%). Off
  by default; per-host spacing and Crawl-delay are unchanged. The current value is the
  `concurrency` counter.
- Browser interaction: `waitForSelector` and `browserActions` (`click`, `scroll`, `wait`,
  `waitForSelector`; at most 20, nothing else) on every actor that renders. With any of
  them, or with screenshots or PDFs, Chrome is driven over `--remote-debugging-pipe`
  (stdlib client in `ww_scraper/cdp.py`, no debugging port) behind the same filtering
  proxy and flags. Invalid actions are refused before the run starts.
- Screenshots and PDFs: the new `screenshot` actor, and `screenshot` (`viewport` or
  `fullPage`), `screenshotFormat` and `pdf` on `website-content-crawler`. Files go to the
  run's key-value store as `screenshot-<hash>.png|jpg` and `page-<hash>.pdf`; items carry
  their keys and API paths. The console shows thumbnails (CSP `img-src` now allows `blob:`
  for them) and `ww-scraper report` embeds up to 60.
- `"render": "auto"`: pages are rendered only when their HTML looks built by scripts (an
  empty app root, a noscript notice, almost no text beside scripts), and a host stops
  being rendered after three renders that added no text. New counter `renderSkipped`.
- `ww-scraper mcp`: an opt-in Model Context Protocol server over stdio (JSON-RPC 2.0, one
  message per line) with `list_actors`, `describe_actor`, `call_actor`, `get_run`,
  `list_runs` and `get_dataset_items`; `--read-only` drops `call_actor`. Runs use the same
  gates as the CLI, loopback stays refused, results are capped (1000 items, 200,000
  characters, 3600 s) and marked as untrusted content. No network port is opened.
- New actors, each built on published structured data or an official API:
  `articles` (schema.org Article, NewsArticle, BlogPosting and subtypes, with Open Graph
  article tags as a fallback; `publishedAfter`, optional Markdown body, rich-result gaps in
  `issues`), `events` (schema.org Event and its subtypes: dates, status, place, address,
  organizer, performers, cheapest offer; past events skipped by default) and `package-info`
  (npm, RubyGems and Packagist JSON APIs; maintainer names and e-mails are never read).
  PyPI is not supported because pypi.org's robots.txt disallows its JSON API.
- `article:*` Open Graph tags are now kept in a page's `og` data next to `og:*` and
  `product:*`.
- Console screens: Tasks (saved tasks, run with one click), Storage (named datasets and
  key-value stores, optionally every run storage, with newest items, keys and downloads)
  and History (runs per day by final status over 30 days from `/v2/stats/runs`, with a
  legend, a table view, success rate, median duration and the latest 50 runs). The chart
  is built with DOM calls under the same strict Content-Security-Policy.
- Run times in any ISO 8601 form are now read by `purge` and `/v2/stats/runs`; before, a
  record with a time not written by ww-scraper itself was skipped.
- RAG output: `--format chunks` on `export` (`?format=chunks&chunkSize=&chunkOverlap=` on the
  API) writes one JSON line per Markdown chunk with a stable `id`, `url`, `title`,
  `headingPath`, `chunkIndex`, `chunkCount` and `text`. Chunks follow headings, then
  paragraphs, with a character overlap; fenced code is never split at a `#` line.
  `ww_scraper.loaders.DatasetLoader` loads a dataset (by id or name) as documents with
  `page_content` and `metadata`, as a LangChain `BaseLoader` when `langchain_core` is
  installed and without any dependency otherwise; `to_llama_index()` converts for
  LlamaIndex.
- Post-run checks: `"checks"` in the run options (`--checks JSON` on `run`, `task save` and
  `task run`, `?checks=` on the API, or a top-level input key) with `minItems`, `maxItems`,
  `maxFailed`, `maxFailedRatio`, `maxDurationSecs`, `statuses` and `fieldFillRate`
  (`{"email": 0.5}`, dotted paths allowed). They are evaluated after the run and stored on
  the record as `checks` (`passed` plus one result per check); the status is not changed.
  The CLI exits with code 3 when a check failed, and webhooks without `eventTypes` also get
  `ACTOR.RUN.CHECKS_FAILED`.
- Run timeout: `--timeout SECS` on `ww-scraper run`, `?timeout=` on the API, or
  `"timeoutSecs"` as a top-level input key. A run over its time is asked to stop, shows
  TIMING-OUT while the actor winds down and ends TIMED-OUT with the items it stored. A stop
  asked through the API shows ABORTING until the actor stops. New webhook event
  `ACTOR.RUN.TIMED_OUT`.
- Live status messages: `ctx.set_status_message(text)` writes the text to the run record at
  once (redacted like any output); `ww-scraper runs`, `GET /v2/actor-runs/<id>` and the
  console run page show it.
- Named storages: `ctx.dataset("name")`, `ctx.kv("name")` and `ctx.queue("name")` open a
  storage shared by every run that uses the name; the run's own storages stay per run.
  Items pushed to a named dataset are redacted like `ctx.push_data`. The run option
  `datasetName` (CLI `--dataset NAME`, API `?dataset=`) appends a run's items to a named
  dataset, so weekly runs can build one list. A name cannot look like a run id.
- `ww-scraper purge --older-than 30d [--dry-run]` deletes finished runs older than the age,
  with their record, log, report and unnamed storages. Named storages and runs still going
  are never touched; `--dry-run` lists what would go and its size.
- Tasks: `ww-scraper task save <name> <actor> --input JSON [--timeout] [--dataset]`, `task
  list`, `task show`, `task run <name> [--input JSON]` (merged over the saved input key by
  key), `task delete`. Stored as `storage/tasks/<name>.json`, validated when saved and when
  run; the run record carries `taskName`.
- Schedules: `ww-scraper schedule --task NAME` prints a cron line and plist that run the
  task; `@hourly`, `@daily`, `@weekly`, `@monthly` and `@yearly` are accepted; the output
  says which time zone applies and `--tz` adds a `CRON_TZ` line. Still prints only.
- Export views on `ww-scraper export` and `GET /v2/datasets/<id>/items`: `omit` (keys or
  dotted paths), `flatten` (nested objects become `a.b` keys, for CSV and XLSX), `unwind
  FIELD` (one row per list element, object elements merged into the row), `desc` (newest
  first), `clean` (no `#hidden` keys, no empty items); `fields` now takes dotted paths.
- `--format rss` (API `format=rss`): an RSS 2.0 feed from items with a title, name or url
  field, with `pubDate` from the first date-like field; only http(s) values become links.
- Actor chaining: `ctx.call_actor(name, input)` runs another actor in the same process and
  returns its final record. The called run has its own run id and unnamed storages,
  `parentRunId` pointing back (the caller lists `childRunIds`), at most the caller's
  remaining time, and stops when the caller is aborted; nesting stops at four levels.
- `lead-list` actor: `places`, then `contact-details` on every place with a website, merged
  into one record per business (name, category, address, website, e-mails, phones, social
  profiles, source with the OSM link, ODbL attribution and pages read). The PII defaults of
  both actors are kept.
- `pipeline` actor: steps run one after another, a field of one step's items feeding the
  start URLs of the next (`sitemap` then `seo-audit`); every step's input is validated before
  the first one starts.
- Run budgets: `maxItems`, `maxRequests` and `maxBytes` (CLI `--max-items` and so on, API
  query parameters, top-level input keys or task options) stop a run cleanly: it ends
  SUCCEEDED with `stopped at maxItems=100`, and `maxItems` keeps exactly that many items.
- Actor state: `ctx.use_state(default)` returns a dict saved to the run's key-value store
  (`STATE`) every 60 seconds and whenever the run ends, in any status; a resumed run gets
  it back.
- Request queue: `add(url, forefront=True)` puts a request at the head of the queue, and
  `add_many(requests, forefront=False)` adds a batch in one journal write; the journal
  replays both in the same order after a crash.
- `POST /v2/actor-runs/<id>/resurrect[?timeout=]` runs a finished run (SUCCEEDED, FAILED,
  ABORTED or TIMED-OUT) again with its stored input and its storages; 409 for a run that is
  still going. The console run page has a "Run again" button for it.
- Webhook events `ACTOR.RUN.CREATED` and `ACTOR.RUN.RESURRECTED`, sent when a run starts or
  is resumed, to webhooks that list them in `eventTypes`; without `eventTypes` a webhook
  still gets only the four finish events.
- Input schemas: `pattern` (also on string list items), `errorMessage` (one text, or per
  keyword), YYYY-MM-DD dates for `editor: "datepicker"`, and form hints `prefill` (shown,
  not applied like `default`), `sectionCaption` / `sectionDescription` (grouped in the
  console form), `editor` (`textarea`, `json`, `datepicker`, `hidden`).
- Secret inputs: a field with `"secret": true` reaches the run but is never written to the
  stored `INPUT`, a task file or the run log (its value is replaced by `[secret]`); with
  `"env": "NAME"` it is read from that environment variable when not given, which is also
  how a task or a resumed run gets it. The console shows it as a password field.
- Actor schemas (`ww_scraper/schema.py`):
  - `output_schema`: named outputs with URL templates, resolved into the run record as
    `output` and listed on the console run page;
  - `dataset_schema`: a JSON Schema for one item, checked on every push (one invalid item
    rejects the whole push with `SchemaValidationError`, counted as `invalidItems`), plus
    named views used by `export --view NAME` and `?view=NAME`;
  - `kv_schema`: collections of keys (`key` or `keyPrefix`) with allowed content types and
    a JSON Schema, checked on `ctx.set_value`; `KeyValueStore.keys(collection=...)`.
  - `job-postings`, `places`, `contact-details`, `product-catalog` and `lead-list` declare
    item schemas and an overview view.
- API: `GET /v2/acts/<name>`; `POST /v2/acts/<name>/run-sync-get-dataset-items` (one call
  in, items out, in any export format and view) and `.../run-sync` (returns `OUTPUT`), both
  capped at 300 s; list, read and delete datasets, key-value stores and request queues
  (`?named=1` for named ones; a running run's storages are refused with 409); store keys
  with `?collection=`; `PUT` and `DELETE` of one record; queue `requests` and `head`;
  `DELETE /v2/actor-runs/<id>`; task routes (`/v2/actor-tasks`); run statistics
  (`/v2/stats/runs`); and the whole API as OpenAPI 3.1 at `/v2/openapi.json` and in
  `docs/openapi.json`.
- `ww_scraper.client`: `Client` and `AsyncClient` for a running `ww-scraper serve`: runs
  (start, wait, call, run-sync items, abort, resurrect, log), storages, records, tasks and
  stats; connection errors, 429 and 5xx retried with exponential backoff and Retry-After;
  refuses a non-loopback base URL.
- `clients/js/ww-scraper-client.mjs`: the same client for Node 18+ in one dependency-free
  file; other languages can generate one from `docs/openapi.json`. Like the Python client
  it accepts only a literal loopback base URL (`127.x.x.x` with every octet <= 255, or
  `[::1]`); `localhost` and names such as `127.0.0.1.nip.io` are refused.
- `ww-scraper serve --max-runs N`: at most N runs execute at once; the others stay READY
  with a status message until a slot is free, and can be aborted while they wait.
- Actor versions: `Actor.version` (the package version when not set) is shown by
  `GET /v2/acts` and written to each run record as `actVersion`.

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
