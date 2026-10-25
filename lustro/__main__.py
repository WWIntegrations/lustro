"""CLI.

    lustro run <actor> --input '<json>' [--storage DIR] [--resume RUN_ID] [--webhook URL]
    lustro actors                     list actors and their input fields
    lustro export <dataset-id> [--format json|jsonl|csv]
    lustro serve [--port 8765]        local API on 127.0.0.1
    lustro schedule <actor> --cron '0 6 * * 1' --input '<json>'   (prints only)
    lustro testsite [--port 8766]     local fixture website for demos/tests
    lustro "<query>" [...]            v0.1 media search (unchanged)

--allow-loopback is a TEST switch: it lets requests reach literal loopback IPs
(127.0.0.1, ::1), e.g. the bundled test site. It is never on by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import search

COMMANDS = {"run", "actors", "export", "serve", "schedule", "testsite"}


def _legacy(argv) -> int:
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


def _parser():
    ap = argparse.ArgumentParser(prog="lustro", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--storage", default=None, help="storage dir (default ./storage or $LUSTRO_STORAGE_DIR)")
        p.add_argument("--lang", default=None, choices=["en", "pl"], help="language of validation errors")
        p.add_argument("--allow-loopback", action="store_true",
                       help="TEST ONLY: allow requests to 127.0.0.1/::1 (e.g. `lustro testsite`)")

    r = sub.add_parser("run", help="run an actor")
    r.add_argument("actor")
    r.add_argument("--input", default=None, help="input as a JSON object")
    r.add_argument("--input-file", default=None, help="read input JSON from a file")
    r.add_argument("--resume", default=None, metavar="RUN_ID", help="resume an earlier run")
    r.add_argument("--webhook", action="append", default=[], help="webhook URL (allowlisted)")
    r.add_argument("--print-items", action="store_true", help="print dataset items as JSONL")
    r.add_argument("--quiet", action="store_true", help="do not stream the run log")
    common(r)

    sub.add_parser("actors", help="list actors")

    e = sub.add_parser("export", help="export a dataset")
    e.add_argument("dataset")
    e.add_argument("--format", default="json", choices=["json", "jsonl", "csv"])
    e.add_argument("--storage", default=None)

    s = sub.add_parser("serve", help="local API (127.0.0.1 only)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--token", default=None, help="Bearer token (default: $LUSTRO_API_TOKEN or a random one)")
    s.add_argument("--no-auth", action="store_true", help="disable the token (loopback only; not recommended)")
    common(s)

    sc = sub.add_parser("schedule", help="print a cron line and a launchd plist (installs nothing)")
    sc.add_argument("actor")
    sc.add_argument("--cron", required=True, help="5-field cron expression")
    sc.add_argument("--input", default="{}")
    sc.add_argument("--workdir", default=os.getcwd())

    t = sub.add_parser("testsite", help="serve the bundled fixture site on 127.0.0.1")
    t.add_argument("--port", type=int, default=8766)
    return ap


def _load_input(a):
    if a.input_file:
        with open(a.input_file, encoding="utf-8") as fh:
            return json.load(fh)
    if a.input is None:
        return None if a.resume else {}
    return json.loads(a.input)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        return _legacy(argv)
    a = _parser().parse_args(argv)

    from .actor import InputError, run_actor
    from .actors import ACTORS, get_actor
    from .storage import Storage

    if a.cmd == "actors":
        for act in ACTORS.values():
            props = act.input_schema.get("properties", {})
            req = set(act.input_schema.get("required", []))
            fields = ", ".join(f"{k}{'*' if k in req else ''}" for k in props)
            print(f"{act.name:26s} {act.description}\n{'':26s} input: {fields}")
        return 0

    if a.cmd == "export":
        body, _ = Storage(a.storage).dataset(a.dataset).export(a.format)
        sys.stdout.write(body.decode("utf-8"))
        return 0

    if a.cmd == "schedule":
        from .schedule import render
        if get_actor(a.actor) is None:
            print(f"unknown actor: {a.actor}", file=sys.stderr)
            return 2
        json.loads(a.input)
        print(render(a.actor, a.input, a.cron, os.path.abspath(a.workdir)))
        return 0

    if a.cmd == "testsite":
        from .testsite import TestSite
        site = TestSite(a.port)
        print(f"test site on {site.base} (Ctrl+C to stop)")
        try:
            site.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if a.cmd == "serve":
        from .api import LustroAPI
        try:
            api = LustroAPI(Storage(a.storage), a.host, a.port, token=a.token,
                            allow_loopback=a.allow_loopback, lang=a.lang, no_auth=a.no_auth)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if not api.token:
            print(f"Lustro API on {api.url} (WARNING: auth disabled with --no-auth)")
        elif api.token_generated:
            print(f"Lustro API on {api.url}\nBearer token (shown once): {api.token}", flush=True)
        else:
            print(f"Lustro API on {api.url} (Bearer token required)")
        try:
            api.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    # run
    if get_actor(a.actor) is None:
        print(f"unknown actor: {a.actor} (see `lustro actors`)", file=sys.stderr)
        return 2
    try:
        data = _load_input(a)
    except (ValueError, OSError) as e:
        print(f"invalid input JSON: {e}", file=sys.stderr)
        return 2
    storage = Storage(a.storage)
    try:
        rec = run_actor(a.actor, data, storage, run_id=a.resume, lang=a.lang,
                        allow_loopback=a.allow_loopback,
                        log_stream=None if a.quiet else sys.stderr,
                        webhooks=a.webhook or None)
    except InputError as e:
        for err in e.errors:
            print(f"input: {err}", file=sys.stderr)
        return 2
    if a.print_items:
        for it in storage.dataset(rec["defaultDatasetId"]).iter_items():
            print(json.dumps(it, ensure_ascii=False))
    else:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        print(f"dataset: {storage.root / 'datasets' / rec['defaultDatasetId'] / 'items.jsonl'}",
              file=sys.stderr)
    return 0 if rec["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
