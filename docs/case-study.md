# Case study: building a scraper that stays inside what a site allows

ww-scraper started as a small media collector and grew, in five releases, into a local
platform with the primitives of a hosted scraping service: datasets, key-value stores,
request queues, actors, runs, an HTTP API and webhooks. This page records the decisions
behind it and the defects found on the way, because those say more about the code than a
feature list.

## The constraint that shaped everything

Most scraping tools treat robots.txt, rate limits and private networks as settings. Here
they are not settings. There is no switch to ignore robots.txt, no proxy rotation and no
anti-bot evasion, and the code is arranged so that every request, including every redirect
hop and every connection a headless browser opens, passes the same gates:

1. The target address must be globally routable unicast. Private, loopback, link-local and
   IPv4 addresses hidden inside IPv6 forms are refused.
2. DNS is resolved once, validated, and the connection is pinned to that address, so a
   second lookup cannot swap in an internal IP (DNS rebinding).
3. robots.txt is parsed per RFC 9309 and checked for that exact URL.
4. A per-host rate limiter spaces requests, also when several pages are in flight.

## Releases

| Release | What it added | What it fixed or hardened |
|---|---|---|
| 0.1 | Licensed media collector (Openverse, Wikimedia, Pexels, The Met), live verification of every image | |
| 0.2 | platform-style datasets, key-value stores, request queues, runs; seven actors; local API | API token required by default; DNS-rebinding-safe connections; linear sitemap reader |
| 0.3 | RFC 9309 robots parser; robots-audit, structured-data, change-monitor, feed-reader | Robots matching is linear in URL length (no regular-expression backtracking) |
| 0.4 | JavaScript rendering, concurrent crawling, one-file actors, runs list, HTML report | Browser traffic forced through a filtering proxy; output capped while reading |
| 0.4 | change-monitor revalidates with ETag / Last-Modified | Validators checked before they are sent back to a server |

## Defects worth describing

**A headless browser can leave by doors the proxy does not see.** Rendering points Chrome
at a local proxy that refuses private addresses. A security review of that change found
three ways around it: WebRTC opens UDP connections outside the proxy, QUIC does the same,
and Chrome can resolve names itself over DNS-over-HTTPS. All three are now closed with
browser flags, and a test asserts the flags are present. The test page used in the suite
tries to reach the cloud metadata address `169.254.169.254`, and the proxy's refusal is
checked.

**A size cap that only applied after the damage.** The first version of rendering read
Chrome's whole output and then compared it with the limit. A hostile page could make the
process allocate far more than the cap. The browser output is now read in chunks and the
process is killed as soon as the cap is passed. A test feeds an endless stream and checks
that the call returns.

**Concurrency multiplied robots.txt requests.** With several pages in flight, every worker
that met a new origin fetched its robots.txt. A per-origin lock now makes the first worker
fetch it and the rest wait for that answer, so a site sees one robots.txt request.

**A monitor that downloads what has not changed.** change-monitor re-fetched every page on
every run. It now asks with `If-None-Match` and `If-Modified-Since`. A 304 counts as
unchanged without a body. Because a 304 has no links to parse, the links the page had last
time are kept in the snapshot and still followed; without that, pages reachable only
through an unchanged page would drop out of the monitor. The validators come from the
server and go back to it, so they are checked first: one printable line, at most 200
characters, and a real HTTP date.

## How it is tested

- An in-process test site serves every case the crawler must handle: robots rules with
  wildcards, redirects into private addresses, redirect loops, 429 with `Retry-After`,
  flaky 500s, oversized bodies, gzip sitemaps, feeds, a page that needs JavaScript, and a
  page that answers 304.
- Tests went from 122 at 0.3.0 to 140, and CI runs them on every push, including the
  rendering tests against a real Chrome.
- Each defect above has a test that failed before its fix.

## What it does not try to be

It runs on one machine and will not scale out to many. It has eleven built-in actors, not
a marketplace of thousands. It will not collect from a site that forbids automated access.
[`comparison.md`](comparison.md) sets this out next to what hosted platforms offer.
