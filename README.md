# Lustro

Compliant, open-source media-collector library. Lustro mirrors the useful parts of
commercial scraping platforms - search public image/media
sources, verify every result live, and carry provenance and license - while staying
strictly inside what each source permits.

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

## Sources (v1)

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
# minimal: pip install requests Pillow
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
headless-browser anti-bot evasion, or collecting from sources that forbid automated
access. These are out of scope **by design** - the point is a collector you can run in
the open, that respects the sites it touches.

## License

MIT. See [`LICENSE`](LICENSE).
