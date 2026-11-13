"""CLI.

    ww-scraper run <actor> --input '<json>' [--storage DIR] [--resume RUN_ID] [--webhook URL]
    ww-scraper actors                     list actors and their input fields
    ww-scraper export <dataset-id> [--format json|jsonl|csv|xlsx|xml|html] [--fields a,b] [--out FILE]
    ww-scraper serve [--port 8765]        local API on 127.0.0.1
    ww-scraper schedule <actor> --cron '0 6 * * 1' --input '<json>'   (prints only)
    ww-scraper testsite [--port 8766]     local fixture website for demos/tests
    ww-scraper "<query>" [...]            v0.1 media search (unchanged)

--allow-loopback is a TEST switch: it lets requests reach literal loopback IPs
(127.0.0.1, ::1), e.g. the bundled test site. It is never on by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import search

COMMANDS = {"run", "actors", "export", "serve", "schedule", "testsite", "init", "runs", "report"}


def _legacy(argv) -> int:
    ap = argparse.ArgumentParser(prog="ww-scraper", description="Compliant media collector.")
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
    ap = argparse.ArgumentParser(prog="ww-scraper", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--storage", default=None, help="storage dir (default ./storage or $WW_SCRAPER_STORAGE_DIR)")
        p.add_argument("--lang", default=None, choices=["en", "pl"], help="language of validation errors")
        p.add_argument("--allow-loopback", action="store_true",
                       help="TEST ONLY: allow requests to 127.0.0.1/::1 (e.g. `ww-scraper testsite`)")

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

    i = sub.add_parser("init", help="write a starting point for your own actor (one .py file)")
    i.add_argument("name", help="actor name, e.g. price-watch")
    i.add_argument("--dir", default=".")

    rs = sub.add_parser("runs", help="list runs in the storage directory")
    rs.add_argument("--storage", default=None)
    rs.add_argument("--limit", type=int, default=20)

    rp = sub.add_parser("report", help="write a self-contained HTML report of a run")
    rp.add_argument("run_id")
    rp.add_argument("--out", default=None, help="output file (default: <storage>/reports/<run-id>.html)")
    rp.add_argument("--storage", default=None)

    e = sub.add_parser("export", help="export a dataset")
    e.add_argument("dataset")
    e.add_argument("--format", default="json", choices=["json", "jsonl", "csv", "xlsx", "xml", "html"])
    e.add_argument("--fields", default=None, help="comma-separated keys to keep, in this order")
    e.add_argument("--out", default=None, help="write to this file (xlsx needs it unless stdout is redirected)")
    e.add_argument("--storage", default=None)

    s = sub.add_parser("serve", help="local API (127.0.0.1 only)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--token", default=None, help="Bearer token (default: $WW_SCRAPER_API_TOKEN or a random one)")
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

    if a.cmd == "init":
        from .plugins import PluginError, scaffold
        try:
            path = scaffold(a.name, a.dir)
        except (PluginError, OSError) as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"wrote {path}\nrun it:  ww-scraper run {path} --input '{{\"startUrls\": [\"https://example.com/\"]}}'")
        return 0

    if a.cmd == "runs":
        runs = Storage(a.storage).list_runs()[::-1][: max(1, a.limit)]
        for r in runs:
            print(f"{r.get('id', ''):26s} {r.get('actId', ''):24s} {r.get('status', ''):10s} "
                  f"{r.get('itemCount', 0):6d} items  {r.get('startedAt') or ''}")
        if not runs:
            print("no runs yet")
        return 0

    if a.cmd == "report":
        from .report import build_report
        from .storage import valid_id
        storage = Storage(a.storage)
        if not valid_id(a.run_id) or storage.load_run(a.run_id) is None:
            print(f"unknown run: {a.run_id} (see `ww-scraper runs`)", file=sys.stderr)
            return 2
        out = a.out or os.path.join(str(storage.root), "reports", f"{a.run_id}.html")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(build_report(storage, a.run_id))
        print(out)
        return 0

    if a.cmd == "export":
        fields = [f.strip() for f in a.fields.split(",")] if a.fields else None
        body, _ = Storage(a.storage).dataset(a.dataset).export(a.format, fields=fields)
        if a.out:
            with open(a.out, "wb") as fh:
                fh.write(body)
            print(a.out)
        elif a.format == "xlsx" and sys.stdout.isatty():
            print("xlsx is binary: pass --out FILE or redirect stdout", file=sys.stderr)
            return 2
        else:
            sys.stdout.buffer.write(body)
            sys.stdout.flush()
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
        from .api import ScraperAPI
        try:
            api = ScraperAPI(Storage(a.storage), a.host, a.port, token=a.token,
                            allow_loopback=a.allow_loopback, lang=a.lang, no_auth=a.no_auth)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if not api.token:
            print(f"ww-scraper API on {api.url} (WARNING: auth disabled with --no-auth)\n"
                  f"Console: {api.url}/console")
        elif api.token_generated:
            # the token rides in the URL fragment, which browsers never send to a server
            print(f"ww-scraper API on {api.url}\nBearer token (shown once): {api.token}\n"
                  f"Console: {api.url}/console#token={api.token}", flush=True)
        else:
            print(f"ww-scraper API on {api.url} (Bearer token required)\n"
                  f"Console: {api.url}/console (paste the token when asked)")
        try:
            api.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    # run
    from .plugins import PluginError, is_actor_path, load_actor_file, register
    if is_actor_path(a.actor):
        try:
            a.actor = register(load_actor_file(a.actor)).name
        except PluginError as e:
            print(str(e), file=sys.stderr)
            return 2
    if get_actor(a.actor) is None:
        print(f"unknown actor: {a.actor} (see `ww-scraper actors`)", file=sys.stderr)
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
        path = storage.root / "datasets" / rec["defaultDatasetId"] / "items.jsonl"
        # a failed run may have written nothing: do not point at a file that is not there
        print(f"dataset: {path}" if path.exists() else "dataset: (no items written)", file=sys.stderr)
    return 0 if rec["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
