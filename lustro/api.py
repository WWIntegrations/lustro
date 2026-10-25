"""Local HTTP API with Apify-shaped routes (stdlib http.server).

Binds to a loopback IP only. A Bearer token is ALWAYS required unless auth is disabled
explicitly (--no-auth): it comes from --token / LUSTRO_API_TOKEN, or a random one is
generated at start and printed once. Requests whose Host header is not a loopback
address are refused (DNS-rebinding guard), browser requests carrying a non-loopback
Origin are refused (CSRF guard), and POST bodies must be application/json.

    GET  /v2/acts
    POST /v2/acts/<name>/runs[?waitForFinish=<s>&webhooks=<base64 JSON>]   body = input
    GET  /v2/actor-runs
    GET  /v2/actor-runs/<id>
    POST /v2/actor-runs/<id>/abort
    GET  /v2/datasets/<id>/items?format=json|jsonl|csv[&offset=&limit=]
    GET  /v2/key-value-stores/<id>/records/<key>
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
from urllib.parse import parse_qs, unquote, urlparse

from .actor import InputError, execute_run, prepare_run
from .actors import ACTORS, get_actor
from .compliance import is_loopback_host
from .storage import Storage, StorageError, valid_id, valid_key

MAX_BODY = 1024 * 1024
MAX_WAIT = 300


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class LustroAPI:
    def __init__(self, storage: Storage = None, host: str = "127.0.0.1", port: int = 8765,
                 token: str = None, allow_loopback: bool = False, lang: str = None,
                 no_auth: bool = False):
        if not is_loopback_host(host):
            raise ValueError("the Lustro API binds to a loopback IP only (127.0.0.1 or ::1)")
        self.storage = storage or Storage()
        self.token_generated = False
        if no_auth:
            self.token = ""
        else:
            self.token = token or os.environ.get("LUSTRO_API_TOKEN", "")
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

    def start_run(self, actor, input, run_id=None, webhooks=None):
        rec = prepare_run(actor, input, self.storage, run_id=run_id, lang=self.lang)
        abort = threading.Event()
        t = threading.Thread(target=execute_run, args=(rec, self.storage), daemon=True,
                             kwargs={"allow_loopback": self.allow_loopback,
                                     "abort_event": abort, "webhooks": webhooks})
        self.runs[rec["id"]] = (t, abort)
        t.start()
        return rec


class _Handler(BaseHTTPRequestHandler):
    ctx: LustroAPI = None
    server_version = "Lustro"
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

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if not is_loopback_host(host):
            self._error(403, "forbidden-host", "Host header must be a loopback address")
            return False
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
    def do_GET(self):
        if not self._guard():
            return
        path, q = self._route()
        st = self.ctx.storage
        if path == "/v2/acts":
            items = [a.describe() for a in ACTORS.values()]
            return self._json(200, {"data": {"total": len(items), "count": len(items),
                                             "offset": 0, "items": items}})
        if path == "/v2/actor-runs":
            runs = st.list_runs()
            return self._json(200, {"data": {"total": len(runs), "items": runs}})
        m = re.fullmatch(r"/v2/actor-runs/([^/]+)", path)
        if m:
            rec = st.load_run(m.group(1))
            return self._json(200, {"data": rec}) if rec else self._error(404, "not-found", "run not found")
        m = re.fullmatch(r"/v2/datasets/([^/]+)/items", path)
        if m:
            did = m.group(1)
            if not valid_id(did) or not (st.root / "datasets" / did).is_dir():
                return self._error(404, "not-found", "dataset not found")
            try:
                offset = int(q.get("offset", 0))
                limit = int(q["limit"]) if "limit" in q else None
                body, ctype = st.dataset(did).export(q.get("format", "json"), offset, limit)
            except (ValueError, StorageError) as e:
                return self._error(400, "invalid-request", str(e))
            return self._send(200, body, ctype)
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
            entry[1].set()
            entry[0].join(10)
            return self._json(200, {"data": self.ctx.storage.load_run(m.group(1))})
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
        webhooks = None
        if q.get("webhooks"):
            try:
                webhooks = json.loads(base64.b64decode(q["webhooks"]))
                if not isinstance(webhooks, list):
                    raise ValueError
            except (ValueError, TypeError):
                return self._error(400, "invalid-request", "webhooks must be base64 JSON list")
        try:
            rec = self.ctx.start_run(actor.name, data, webhooks=webhooks)
        except InputError as e:
            return self._json(400, {"error": {"type": "invalid-input", "message": str(e),
                                              "errors": e.errors}})
        try:
            wait = min(float(q.get("waitForFinish", 0)), MAX_WAIT)
        except ValueError:
            wait = 0
        if wait > 0:
            self.ctx.runs[rec["id"]][0].join(wait)
        return self._json(201, {"data": self.ctx.storage.load_run(rec["id"])})
