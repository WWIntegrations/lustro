# Crawl benchmark: ww-scraper and Crawlee on a 500-page site full of duplicates

[Crawlee](https://crawlee.dev) is a widely used open-source crawler library for Node.js. This
benchmark crawls one generated local site with both tools and counts, from the server's own
request log, what each one fetched. Time, CPU and peak memory come from `/usr/bin/time -l`
around each tool's process, so neither tool grades itself.

The site ([`bench_site.py`](bench_site.py)) has 500 pages, `/p/0` to `/p/499`, all reachable
within 5 hops. Like a real shop, every page is also linked under other URLs:
`#part`, `?utm_source=..&utm_medium=..`, `?gclid=..`, `?b=2&a=1`, `?a=1&b=2`, a trailing
slash, `?view=print`, and `/old/<n>` (301 to `/p/<n>`). The variants that serve content
carry `<link rel="canonical" href="/p/<n>">`, as real sites do. It also has traps: five
links into `/admin/` (disallowed in robots.txt) and ten pages linked only from a page marked
`<meta name="robots" content="nofollow">`. Every response is delayed 10 ms.

Measured on 2026-11-12: ww-scraper 0.7.0, Crawlee 3.18.2 (`CheerioCrawler`), Node 22.23,
Python 3.12, macOS on Apple silicon. Raw output: [`results.json`](results.json). Time,
CPU and memory are the median of 3 runs; counts are the same in every run.

## Same budget, whole site

Both tools with 10 requests in flight, no delay between requests to the host, robots.txt on.

| | ww-scraper 0.7.0 | Crawlee 3.18.2 |
|---|---|---|
| Pages found (of 500) | 500 | 500 |
| Page downloads | **510** | 2,500 |
| ... of which the same page again | **10** | 2,000 |
| Duplicate items handed to your code | **0** | 2,000 |
| Requests to the server in total | **1,013** | 3,013 |
| Pages linked only from a `nofollow` page fetched | **0** | 10 |
| Pages disallowed by robots.txt fetched | 0 | 0 |
| Wall time | **1.63 s** | 8.31 s |
| CPU time | **0.88 s** | 7.77 s |
| Peak memory | **35 MB** | 389 MB |

Why the downloads differ:

- Both drop the fragment. Both sort query parameters and drop `utm_*` (ww-scraper since 0.7.0;
  it also drops `gclid`, `fbclid`, `msclkid` and 20 other click ids, which Crawlee keeps).
- Crawlee also drops the trailing slash everywhere. ww-scraper keeps it, because `/a` and
  `/a/` can be different pages, and instead learns per site from `rel=canonical` that they
  are the same (after 3 agreeing pages, never after a disagreeing one). The same learning
  covers `?view=print` and `?a=1&b=2`. Crawlee does not read `rel=canonical`, so each of those
  variants is a separate download and a separate item.
- A redirect to a page already crawled stops after the first hop in ww-scraper. Crawlee
  follows it and downloads the page again.
- The 10 duplicate downloads in ww-scraper are the learning phase: variants fetched before 3
  pages had confirmed a rule, or already in flight. Each was recognised by its canonical and
  not passed to your code.

## Defaults, first 60 pages

Each tool as shipped (ww-scraper with only the loopback test switch on).

| | ww-scraper 0.7.0 | Crawlee 3.18.2 |
|---|---|---|
| Requests per second to the one host | **1.0** | 325 |
| Requests in flight at once | **1** | 10 |
| Pages disallowed by robots.txt fetched | **0** | 5 |

ww-scraper's default is one request per second per host, raised by robots.txt Crawl-delay
and by 429 or 503 answers. Crawlee's default has no per-host delay and scales concurrency up
on its own; on a real site with a slower server it goes well past 10 in flight. That
makes Crawlee much faster on one host when nobody sets a limit, and ww-scraper the one that
does not need a limit to be safe. With the same budget (table above) ww-scraper was the
faster of the two here.

## What it does not say

- One site, generated, on loopback. Real sites have slower servers, bigger pages and
  JavaScript; network time would shrink the CPU difference in wall time, not the count of
  downloads.
- Crawlee can be configured to dedupe more (a custom `uniqueKey`, `transformRequestFunction`).
  The benchmark measures what happens when nobody does.
- Crawlee and the hosted platforms built on it do much that ww-scraper does not: many machines, browser pools, proxy
  rotation, thousands of ready-made actors.

## Run it

```bash
mkdir /tmp/crawlee && (cd /tmp/crawlee && npm init -y >/dev/null && npm install crawlee@3.18.2)
python bench/crawl/run.py --crawlee-dir /tmp/crawlee --repeat 3
```

Needs `/usr/bin/time` (macOS or GNU). The defaults run of ww-scraper takes about a minute,
because it paces at one request per second.
