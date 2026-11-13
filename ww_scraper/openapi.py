"""OpenAPI 3.1 description of the local API (served at /v2/openapi.json, written to
docs/openapi.json by `python -m ww_scraper.openapi`). Callers in any language can generate
a client from it; ww_scraper.client is the Python one."""
from __future__ import annotations

import json
import sys

from ._version import __version__


def _p(name, where="query", typ="string", desc=""):
    return {"name": name, "in": where, "required": where == "path", "schema": {"type": typ},
            "description": desc}


RUN_OPTS = [_p("timeout", typ="number", desc="seconds; the run ends TIMED-OUT after it"),
            _p("dataset", desc="also append the items to this named dataset"),
            _p("maxItems", typ="integer"), _p("maxRequests", typ="integer"), _p("maxBytes", typ="integer"),
            _p("waitForFinish", typ="number", desc="wait up to this many seconds (max 300)"),
            _p("webhooks", desc="base64 JSON list of {requestUrl, eventTypes}")]
VIEW = [_p("format", desc="json, jsonl, csv, xlsx, xml, html, rss"), _p("offset", typ="integer"),
        _p("limit", typ="integer"), _p("fields", desc="comma separated, dotted paths allowed"),
        _p("omit"), _p("flatten", typ="boolean"), _p("unwind"), _p("desc", typ="boolean"),
        _p("clean", typ="boolean"), _p("view", desc="a view from the dataset schema")]
ID, NAME, KEY = _p("id", "path"), _p("name", "path"), _p("key", "path")
JSON_BODY = {"content": {"application/json": {"schema": {"type": "object"}}}}


def _op(summary, params=(), body=None, ok="200"):
    op = {"summary": summary, "parameters": list(params),
          "responses": {ok: {"description": "OK"}, "401": {"description": "missing or invalid token"},
                        "404": {"description": "not found"}}}
    if body:
        op["requestBody"] = body
    return op


def spec() -> dict:
    paths = {
        "/v2/acts": {"get": _op("List actors with their schemas")},
        "/v2/acts/{name}": {"get": _op("One actor", [NAME])},
        "/v2/acts/{name}/runs": {"post": _op("Start a run; body = input", [NAME, *RUN_OPTS], JSON_BODY, "201")},
        "/v2/acts/{name}/run-sync": {"post": _op("Run, wait and return the OUTPUT record", [NAME, *RUN_OPTS], JSON_BODY)},
        "/v2/acts/{name}/run-sync-get-dataset-items": {
            "post": _op("Run, wait and return the items", [NAME, *RUN_OPTS, *VIEW], JSON_BODY)},
        "/v2/actor-runs": {"get": _op("List runs")},
        "/v2/actor-runs/{id}": {"get": _op("A run record", [ID]),
                                "delete": _op("Delete a finished run and its unnamed storages", [ID], ok="204")},
        "/v2/actor-runs/{id}/abort": {"post": _op("Ask a run to stop", [ID], JSON_BODY)},
        "/v2/actor-runs/{id}/resurrect": {"post": _op("Run a finished run again", [ID, *RUN_OPTS], JSON_BODY, "201")},
        "/v2/actor-runs/{id}/log": {"get": _op("The last 256 KB of the run log", [ID])},
        "/v2/actor-tasks": {"get": _op("List tasks"),
                            "post": _op("Create or replace a task: {name, actId, input, options}", body=JSON_BODY, ok="201")},
        "/v2/actor-tasks/{name}": {"get": _op("One task", [NAME]), "delete": _op("Delete a task", [NAME], ok="204")},
        "/v2/actor-tasks/{name}/runs": {"post": _op("Run a task; body = input override", [NAME, *RUN_OPTS], JSON_BODY, "201")},
        "/v2/stats/runs": {"get": _op("Run counts per day and status, metrics of recent runs",
                                      [_p("days", typ="integer"), _p("last", typ="integer")])},
        "/v2/openapi.json": {"get": _op("This description")},
    }
    for kind, what in (("datasets", "dataset"), ("key-value-stores", "key-value store"),
                       ("request-queues", "request queue")):
        paths[f"/v2/{kind}"] = {"get": _op(f"List {what}s", [_p("named", typ="boolean")])}
        paths[f"/v2/{kind}/{{id}}"] = {"get": _op(f"One {what}", [ID]),
                                       "delete": _op(f"Delete a {what}", [ID], ok="204")}
    paths["/v2/datasets/{id}/items"] = {"get": _op("Items in any export format", [ID, *VIEW])}
    paths["/v2/key-value-stores/{id}/keys"] = {"get": _op("Record keys", [ID, _p("collection")])}
    paths["/v2/key-value-stores/{id}/records/{key}"] = {
        "get": _op("One record with its content type", [ID, KEY]),
        "put": _op("Write one record (any content type)", [ID, KEY], {"content": {"*/*": {}}}, "201"),
        "delete": _op("Delete one record", [ID, KEY], ok="204")}
    paths["/v2/request-queues/{id}/requests"] = {"get": _op("Requests in the order added",
                                                            [ID, _p("offset", typ="integer"), _p("limit", typ="integer")])}
    paths["/v2/request-queues/{id}/head"] = {"get": _op("The next pending requests", [ID, _p("limit", typ="integer")])}
    return {
        "openapi": "3.1.0",
        "info": {"title": "ww-scraper local API", "version": __version__,
                 "description": "Binds to 127.0.0.1 only. Every /v2 route needs a Bearer token; requests with a "
                                "non-loopback Host or Origin header are refused."},
        "servers": [{"url": "http://127.0.0.1:8765"}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
        "paths": dict(sorted(paths.items())),
    }


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "docs/openapi.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(spec(), fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(out)
