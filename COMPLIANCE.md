# Compliance

Lustro is the *compliant* alternative to aggressive scraping stacks. This document maps
the legal and ethical risk criteria for data collection to exactly how Lustro handles each
one. The rule throughout: **when something is not clearly permitted, Lustro refuses it** -
it never disguises a request to get around a control.

## Criteria

### 1. Kind of data (personal data / GDPR-RODO)
- **Permitted:** public, non-personal data from official, unauthenticated APIs - media,
  licenses, public metadata.
- **Risky:** personal data (GDPR/RODO) without a legal basis, or confidential data behind
  a login.
- **Lustro:** queries only public, unauthenticated or official-key APIs (Openverse,
  Wikimedia Commons, Pexels) and public pages. It never logs in, never touches
  login-gated content, and collects media, page content and technical metadata - not
  personal profiles.
- **v0.2:** e-mail addresses and phone numbers are **redacted by default** in every
  dataset item (and in Markdown / generated text) before anything is stored
  (`redactPii: true`). Turning it off is an explicit per-run input and the operator's
  legal-basis decision. `mailto:` / `tel:` links are never followed. The
  security-headers actor keeps cookie names and flags only, never cookie values.

### 2. Copyright
- **Permitted:** data for analytics, comparison, or text/data mining (TDM); using works
  under their license, with attribution.
- **Risky:** copying unique content (whole articles, image databases) and republishing it
  as your own.
- **Lustro:** every result carries its `license`, `license_url`, and `author`, so
  downstream use can honor the terms. Lustro returns references and metadata; it does not
  strip licenses or present others' work as original.

### 3. Terms of Service / robots.txt
- **Permitted:** sources that allow automated access, or do not disallow it in
  `robots.txt`.
- **Risky:** ignoring a platform's ToS that forbids scraping (e.g. LinkedIn, Meta) -
  account/IP bans or civil suits.
- **Lustro:** uses official APIs, and every page fetch in v0.2 goes through
  `PoliteCrawler`, which checks robots.txt for the origin of **every redirect hop**
  before requesting it. There is no option to skip robots.txt. robots.txt that answers
  401/403 means "everything disallowed"; one that cannot be read (5xx, network error)
  means the host is skipped. Pages with `noindex` (meta robots or `X-Robots-Tag`) are
  not stored as content, and `nofollow` pages are not expanded. Any page-based source
  **must** pass these checks before fetching, and a source whose operator forbids automated
  access is simply **not added** (for example, a site returning `403 "automated access
  blocked"` is left out rather than worked around). Lustro does not collect from
  ToS-protected, login-gated platforms.

### 4. Server stability
- **Permitted:** collecting with intervals/rate limits that don't burden the target.
- **Risky:** aggressive collection that slows or hangs the site (can be treated as
  DoS/DDoS).
- **Lustro:** `make_session()` returns a `PoliteSession` that enforces a per-host minimum
  interval between requests. No source can hammer a host, and the limit is a parameter,
  not a promise.
- **v0.2:** crawls are sequential; the per-host interval has a floor of 0.5 s (only
  loopback test hosts under the explicit test switch may go lower) and is raised by
  robots.txt `Crawl-delay` (capped at 30 s). 429 and 5xx responses are retried with
  backoff that honours `Retry-After`; if a server asks to wait longer than the cap,
  Lustro stops instead of retrying early. Runs are bounded by `maxPages`, `maxDepth`
  and a byte limit per response.

### 5. No human-impersonation / anti-bot evasion (hard line)
- **Risky pattern:** bots configured to look like real people - spoofed browser headers,
  simulated mouse movement, **rotating residential IPs / proxies** - to defeat anti-bot
  systems (Cloudflare, CAPTCHA).
- **Lustro:** does **none** of this, by design. No residential/rotating proxies, no
  identity rotation, no CAPTCHA solving, no headless-browser evasion. Lustro identifies
  itself honestly in its `User-Agent` (with a contact URL) and backs off when limited.
  This is the single line that separates a collector you can run in the open from one you
  cannot.

### 6. Network safety (SSRF) and local services (v0.2)
- Every request and every redirect hop is checked: http(s) only, no credentials in
  URLs, and hosts resolving to private, loopback, link-local, reserved or multicast
  addresses are refused. The binding check happens at connect time (`PinnedAdapter`):
  the host is resolved once, ALL addresses are validated, the socket is opened to the
  validated IP with the original Host header and TLS SNI, and the peer address is
  re-checked, so a DNS answer that changes after the early check (rebinding) cannot
  reach an internal address. These sessions do not use environment proxies. Loopback IPs (127.0.0.1, ::1) are reachable only with the
  explicit test switch (`allow_loopback` / `--allow-loopback`), never by default, and
  the switch never opens other private ranges or names such as `localhost`.
- Sitemaps are read without an XML parser, in one linear pass over tag boundaries (no
  backtracking regex, so hostile input cannot cause super-linear work); documents with
  DOCTYPE/ENTITY declarations are refused (no XXE, no entity expansion); size is capped
  at 50 MB, also after gzip.
- The local API binds to a loopback IP only and always requires a Bearer token
  (`--token`, `LUSTRO_API_TOKEN`, or a random one printed once at start; only an
  explicit `--no-auth` disables it), compared in constant time. It rejects non-loopback
  `Host` headers (DNS rebinding) and non-loopback `Origin` headers (browser CSRF),
  accepts POST bodies only as `application/json`, and serves stored records with
  `Content-Security-Policy: sandbox` and `nosniff`.
- Webhooks go only to allowlisted hosts (`LUSTRO_WEBHOOK_HOSTS`), over HTTPS, without
  redirects, signed with HMAC-SHA256 (`LUSTRO_WEBHOOK_SECRET`).
- `lustro schedule` prints cron/launchd entries; it never installs persistence.

### 7. Out of scope by decision
No residential or rotating proxies, no identity rotation, no CAPTCHA solving, no
anti-bot evasion, no multi-tenant hosting, no scraping through MCP/agent tools.

## Summary

Lustro trades reach for trust: it will not touch a source that forbids automated access,
will not impersonate a human, and will not overload a server - and in return it produces
data you can actually use and publish without a terms-of-service fight.
