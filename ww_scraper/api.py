"""Local HTTP API for runs and storages (stdlib http.server).

Binds to a loopback IP only. A Bearer token is ALWAYS required unless auth is disabled
explicitly (--no-auth): it comes from --token / WW_SCRAPER_API_TOKEN, or a random one is
generated at start and printed once. Requests whose Host header is not a loopback
address are refused (DNS-rebinding guard), browser requests carrying a non-loopback
Origin are refused (CSRF guard), and POST bodies must be application/json.

    GET  /v2/acts
    POST /v2/acts/<name>/runs[?waitForFinish=<s>&timeout=<s>&dataset=<name>&maxItems=<n>&webhooks=<base64 JSON>]   body = input
    GET  /v2/actor-runs
    GET  /v2/actor-runs/<id>
    POST /v2/actor-runs/<id>/abort
    POST /v2/actor-runs/<id>/resurrect[?timeout=<s>&waitForFinish=<s>]   run a finished run again
                                                        with its storages and stored input
    GET  /v2/datasets/<id>/items?format=json|jsonl|csv|xlsx|xml|html|rss[&offset=&limit=&fields=a,b
         &omit=a,b.c&flatten=1&unwind=field&desc=1&clean=1&view=name]
    GET  /v2/key-value-stores/<id>/records/<key>
    GET  /v2/actor-runs/<id>/log                        last 256 KB of the run log
    GET  /v2/acts/<name>                                one actor with its schemas and version
    POST /v2/acts/<name>/run-sync-get-dataset-items[?timeout=&format=&fields=&view=...]
                                                        run, wait (up to 300 s), return the items
    POST /v2/acts/<name>/run-sync[?timeout=]           run, wait, return the OUTPUT record
    DELETE /v2/actor-runs/<id>                          a finished run with its unnamed storages
    GET  /v2/datasets[?named=1]   GET|DELETE /v2/datasets/<id>
    GET  /v2/key-value-stores[?named=1]   GET|DELETE /v2/key-value-stores/<id>
    GET  /v2/key-value-stores/<id>/keys[?collection=]
    PUT|DELETE /v2/key-value-stores/<id>/records/<key>  write or remove one record
    GET  /v2/request-queues[?named=1]   GET|DELETE /v2/request-queues/<id>
    GET  /v2/request-queues/<id>/requests[?offset=&limit=]   GET .../head[?limit=]
    GET  /v2/actor-tasks   POST /v2/actor-tasks (body {name, actId, input, options})
    GET|DELETE /v2/actor-tasks/<name>   POST /v2/actor-tasks/<name>/runs (body = input override)
    GET  /v2/stats/runs[?days=30&last=200]              runs per day and status, recent run metrics
    GET  /v2/openapi.json                               this API as OpenAPI 3.1
    GET  /console                                       web console (static files only)

The console page and its script and stylesheet carry no data, so they are served without
the token (the Host check still applies); everything they show comes from the /v2 routes
above, which need the token as before. They are sent with a strict Content-Security-Policy
(scripts and connections to this origin only, no framing).
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .actor import TERMINAL, InputError, execute_run, prepare_run, request_abort
from .actors import ACTORS, get_actor
from .compliance import is_loopback_host
from .storage import Storage, StorageError, valid_id, valid_key

MAX_BODY = 1024 * 1024
MAX_WAIT = 300
MAX_LOG = 256 * 1024

CONSOLE_DIR = Path(__file__).with_name("console")
# exact names only: nothing under /console is resolved against the filesystem by the request
CONSOLE_FILES = {"/console": ("index.html", "text/html; charset=utf-8"),
                 "/console/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/console/app.css": ("app.css", "text/css; charset=utf-8")}
CONSOLE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
               "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
LIVE_STATUSES = ("READY", "RUNNING", "ABORTING", "TIMING-OUT")


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class ScraperAPI:
    def __init__(self, storage: Storage = None, host: str = "127.0.0.1", port: int = 8765,
                 token: str = None, allow_loopback: bool = False, lang: str = None,
                 no_auth: bool = False):
        if not is_loopback_host(host):
            raise ValueError("the ww-scraper API binds to a loopback IP only (127.0.0.1 or ::1)")
        self.storage = storage or Storage()
        self.token_generated = False
        if no_auth:
            self.token = ""
        else:
            self.token = token or os.environ.get("WW_SCRAPER_API_TOKEN", "")
            if not self.token:
                self.token = secrets.token_urlsafe(32)
                self.token_generated = True
        self.allow_loopback = allow_loopback
        self.lang = lang
        self.runs: dict = {}
        api = self

        class Handler(_Handler):
            ctx = api

        self.httpd = _Server((host, port), Handler)
        self.host, self.port = self.httpd.server_address[:2]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def serve_forever(self):
        self.httpd.serve_forever()

    def start(self):
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def start_run(self, actor, input, run_id=None, webhooks=None, options=None):
        rec = prepare_run(actor, input, self.storage, run_id=run_id, lang=self.lang,
                          options=options)
        return self.launch(rec, webhooks)

    def launch(self, rec, webhooks=None):
        abort = threading.Event()
        t = threading.Thread(target=execute_run, args=(rec, self.storage), daemon=True,
                             kwargs={"allow_loopback": self.allow_loopback,
                                     "abort_event": abort, "webhooks": webhooks})
        self.runs[rec["id"]] = (t, abort)
        t.start()
        return rec


class _Handler(BaseHTTPRequestHandler):
    ctx: ScraperAPI = None
    server_version = "ww-scraper"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet by default
        pass

    # -- helpers ----------------------------------------------------------------------
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))

    def _error(self, code, etype, msg):
        self._json(code, {"error": {"type": etype, "message": msg}})

    def _guard(self, auth: bool = True) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if not is_loopback_host(host):
            self._error(403, "forbidden-host", "Host header must be a loopback address")
            return False
        if not auth:
            return True
        origin = self.headers.get("Origin")
        if origin is not None:
            try:
                ohost = urlparse(origin).hostname or ""
            except ValueError:
                ohost = ""
            if not is_loopback_host(ohost):
                self._error(403, "forbidden-origin", "cross-origin browser requests are refused")
                return False
        token = self.ctx.token
        if token:
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.startswith("Bearer ") else ""
            if not hmac.compare_digest(given.encode(), token.encode()):
                self._error(401, "unauthorized", "missing or invalid Bearer token")
                return False
        return True

    def _route(self):
        u = urlparse(self.path)
        return u.path.rstrip("/") or "/", {k: v[-1] for k, v in parse_qs(u.query).items()}

    # -- GET --------------------------------------------------------------------------
    def _console(self, path) -> None:
        name, ctype = CONSOLE_FILES[path]
        body = (CONSOLE_DIR / name).read_bytes()
        self._send(200, body, ctype, {"Content-Security-Policy": CONSOLE_CSP,
                                      "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})

    def do_GET(self):
        path, q = self._route()
        if path in CONSOLE_FILES or path == "/":
            if not self._guard(auth=False):
                return
            if path == "/":
                return self._send(302, b"", "text/plain", {"Location": "/console"})
            return self._console(path)
        if not self._guard():
            return
        st = self.ctx.storage
        if path == "/v2/acts":
            items = [a.describe() for a in ACTORS.values()]
            return self._json(200, {"data": {"total": len(items), "count": len(items),
                                             "offset": 0, "items": items}})
        if path == "/v2/actor-runs":
            runs = st.list_runs()
            return self._json(200, {"data": {"total": len(runs), "items": runs}})
        if path == "/v2/openapi.json":
            from .openapi import spec
            return self._json(200, spec())
        handled = self._get_more(path, q)
        if handled is not False:
            return handled
        m = re.fullmatch(r"/v2/actor-runs/([^/]+)", path)
        if m:
            rec = st.load_run(m.group(1))
            if not rec:
                return self._error(404, "not-found", "run not found")
            if rec.get("status") in LIVE_STATUSES:
                # the record is written at start and at the end; count what is stored so far
                rec["itemCount"] = st.dataset(rec.get("defaultDatasetId") or rec["id"]).count()
            return self._json(200, {"data": rec})
        m = re.fullmatch(r"/v2/actor-runs/([^/]+)/log", path)
        if m:
            rid = m.group(1)
            if not valid_id(rid) or st.load_run(rid) is None:
                return self._error(404, "not-found", "run not found")
            p = st.log_path(rid)
            data = b""
            if p.is_file():
                with open(p, "rb") as fh:
                    size = fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, size - MAX_LOG))
                    data = fh.read()
                if size > MAX_LOG:
                    data = data[data.find(b"\n") + 1:]   # drop the cut first line
            return self._send(200, data, "text/plain; charset=utf-8", {"Content-Security-Policy": "sandbox"})
        m = re.fullmatch(r"/v2/datasets/([^/]+)/items", path)
        if m:
            did = m.group(1)
            if not valid_id(did) or not (st.root / "datasets" / did).is_dir():
                return self._error(404, "not-found", "dataset not found")
            try:
                offset = int(q.get("offset", 0))
                limit = int(q["limit"]) if "limit" in q else None
                split = lambda k: [f.strip() for f in q[k].split(",")] if q.get(k) else None
                flag = lambda k: q.get(k, "").lower() in ("1", "true", "yes")
                fmt = q.get("format", "json")
                body, ctype = st.dataset(did).export(fmt, offset, limit, split("fields"),
                                                     omit=split("omit"), flatten=flag("flatten"),
                                                     unwind=q.get("unwind") or None,
                                                     desc=flag("desc"), clean=flag("clean"),
                                                     view=q.get("view") or None)
            except (ValueError, StorageError) as e:
                return self._error(400, "invalid-request", str(e))
            extra = {}
            if fmt == "html":
                extra["Content-Security-Policy"] = "sandbox"  # scraped values, shown never run
            if fmt == "xlsx":
                extra["Content-Disposition"] = f'attachment; filename="{did}.xlsx"'
            return self._send(200, body, ctype, extra)
        m = re.fullmatch(r"/v2/key-value-stores/([^/]+)/records/([^/]+)", path)
        if m:
            sid, key = m.group(1), unquote(m.group(2))
            if not valid_id(sid) or not valid_key(key):
                return self._error(404, "not-found", "record not found")
            rec = st.kv(sid).get_record(key)
            if rec is None:
                return self._error(404, "not-found", "record not found")
            # stored records may come from third-party sites: never let a browser run them
            return self._send(200, rec[0], rec[1], {"Content-Security-Policy": "sandbox"})
        return self._error(404, "not-found", "unknown route")

    def _list(self, items):
        return self._json(200, {"data": {"total": len(items), "count": len(items), "offset": 0, "items": items}})

    def _named(self, q):
        v = q.get("named")
        return None if v is None else v.lower() in ("1", "true", "yes")

    def _get_more(self, path, q):
        """Storage, queue, task and actor routes; False when the path is not one of them."""
        st = self.ctx.storage
        m = re.fullmatch(r"/v2/acts/([^/]+)", path)
        if m:
            actor = get_actor(unquote(m.group(1)))
            if actor is None:
                return self._error(404, "not-found", "actor not found")
            return self._json(200, {"data": actor.describe()})
        m = re.fullmatch(r"/v2/(datasets|key-value-stores|request-queues)", path)
        if m:
            return self._list(st.list_storages(m.group(1), self._named(q)))
        m = re.fullmatch(r"/v2/(datasets|key-value-stores|request-queues)/([^/]+)", path)
        if m:
            info = st.storage_info(m.group(1), m.group(2)) if valid_id(m.group(2)) else None
            if info is None:
                return self._error(404, "not-found", "storage not found")
            return self._json(200, {"data": info})
        m = re.fullmatch(r"/v2/key-value-stores/([^/]+)/keys", path)
        if m:
            if not valid_id(m.group(1)) or st.storage_info("key-value-stores", m.group(1)) is None:
                return self._error(404, "not-found", "store not found")
            kv = st.kv(m.group(1))
            try:
                keys = kv.keys(collection=q.get("collection") or None)
            except StorageError as e:
                return self._error(400, "invalid-request", str(e))
            items = []
            for k in keys:
                rec = kv.get_record(k)
                items.append({"key": k, "size": len(rec[0]) if rec else 0, "contentType": rec[1] if rec else None})
            return self._list(items)
        m = re.fullmatch(r"/v2/request-queues/([^/]+)/(requests|head)", path)
        if m:
            if not valid_id(m.group(1)) or st.storage_info("request-queues", m.group(1)) is None:
                return self._error(404, "not-found", "queue not found")
            queue = st.queue(m.group(1))
            try:
                offset = max(0, int(q.get("offset", 0)))
                limit = max(1, min(1000, int(q.get("limit", 100 if m.group(2) == "requests" else 10))))
            except ValueError:
                return self._error(400, "invalid-request", "offset and limit must be whole numbers")
            items = queue.list_requests(offset, limit) if m.group(2) == "requests" else queue.head(limit)
            return self._list(items)
        if path == "/v2/stats/runs":
            try:
                days = max(1, min(366, int(q.get("days", 30))))
                last = max(1, min(1000, int(q.get("last", 200))))
            except ValueError:
                return self._error(400, "invalid-request", "days and last must be whole numbers")
            from .storage import run_stats
            return self._json(200, {"data": run_stats(st.list_runs(), days, last)})
        if path == "/v2/actor-tasks":
            from .tasks import list_tasks
            return self._list(list_tasks(st))
        m = re.fullmatch(r"/v2/actor-tasks/([^/]+)", path)
        if m:
            from .tasks import TaskError, load_task
            try:
                task = load_task(st, unquote(m.group(1)))
            except TaskError:
                task = None
            if task is None:
                return self._error(404, "not-found", "task not found")
            return self._json(200, {"data": task})
        return False

    # -- PUT / DELETE -------------------------------------------------------------------
    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, self._error(400, "invalid-request", "bad Content-Length")
        if length > MAX_BODY:
            return None, self._error(413, "payload-too-large", "body over 1 MB")
        return (self.rfile.read(length) if length else b""), None

    def do_PUT(self):
        if not self._guard():
            return
        path, _ = self._route()
        m = re.fullmatch(r"/v2/key-value-stores/([^/]+)/records/([^/]+)", path)
        if not m:
            return self._error(404, "not-found", "unknown route")
        sid, key = m.group(1), unquote(m.group(2))
        if not valid_id(sid) or not valid_key(key):
            return self._error(400, "invalid-request", "invalid store id or key")
        if self.ctx.storage.run_is_live(sid):
            return self._error(409, "run-not-finished", "the store belongs to a run that is still going")
        body, err = self._body()
        if err is not None:
            return err
        ctype = self.headers.get("Content-Type") or "application/octet-stream"
        if ctype.split(";")[0].strip().lower() == "application/json":
            try:
                json.loads(body or b"null")
            except ValueError:
                return self._error(400, "invalid-input", "body is not valid JSON")
        self.ctx.storage.kv(sid).set(key, body, ctype)
        return self._json(201, {"data": {"key": key, "size": len(body), "contentType": ctype}})

    def do_DELETE(self):
        if not self._guard():
            return
        path, _ = self._route()
        st = self.ctx.storage
        try:
            m = re.fullmatch(r"/v2/key-value-stores/([^/]+)/records/([^/]+)", path)
            if m:
                sid, key = m.group(1), unquote(m.group(2))
                if not valid_id(sid) or not valid_key(key) or st.kv(sid).get_record(key) is None:
                    return self._error(404, "not-found", "record not found")
                if st.run_is_live(sid):
                    raise StorageError("the store belongs to a run that is still going")
                st.kv(sid).delete(key)
                return self._send(204, b"")
            m = re.fullmatch(r"/v2/(datasets|key-value-stores|request-queues)/([^/]+)", path)
            if m:
                if not valid_id(m.group(2)) or not st.delete_storage(m.group(1), m.group(2)):
                    return self._error(404, "not-found", "storage not found")
                return self._send(204, b"")
            m = re.fullmatch(r"/v2/actor-runs/([^/]+)", path)
            if m:
                if not valid_id(m.group(1)) or not st.delete_run(m.group(1)):
                    return self._error(404, "not-found", "run not found")
                return self._send(204, b"")
            m = re.fullmatch(r"/v2/actor-tasks/([^/]+)", path)
            if m:
                from .tasks import TaskError, delete_task
                try:
                    gone = delete_task(st, unquote(m.group(1)))
                except TaskError:
                    gone = False
                if not gone:
                    return self._error(404, "not-found", "task not found")
                return self._send(204, b"")
        except StorageError as e:
            return self._error(409, "conflict", str(e))
        return self._error(404, "not-found", "unknown route")

    # -- POST -------------------------------------------------------------------------
    def do_POST(self):
        if not self._guard():
            return
        path, q = self._route()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._error(415, "unsupported-media-type", "POST body must be application/json")
        m = re.fullmatch(r"/v2/actor-runs/([^/]+)/abort", path)
        if m:
            entry = self.ctx.runs.get(m.group(1))
            if not entry:
                return self._error(404, "not-found", "no active run with this id")
            if not request_abort(m.group(1)):    # not started yet: the event stops it at once
                entry[1].set()
            entry[0].join(10)
            return self._json(200, {"data": self.ctx.storage.load_run(m.group(1))})
        m = re.fullmatch(r"/v2/actor-runs/([^/]+)/resurrect", path)
        if m:
            return self._resurrect(m.group(1), q)
        if path == "/v2/actor-tasks" or re.fullmatch(r"/v2/actor-tasks/([^/]+)/runs", path):
            return self._task_post(path, q)
        m = re.fullmatch(r"/v2/acts/([^/]+)/(run-sync|run-sync-get-dataset-items)", path)
        if m:
            return self._run_sync(unquote(m.group(1)), m.group(2), q)
        m = re.fullmatch(r"/v2/acts/([^/]+)/runs", path)
        if not m:
            return self._error(404, "not-found", "unknown route")
        actor = get_actor(unquote(m.group(1)))
        if actor is None:
            return self._error(404, "not-found", f"actor {m.group(1)!r} not found")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(400, "invalid-request", "bad Content-Length")
        if length > MAX_BODY:
            return self._error(413, "payload-too-large", "input over 1 MB")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return self._error(400, "invalid-input", "body is not valid JSON")
        try:
            webhooks = self._webhooks(q)
        except (ValueError, TypeError):
            return self._error(400, "invalid-request", "webhooks must be base64 JSON list")
        try:
            options = self._options(q)
        except ValueError as e:
            return self._error(400, "invalid-request", str(e))
        try:
            rec = self.ctx.start_run(actor.name, data, webhooks=webhooks, options=options)
        except InputError as e:
            return self._json(400, {"error": {"type": "invalid-input", "message": str(e),
                                              "errors": e.errors}})
        return self._started(rec, q)

    def _read_json(self):
        body, err = self._body()
        if err is not None:
            return None, err
        try:
            return (json.loads(body) if body.strip() else {}), None
        except ValueError:
            return None, self._error(400, "invalid-input", "body is not valid JSON")

    def _options(self, q):
        """Run options from query parameters; raises ValueError with a message."""
        options = {}
        if q.get("timeout"):
            try:
                options["timeoutSecs"] = float(q["timeout"])
            except ValueError:
                raise ValueError("timeout must be a number of seconds") from None
        if q.get("dataset"):
            options["datasetName"] = q["dataset"]
        for key in ("maxItems", "maxRequests", "maxBytes"):
            if q.get(key):
                try:
                    options[key] = int(q[key])
                except ValueError:
                    raise ValueError(f"{key} must be a whole number") from None
        return options

    def _task_post(self, path, q):
        from .tasks import TaskError, save_task, task_run_args
        st = self.ctx.storage
        data, err = self._read_json()
        if err is not None:
            return err
        try:
            if path == "/v2/actor-tasks":
                if not isinstance(data, dict):
                    return self._error(400, "invalid-input", "body must be a JSON object")
                task = save_task(st, str(data.get("name") or ""), str(data.get("actId") or ""),
                                 data.get("input") or {}, data.get("options") or {}, lang=self.ctx.lang)
                return self._json(201, {"data": task})
            name = unquote(re.fullmatch(r"/v2/actor-tasks/([^/]+)/runs", path).group(1))
            actor, merged, opts = task_run_args(st, name, data or None, self._options(q))
            hooks = self._webhooks(q)
            rec = prepare_run(actor, merged, st, lang=self.ctx.lang, options=opts, task_name=name)
        except TaskError as e:
            return self._error(404 if "unknown task" in str(e) else 400, "invalid-request", str(e))
        except InputError as e:
            return self._json(400, {"error": {"type": "invalid-input", "message": str(e), "errors": e.errors}})
        except (ValueError, TypeError) as e:
            return self._error(400, "invalid-request", str(e))
        self.ctx.launch(rec, hooks)
        return self._started(rec, q)

    def _run_sync(self, name, kind, q):
        actor = get_actor(name)
        if actor is None:
            return self._error(404, "not-found", f"actor {name!r} not found")
        data, err = self._read_json()
        if err is not None:
            return err
        try:
            options = self._options(q)
        except ValueError as e:
            return self._error(400, "invalid-request", str(e))
        # a synchronous call never waits longer than MAX_WAIT: the run is given that timeout
        options["timeoutSecs"] = min(options.get("timeoutSecs") or MAX_WAIT, MAX_WAIT)
        try:
            rec = self.ctx.start_run(actor.name, data, options=options)
        except InputError as e:
            return self._json(400, {"error": {"type": "invalid-input", "message": str(e), "errors": e.errors}})
        self.ctx.runs[rec["id"]][0].join(MAX_WAIT + 15)
        rec = self.ctx.storage.load_run(rec["id"])
        extra = {"X-WW-Scraper-Run-Id": rec["id"], "X-WW-Scraper-Run-Status": rec["status"]}
        if rec["status"] not in TERMINAL:
            return self._json(408, {"error": {"type": "run-timeout", "message": "the run did not finish in time",
                                              "run": rec}})
        if kind == "run-sync":
            got = self.ctx.storage.kv(rec["defaultKeyValueStoreId"]).get_record("OUTPUT")
            if got is None:
                return self._json(200 if rec["status"] == "SUCCEEDED" else 400, {"data": rec})
            return self._send(200 if rec["status"] == "SUCCEEDED" else 400, got[0], got[1],
                              dict(extra, **{"Content-Security-Policy": "sandbox"}))
        if rec["status"] not in ("SUCCEEDED", "TIMED-OUT"):
            return self._json(400, {"error": {"type": "run-failed", "message": rec.get("statusMessage") or "",
                                              "run": rec}})
        split = lambda k: [f.strip() for f in q[k].split(",")] if q.get(k) else None
        flag = lambda k: q.get(k, "").lower() in ("1", "true", "yes")
        fmt = q.get("format", "json")
        try:
            body, ctype = self.ctx.storage.dataset(rec["defaultDatasetId"]).export(
                fmt, int(q.get("offset", 0)), int(q["limit"]) if "limit" in q else None, split("fields"),
                omit=split("omit"), flatten=flag("flatten"), unwind=q.get("unwind") or None,
                desc=flag("desc"), clean=flag("clean"), view=q.get("view") or None)
        except (ValueError, StorageError) as e:
            return self._error(400, "invalid-request", str(e))
        if fmt == "html":
            extra["Content-Security-Policy"] = "sandbox"
        return self._send(200, body, ctype, extra)

    def _started(self, rec, q):
        try:
            wait = min(float(q.get("waitForFinish", 0)), MAX_WAIT)
        except ValueError:
            wait = 0
        if wait > 0:
            self.ctx.runs[rec["id"]][0].join(wait)
        return self._json(201, {"data": self.ctx.storage.load_run(rec["id"])})

    @staticmethod
    def _webhooks(q):
        if not q.get("webhooks"):
            return None
        hooks = json.loads(base64.b64decode(q["webhooks"]))
        if not isinstance(hooks, list):
            raise ValueError
        return hooks

    def _resurrect(self, run_id, q):
        st = self.ctx.storage
        prev = st.load_run(run_id) if valid_id(run_id) else None
        if prev is None:
            return self._error(404, "not-found", "run not found")
        if prev.get("status") not in TERMINAL:
            return self._error(409, "run-not-finished",
                               f"run is {prev.get('status')}; only a finished run can be resurrected")
        if get_actor(prev.get("actId")) is None:
            return self._error(404, "not-found", f"actor {prev.get('actId')!r} is not loaded")
        try:
            options = self._options(q)
        except ValueError as e:
            return self._error(400, "invalid-request", str(e))
        try:
            hooks = self._webhooks(q)
        except (ValueError, TypeError):
            return self._error(400, "invalid-request", "webhooks must be base64 JSON list")
        try:
            rec = self.ctx.start_run(prev["actId"], None, run_id=run_id, webhooks=hooks, options=options)
        except InputError as e:
            return self._json(400, {"error": {"type": "invalid-input", "message": str(e), "errors": e.errors}})
        return self._started(rec, q)
