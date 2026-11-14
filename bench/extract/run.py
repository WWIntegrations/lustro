"""Extraction benchmark: product-catalog's parser against known answers.

Builds the corpus in corpus.py (12 encodings x 25 pages by default), runs the same parser the
product-catalog actor runs on each page (parse_html + products_on_page), and compares each
field with the answer the generator wrote. Prints a table per encoding and per field, and
with --json writes the raw counts.

    python bench/extract/run.py [--per-encoding 25] [--json bench/extract/results.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

from corpus import ENCODINGS, corpus  # noqa: E402
from ww_scraper import __version__  # noqa: E402
from ww_scraper.actors.product_catalog import products_on_page  # noqa: E402
from ww_scraper.extract import parse_html  # noqa: E402

FIELDS = ["name", "price", "lowPrice", "highPrice", "currency", "availability", "sku", "gtin",
          "brand", "image"]


def same(field, want, got):
    if field in ("price", "lowPrice", "highPrice"):
        return (want is None and got is None) or (
            want is not None and got is not None and abs(float(want) - float(got)) < 0.005)
    return want == got


def score(pages):
    by_enc = defaultdict(lambda: {"pages": 0, "pagesExact": 0, "expected": 0, "found": 0,
                                  "spurious": 0, "fields": 0, "fieldsRight": 0})
    by_field = {f: {"checked": 0, "right": 0} for f in FIELDS}
    misses = defaultdict(list)
    for pg in pages:
        e = by_enc[pg["encoding"]]
        got = products_on_page(parse_html(pg["html"], pg["url"]), pg["url"])
        by_sku = {g.get("sku"): g for g in got}
        e["pages"] += 1
        e["expected"] += len(pg["truth"])
        exact = len(got) == len(pg["truth"])
        matched = 0
        for i, want in enumerate(pg["truth"]):
            g = by_sku.get(want["sku"]) or (got[i] if len(got) == len(pg["truth"]) else None)
            if g is None:
                exact = False
                e["fields"] += len(FIELDS)
                for f in FIELDS:
                    by_field[f]["checked"] += 1
                if len(misses[pg["encoding"]]) < 3:
                    misses[pg["encoding"]].append({"url": pg["url"], "missing": True})
                continue
            matched += 1
            for f in FIELDS:
                ok = same(f, want[f], g.get(f))
                e["fields"] += 1
                e["fieldsRight"] += ok
                by_field[f]["checked"] += 1
                by_field[f]["right"] += ok
                if not ok:
                    exact = False
                    if len(misses[pg["encoding"]]) < 3:
                        misses[pg["encoding"]].append({"url": pg["url"], "field": f, "want": want[f],
                                                       "got": g.get(f)})
        e["found"] += matched
        e["spurious"] += max(0, len(got) - matched)
        e["pagesExact"] += exact
    return dict(by_enc), by_field, dict(misses)


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-encoding", type=int, default=25)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    pages = corpus(a.per_encoding, a.seed)
    by_enc, by_field, misses = score(pages)
    print(f"ww-scraper {__version__}, {len(pages)} pages\n")
    print("| Encoding | Products found | Fields right | Pages fully right |")
    print("|---|---|---|---|")
    tot = defaultdict(int)
    for enc in ENCODINGS:
        e = by_enc[enc]
        for k, v in e.items():
            tot[k] += v
        print(f"| {enc} | {e['found']}/{e['expected']} | {pct(e['fieldsRight'], e['fields'])} | "
              f"{e['pagesExact']}/{e['pages']} |")
    print(f"| **all** | {tot['found']}/{tot['expected']} | {pct(tot['fieldsRight'], tot['fields'])} | "
          f"{tot['pagesExact']}/{tot['pages']} |\n")
    print("| Field | Right |")
    print("|---|---|")
    for f, v in by_field.items():
        print(f"| {f} | {pct(v['right'], v['checked'])} |")
    if misses:
        print("\nFirst misses per encoding:")
        for enc, m in misses.items():
            for x in m:
                print(f"  {enc}: {x}")
    if a.json:
        Path(a.json).write_text(json.dumps({"ww_scraper": __version__, "pages": len(pages),
                                            "seed": a.seed, "byEncoding": by_enc, "byField": by_field,
                                            "totals": dict(tot)}, indent=2) + "\n")


if __name__ == "__main__":
    main()
