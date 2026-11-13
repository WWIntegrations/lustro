"""A small client for a running `ww-scraper serve`, sync (Client) and async (AsyncClient).

    from ww_scraper.client import Client
    c = Client("http://127.0.0.1:8765", token="...")
    rec = c.call("sitemap", {"startUrls": ["https://example.com/"]})      # start and wait
    items = c.items(rec["defaultDatasetId"])

Connection errors, 429 and 5xx answers are retried with exponential backoff (Retry-After is
honoured, up to max_backoff); other 4xx answers raise ApiError at once. The token is sent
only to the base URL given, which must be a loopback address (the API binds to nothing else).
"""
from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from urllib.parse import quote, urlsplit

import requests
from urllib3.util import parse_url

from .compliance import is_loopback_host

TERMINAL = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")


class ApiError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        err = body.get("error", {}) if isinstance(body, dict) else {}
        super().__init__(f"HTTP {status}: {err.get('type', '')} {err.get('message', body)}".strip())


# characters that make URL parsers disagree on the host ("http://evil.com\@127.0.0.1" is
# 127.0.0.1 to urlsplit but evil.com to urllib3, which is what requests sends to)
_BASE_URL_BAD = re.compile(r"[\\@\s\x00-\x1f\x7f]")


def check_base_url(base_url) -> str:
    """base_url when it is http(s) to a literal loopback IP by every parser involved, else
    ValueError. The token is only ever sent to this address."""
    msg = "the ww-scraper API listens on a loopback address only"
    if not isinstance(base_url, str) or _BASE_URL_BAD.search(base_url):
        raise ValueError(msg + " (base_url must not contain '\\', '@', whitespace or control characters)")
    try:
        parts, u3 = urlsplit(base_url), parse_url(base_url)
        port = parts.port
    except ValueError:
        raise ValueError(msg) from None
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or u3.scheme != parts.scheme:
        raise ValueError(msg)
    if not is_loopback_host(host) or not is_loopback_host(u3.host or ""):
        raise ValueError(msg)
    if (u3.host or "").strip("[]").lower() != host or u3.port != port:
        raise ValueError(msg)
    return base_url


