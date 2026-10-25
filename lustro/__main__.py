"""CLI: python -m lustro "query" [--sources a,b] [--limit N] [--no-verify] [--json]"""
from __future__ import annotations

import argparse
import json
import sys

from . import search


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lustro", description="Compliant media collector.")
    ap.add_argument("query")
    ap.add_argument("--sources", default="", help="comma-separated source names (default: all)")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--no-verify", action="store_true", help="skip live image verification")
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    a = ap.parse_args(argv)

    sources = [s for s in a.sources.split(",") if s] or None
    items = search(a.query, sources=sources, limit=a.limit, verify=not a.no_verify)

    if a.json:
        json.dump([r.to_dict() for r in items], sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        for r in items:
            print(f"{r.title[:58]:58s} {r.license:14s} {r.width}x{r.height}  {r.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
