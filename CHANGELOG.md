# Changelog

## 0.2.0

Local, compliant mirror of Apify-style platform primitives, plus seven actors.

- Storage under `./storage/` (`LUSTRO_STORAGE_DIR`): `Dataset` (JSONL, export
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
- Local API on 127.0.0.1 (`lustro serve`, optional Bearer token, Host-header guard):
  `/v2/acts`, `/v2/acts/<name>/runs`, `/v2/actor-runs/<id>`, dataset items,
  key-value records. Signed (HMAC-SHA256) webhooks to allowlisted hosts only.
- CLI: `run`, `actors`, `export`, `serve`, `schedule` (prints cron + launchd, installs
  nothing), `testsite`; `lustro "<query>"` media search unchanged.
- Offline test suite against a bundled local test site (robots Disallow, sitemaps,
  redirects, SSRF redirect, 429 Retry-After, security headers). 127.0.0.1 is reachable
  only through the explicit `allow_loopback` test switch.
- No new core dependencies (standard library + requests).
- Security hardening: API token required by default (random token printed once,
  `--no-auth` to opt out), JSON-only POST (415), foreign `Origin` refused (403);
  DNS-rebinding-safe connections (resolve once, validate all addresses, connect to the
  validated IP, re-check the peer, on every hop); linear-time sitemap tokenizer.

## 0.1.0

First release.

- Compliant collector core: `search(query, sources=..., limit=..., verify=...)` returning
  live-verified `Resource` items with provenance and license.
- Sources: Openverse, Wikimedia Commons, Pexels (`PEXELS_API_KEY`), Met Museum (CC0).
- Compliance spine: per-host rate limiting (`PoliteSession`), `robots_ok()` gate for
  page-based sources, SSRF host allowlist with per-redirect checks, decompression-bomb
  guard, honest self-identifying User-Agent. No proxies, no evasion. See `COMPLIANCE.md`.
- Live verification: HTTP 200 + `image/*` + real decoded dimensions, byte-capped.
- CLI: `python -m lustro "<query>" [--sources ...] [--limit N] [--no-verify] [--json]`.
- Tested: offline unit + structural tests; CI on every push/PR.
