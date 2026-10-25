# Lustro

Compliant, open-source local mirror of the useful primitives of commercial scraping
platforms (Apify-style datasets, key-value stores, request queues, actors, runs and a
small HTTP API), plus seven ready actors and the original licensed media collector.
Everything runs on your machine, in the open, strictly inside what each site permits:
robots.txt always, a per-host rate limit, an honest User-Agent with a contact, and
personal data redacted by default.

## Principles (what makes Lustro different)

- **Official APIs or robots-allowed pages only.** Each source is an official API, or a
  public page explicitly allowed by its `robots.txt`, re-checked on every run.
- **No evasion, ever.** Lustro never uses residential/rotating proxies, never rotates
  identities to dodge rate limits, never solves CAPTCHAs, never bypasses anti-bot
  measures. A source that asks automated clients to stay out (e.g. HTTP 403 "automated
  access blocked") is *skipped*, not worked around.
- **Live verification.** Every returned asset is fetched and checked: HTTP 200, a real
  `image/*` content-type, real decoded dimensions, a byte cap, and a decompression-bomb
  guard. No dead links.
- **Provenance + license on every item.** Author, license, license URL and source page
  travel with each result. Untrusted text is sanitized; URLs are restricted to http(s).
- **SSRF-safe.** Asset fetches follow a per-source host allowlist, every redirect hop is
  re-checked, and private / loopback / link-local addresses are refused.
- **No secrets in the repo.** API keys (e.g. `PEXELS_API_KEY`) are read from the
  environment only.

See [`COMPLIANCE.md`](COMPLIANCE.md) for how each risk criterion - data type (GDPR),
copyright, ToS / robots.txt, server load, and no-evasion - maps to Lustro's behavior.

## Platform primitives (v0.2)

Stored under `./storage/` (or `$LUSTRO_STORAGE_DIR`), laid out like `apify_storage`:

| Primitive      | What it is                                                              |
|----------------|-------------------------------------------------------------------------|
| `Dataset`      | append-only JSONL; export as `json`, `jsonl` or `csv` (formula-safe)     |
| `KeyValueStore`| named files with their content-type (`INPUT`, `OUTPUT`, `llms.txt`...)   |
| `RequestQueue` | durable, deduplicated by `unique_key`, resumable after a crash           |
| `Actor`        | name + `input_schema` (validated, errors in English or Polish) + `run(ctx)` |
| `RunContext`   | input, dataset, kv, queue, log, polite session, statistics              |
| Run record     | `storage/runs/<id>.json`: READY / RUNNING / SUCCEEDED / FAILED / ABORTED, times, counters |

`PoliteCrawler` is the only way actors fetch pages: robots.txt per origin (cached, with
Crawl-delay), a minimum interval per host, retries with backoff on 429/5xx that honour
`Retry-After` (and give up if asked to wait longer than the cap), `max_pages` /
`max_depth`, same-origin scope with include/exclude globs, the SSRF guard on every
redirect hop, a content-type filter and a byte limit. HTML is turned into Markdown and
metadata with the standard library only (`html.parser`).

## Actors

| Actor                     | Output                                                              |
|---------------------------|---------------------------------------------------------------------|
| `website-content-crawler` | main content of each page as Markdown (for RAG); noindex pages skipped |
| `sitemap`                 | URLs from robots.txt `Sitemap:`, `/sitemap.xml`, indexes, `.xml.gz`, text sitemaps |
| `seo-audit`               | per page: status, title/description length, h1 count, canonical, robots, hreflang, OG, JSON-LD, image alt; per site: robots.txt, sitemap, `/llms.txt` |
| `security-headers`        | per origin: CSP, HSTS, XFO / frame-ancestors, XCTO, Referrer-Policy, Permissions-Policy, COOP/CORP, cookie flags; grade A-F with reasons |
| `llms-txt`                | checks `/llms.txt` + `/llms-full.txt` against llmstxt.org, or drafts one (`"mode": "generate"`) |
| `link-checker`            | broken links (internal and external) and redirect chains            |
| `media-collector`         | the v0.1 sources below, as an actor                                 |

```bash
lustro actors                                   # list actors and input fields
lustro run seo-audit --input '{"startUrls": ["https://example.com/"], "maxPages": 10}'
lustro run website-content-crawler --input-file input.json --print-items
lustro run sitemap --resume <run-id>            # continue an interrupted run
lustro export <dataset-id> --format csv
```

Try it offline against the bundled test site (the `--allow-loopback` switch is for
tests only; by default 127.0.0.1 and every private address are refused):

```bash
lustro testsite --port 8766 &
lustro run seo-audit --allow-loopback --storage /tmp/lustro-demo \
  --input '{"startUrls": ["http://127.0.0.1:8766/"], "minIntervalSecs": 0}'
```

Python:

```python
from lustro import run_actor
rec = run_actor("link-checker", {"startUrls": ["https://example.com/"]})
print(rec["status"], rec["itemCount"], rec["defaultDatasetId"])
```

### Local API

```bash
lustro serve --port 8765               # binds to 127.0.0.1 only
# a Bearer token is always required: --token / $LUSTRO_API_TOKEN, otherwise a random
# one is generated and printed once at start; --no-auth disables it explicitly
```

| Route | |
|---|---|
| `GET /v2/acts` | list actors with input schemas |
| `POST /v2/acts/<name>/runs?waitForFinish=60` | start a run, body = input JSON |
| `GET /v2/actor-runs/<id>` | run record (`POST .../abort` to stop) |
| `GET /v2/datasets/<id>/items?format=json\|jsonl\|csv` | items |
| `GET /v2/key-value-stores/<id>/records/<key>` | a record with its content-type |

Requests with a non-loopback `Host` header (DNS-rebinding guard) or a non-loopback
`Origin` header (browser CSRF guard) get 403; POST bodies must be `application/json`
(415 otherwise).

### Webhooks

After a run, Lustro can POST `ACTOR.RUN.SUCCEEDED|FAILED|ABORTED` events (CLI
`--webhook URL`, or the API `webhooks` query parameter as base64 JSON
`[{"requestUrl": ..., "eventTypes": [...]}]`). Only hosts in `LUSTRO_WEBHOOK_HOSTS`
(comma separated) are contacted, over HTTPS, without following redirects, and only
when `LUSTRO_WEBHOOK_SECRET` is set. Each request carries `X-Lustro-Timestamp` and
`X-Lustro-Signature: sha256=HMAC_SHA256(secret, "<timestamp>." + body)`;
`lustro.webhooks.verify()` checks it on the receiving side.

### Scheduling

`lustro schedule <actor> --cron '0 6 * * 1' --input '<json>'` only **prints** a crontab
line and a launchd plist. Installing them is the operator's decision.

## Media sources (v0.1)

| Source     | Access                              | License info       |
|------------|-------------------------------------|--------------------|
| Openverse  | `api.openverse.org` (anonymous)     | yes (CC / PDM)     |
| Wikimedia  | Commons MediaWiki API               | yes (per-file)     |
| Pexels     | `api.pexels.com` (`PEXELS_API_KEY`) | Pexels License     |
| Met Museum | Open Access API (anonymous)         | CC0 (public domain)|

Every adapter implements the same `Source` contract and declares the hosts its assets
may live on (the SSRF allowlist used during verification). New sources are added only
after they pass the compliance checklist above. Pexels needs `PEXELS_API_KEY` in the
environment; without it that source simply yields nothing.

## Install

```bash
pip install -e .
# core needs only the standard library + requests; Pillow is used by media verification
```

## Usage

```python
from lustro import search

for r in search("brutalist concrete architecture", sources=["openverse"], limit=12):
    print(r.title, "-", r.author, f"({r.license})", r.url, f"{r.width}x{r.height}")
```

CLI:

```bash
python -m lustro "brutalist concrete architecture" --sources openverse --limit 12 --json
```

Set a contact for the User-Agent (polite crawling):

```bash
export LUSTRO_CONTACT="https://github.com/<you>/lustro"
```

## What Lustro deliberately does NOT do

Residential/rotating proxies, identity rotation to defeat rate limits, CAPTCHA solving,
headless-browser anti-bot evasion, ignoring robots.txt (there is no switch for it),
collecting from sources that forbid automated access, multi-tenant hosting, or
scraping on behalf of an MCP/agent tool. These are out of scope **by design** - the point is a collector you can run in
the open, that respects the sites it touches.

## Known limits

- robots.txt matching uses `urllib.robotparser`: no `*` / `$` wildcards inside paths.
- DNS rebinding is closed for crawler, robots.txt and webhook requests: the host is
  resolved once at connect time, every address is validated, the socket goes to that
  IP (Host/SNI unchanged) and the peer is re-checked, on every redirect hop. Those
  sessions ignore `HTTP(S)_PROXY`, because a proxy would resolve names itself. The
  v0.1 media verification still uses its per-source host allowlist instead.
- Sitemaps are read by a single linear pass over tag boundaries (no XML parser), so
  unusual but valid XML constructs beyond the sitemap vocabulary are ignored.
- PII redaction is a heuristic for e-mail addresses and phone numbers, not a full
  personal-data classifier.
- No JavaScript rendering: pages that only build content in the browser yield little.
- Crawls are sequential (one request at a time) by design.

## License

MIT. See [`LICENSE`](LICENSE).