class Client:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: str = "", *, retries: int = 4,
                 backoff: float = 0.5, max_backoff: float = 30.0, timeout: float = 30.0, session=None):
        self.base = check_base_url(base_url).rstrip("/")
        self.token = token
        self.retries, self.backoff, self.max_backoff, self.timeout = retries, backoff, max_backoff, timeout
        self.session = session or requests.Session()
        self.session.trust_env = False          # never send the token through a proxy

    # -- transport --------------------------------------------------------------------
    def request(self, method: str, path: str, *, params=None, json_body=None, data=None,
                headers=None, raw: bool = False, timeout: float = None):
        h = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        h.update(headers or {})
        if json_body is not None:
            data = json.dumps(json_body)
            h["Content-Type"] = "application/json"
        params = {k: (("1" if v else "0") if isinstance(v, bool) else v)
                  for k, v in (params or {}).items() if v is not None}
        for attempt in range(self.retries + 1):
            try:
                r = self.session.request(method, self.base + path, params=params, data=data, headers=h,
                                         timeout=timeout or self.timeout, allow_redirects=False)
            except requests.ConnectionError:
                if attempt >= self.retries:
                    raise
                time.sleep(self._delay(attempt, None))
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt < self.retries:
                time.sleep(self._delay(attempt, r.headers.get("Retry-After")))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                raise ApiError(r.status_code, body)
            if raw or r.status_code == 204:
                return r
            ctype = r.headers.get("Content-Type", "")
            return r.json() if ctype.startswith("application/json") else r.content
        raise RuntimeError("unreachable")  # pragma: no cover

    def _delay(self, attempt, retry_after):
        if retry_after and str(retry_after).strip().isdigit():
            return min(self.max_backoff, float(retry_after))
        return min(self.max_backoff, self.backoff * 2 ** attempt)

    @staticmethod
    def _data(res):
        return res["data"] if isinstance(res, dict) and "data" in res else res

    # -- actors and runs --------------------------------------------------------------
    def actors(self) -> list:
        return self._data(self.request("GET", "/v2/acts"))["items"]

    def actor(self, name: str) -> dict:
        return self._data(self.request("GET", f"/v2/acts/{quote(name, safe='')}"))

    def start(self, actor: str, input=None, *, wait: float = 0, **options) -> dict:
        """Start a run; options: timeout, dataset, maxItems, maxRequests, maxBytes."""
        params = dict(options, waitForFinish=wait or None)
        return self._data(self.request("POST", f"/v2/acts/{quote(actor, safe='')}/runs", params=params,
                                       json_body=input or {}))

    def run(self, run_id: str) -> dict:
        return self._data(self.request("GET", f"/v2/actor-runs/{quote(run_id, safe='')}"))

    def wait(self, run_id: str, timeout: float = None, poll: float = 1.0) -> dict:
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            rec = self.run(run_id)
            if rec.get("status") in TERMINAL or (end is not None and time.monotonic() >= end):
                return rec
            time.sleep(poll)

    def call(self, actor: str, input=None, *, timeout: float = None, **options) -> dict:
        """Start a run and wait for it to finish (or for `timeout` seconds of waiting)."""
        rec = self.start(actor, input, wait=min(timeout or 60, 60), **options)
        return rec if rec.get("status") in TERMINAL else self.wait(rec["id"], timeout)

    def call_items(self, actor: str, input=None, *, format: str = "json", **params):
        """One request: run, wait (up to 300 s on the server) and return the items."""
        params = dict(params, format=format)
        return self.request("POST", f"/v2/acts/{quote(actor, safe='')}/run-sync-get-dataset-items",
                            params=params, json_body=input or {}, timeout=330)

    def abort(self, run_id: str) -> dict:
        return self._data(self.request("POST", f"/v2/actor-runs/{quote(run_id, safe='')}/abort", json_body={}))

    def resurrect(self, run_id: str, *, wait: float = 0, **options) -> dict:
        return self._data(self.request("POST", f"/v2/actor-runs/{quote(run_id, safe='')}/resurrect",
                                       params=dict(options, waitForFinish=wait or None), json_body={}))

    def log(self, run_id: str) -> str:
        return self.request("GET", f"/v2/actor-runs/{quote(run_id, safe='')}/log").decode("utf-8", "replace")

    def runs(self) -> list:
        return self._data(self.request("GET", "/v2/actor-runs"))["items"]

    def delete_run(self, run_id: str) -> None:
        self.request("DELETE", f"/v2/actor-runs/{quote(run_id, safe='')}")

    # -- storages ---------------------------------------------------------------------
    def items(self, dataset_id: str, *, format: str = "json", **params):
        """Items of a dataset; params: offset, limit, fields, omit, flatten, unwind, desc,
        clean, view (lists are joined with commas)."""
        params = {k: ",".join(v) if isinstance(v, (list, tuple)) else v for k, v in params.items()}
        return self.request("GET", f"/v2/datasets/{quote(dataset_id, safe='')}/items",
                            params=dict(params, format=format))

    def storages(self, kind: str = "datasets", named: bool = None) -> list:
        return self._data(self.request("GET", f"/v2/{kind}", params={"named": named}))["items"]

    def storage(self, kind: str, storage_id: str) -> dict:
        return self._data(self.request("GET", f"/v2/{kind}/{quote(storage_id, safe='')}"))

    def delete_storage(self, kind: str, storage_id: str) -> None:
        self.request("DELETE", f"/v2/{kind}/{quote(storage_id, safe='')}")

    def keys(self, store_id: str, collection: str = None) -> list:
        return self._data(self.request("GET", f"/v2/key-value-stores/{quote(store_id, safe='')}/keys",
                                       params={"collection": collection}))["items"]

    def record(self, store_id: str, key: str):
        return self.request("GET", f"/v2/key-value-stores/{quote(store_id, safe='')}/records/{quote(key, safe='')}")

    def put_record(self, store_id: str, key: str, value, content_type: str = None) -> dict:
        if isinstance(value, (dict, list)):
            body, ctype = json.dumps(value).encode("utf-8"), content_type or "application/json"
        elif isinstance(value, str):
            body, ctype = value.encode("utf-8"), content_type or "text/plain; charset=utf-8"
        else:
            body, ctype = value, content_type or "application/octet-stream"
        return self._data(self.request("PUT", f"/v2/key-value-stores/{quote(store_id, safe='')}/records/"
                                              f"{quote(key, safe='')}", data=body, headers={"Content-Type": ctype}))

    def delete_record(self, store_id: str, key: str) -> None:
        self.request("DELETE", f"/v2/key-value-stores/{quote(store_id, safe='')}/records/{quote(key, safe='')}")

    def queue_head(self, queue_id: str, limit: int = 10) -> list:
        return self._data(self.request("GET", f"/v2/request-queues/{quote(queue_id, safe='')}/head",
                                       params={"limit": limit}))["items"]

    # -- tasks, stats -----------------------------------------------------------------
    def tasks(self) -> list:
        return self._data(self.request("GET", "/v2/actor-tasks"))["items"]

    def save_task(self, name: str, actor: str, input=None, options=None) -> dict:
        return self._data(self.request("POST", "/v2/actor-tasks", json_body={
            "name": name, "actId": actor, "input": input or {}, "options": options or {}}))

    def run_task(self, name: str, input_override=None, *, wait: float = 0, **options) -> dict:
        return self._data(self.request("POST", f"/v2/actor-tasks/{quote(name, safe='')}/runs",
                                       params=dict(options, waitForFinish=wait or None),
                                       json_body=input_override or {}))

    def delete_task(self, name: str) -> None:
        self.request("DELETE", f"/v2/actor-tasks/{quote(name, safe='')}")

    def stats(self, days: int = 30, last: int = 200) -> dict:
        return self._data(self.request("GET", "/v2/stats/runs", params={"days": days, "last": last}))


class AsyncClient:
    """The same methods as Client, as coroutines (each call runs in a worker thread)."""

    def __init__(self, *args, **kwargs):
        self._sync = Client(*args, **kwargs)

    def __getattr__(self, name):
        target = getattr(self._sync, name)
        if not callable(target):
            return target

        @functools.wraps(target)
        async def call(*args, **kwargs):
            return await asyncio.to_thread(target, *args, **kwargs)
        return call
