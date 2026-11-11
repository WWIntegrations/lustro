# System map: ww-scraper

Each diagram shows one real path through this repository. A highlighted box is a step that can reject its input; a vertical bar marks the boundary between zones.

[Back to the README](../README.md)

## Every fetch passes four gates

PoliteCrawler is the only way actors fetch pages. A URL is refused before any request if it resolves to a private address or robots.txt does not allow it; allowed requests wait for the per-host rate limit, and every redirect hop goes through the same gates again.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/fetch-gates-dark.svg">
  <img src="system-map/fetch-gates-light.svg" alt="A URL passes the SSRF check, then the robots.txt check, then the per-host rate limiter, and only then is requested. Each redirect hop repeats the checks." width="100%">
</picture>

Source: [`ww_scraper/crawler.py`](../ww_scraper/crawler.py), [`ww_scraper/compliance.py`](../ww_scraper/compliance.py)

## How a robots.txt decision is made

robots.py picks the group for the crawler token, then the longest matching Allow or Disallow pattern; Allow wins a tie. A robots.txt that answers 401, 403 or 5xx blocks the whole host.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/robots-rules-dark.svg">
  <img src="system-map/robots-rules-light.svg" alt="The robots.txt of the origin is fetched once and parsed into groups. The group naming ww-scraper is used, otherwise the star group. The longest matching pattern decides, and Allow wins a tie." width="100%">
</picture>

Source: [`ww_scraper/robots.py`](../ww_scraper/robots.py), [`ww_scraper/compliance.py`](../ww_scraper/compliance.py), [`tests/test_robots.py`](../tests/test_robots.py)

## Run lifecycle

A run validates its input against the actor schema, then moves READY, RUNNING and ends SUCCEEDED, FAILED or ABORTED. An actor bug ends as FAILED and never crashes the host process.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/run-lifecycle-dark.svg">
  <img src="system-map/run-lifecycle-light.svg" alt="Input JSON is validated against the actor schema. A run record is created and the actor runs with a run context. The record ends as succeeded, failed or aborted with statistics." width="100%">
</picture>

Source: [`ww_scraper/actor.py`](../ww_scraper/actor.py), [`tests/test_actor_core.py`](../tests/test_actor_core.py)

## What a run writes

Each run has a dataset, a key-value store and a request queue that share the run id. The queue is durable, so an interrupted run resumes where it stopped.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/storage-layout-dark.svg">
  <img src="system-map/storage-layout-light.svg" alt="A run pushes items to an append-only JSONL dataset, keeps INPUT and OUTPUT in a key-value store, and tracks URLs in a durable deduplicated request queue. Datasets export as JSON, JSONL or CSV." width="100%">
</picture>

Source: [`ww_scraper/storage.py`](../ww_scraper/storage.py), [`tests/test_storage.py`](../tests/test_storage.py)

## Eleven actors, one crawler

All actors run through the same run context and crawler, so the compliance gates apply to each of them.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/actors-dark.svg">
  <img src="system-map/actors-light.svg" alt="Content actors crawl pages into Markdown, feeds and structured data. Audit actors report SEO, security headers, links, robots.txt and llms.txt. Monitoring compares content hashes between runs." width="100%">
</picture>

Source: [`ww_scraper/actors/__init__.py`](../ww_scraper/actors/__init__.py), [`tests/test_actors.py`](../tests/test_actors.py), [`tests/test_actors_v3.py`](../tests/test_actors_v3.py)

## Local HTTP API

The API binds to loopback only and always asks for a Bearer token. A foreign Host or Origin header is refused, which closes DNS rebinding and browser CSRF.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/local-api-dark.svg">
  <img src="system-map/local-api-light.svg" alt="A request to the local API must carry a loopback Host header, no foreign Origin and a valid Bearer token. Only then is a run started or a dataset returned." width="100%">
</picture>

Source: [`ww_scraper/api.py`](../ww_scraper/api.py), [`tests/test_api_webhooks_schedule.py`](../tests/test_api_webhooks_schedule.py)

## Signed webhooks

After a run, an event is sent only to an allowlisted HTTPS host and only when a secret is set. The receiver checks the HMAC signature and timestamp.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/webhooks-dark.svg">
  <img src="system-map/webhooks-light.svg" alt="A finished run produces an event. It is sent only to hosts on the allowlist, over HTTPS, without following redirects, signed with HMAC SHA-256. The receiver verifies the signature." width="100%">
</picture>

Source: [`ww_scraper/webhooks.py`](../ww_scraper/webhooks.py), [`tests/test_api_webhooks_schedule.py`](../tests/test_api_webhooks_schedule.py)

## Licensed media collector

Media comes only from official APIs. Every asset is fetched and checked before it is returned, and carries author, license and source page.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/media-dark.svg">
  <img src="system-map/media-light.svg" alt="A query goes to official source APIs. Each returned asset is checked against the source host allowlist and fetched to confirm a real image within size limits. Results carry license and provenance." width="100%">
</picture>

Source: [`ww_scraper/sources/base.py`](../ww_scraper/sources/base.py), [`ww_scraper/verify.py`](../ww_scraper/verify.py), [`tests/test_sources.py`](../tests/test_sources.py)

## How it is verified

Tests run offline against a bundled local site that exercises every compliance path. CI runs the same suite on each push and pull request.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="system-map/verification-dark.svg">
  <img src="system-map/verification-light.svg" alt="A bundled local site serves robots rules, redirects, a redirect into a private address, rate limit answers and feeds. The test suite runs every actor against it offline, and CI repeats this on each push." width="100%">
</picture>

Source: [`ww_scraper/testsite.py`](../ww_scraper/testsite.py), [`tests/conftest.py`](../tests/conftest.py), [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
