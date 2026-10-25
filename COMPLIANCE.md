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
  Wikimedia Commons, Pexels). It never logs in, never touches login-gated content, and
  collects media + license metadata - not personal profiles.

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
- **Lustro:** uses official APIs. Any future page-based source **must** pass
  `compliance.robots_ok()` before fetching, and a source whose operator forbids automated
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

### 5. No human-impersonation / anti-bot evasion (hard line)
- **Risky pattern:** bots configured to look like real people - spoofed browser headers,
  simulated mouse movement, **rotating residential IPs / proxies** - to defeat anti-bot
  systems (Cloudflare, CAPTCHA).
- **Lustro:** does **none** of this, by design. No residential/rotating proxies, no
  identity rotation, no CAPTCHA solving, no headless-browser evasion. Lustro identifies
  itself honestly in its `User-Agent` (with a contact URL) and backs off when limited.
  This is the single line that separates a collector you can run in the open from one you
  cannot.

## Summary

Lustro trades reach for trust: it will not touch a source that forbids automated access,
will not impersonate a human, and will not overload a server - and in return it produces
data you can actually use and publish without a terms-of-service fight.
