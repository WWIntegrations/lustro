# Changelog

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
