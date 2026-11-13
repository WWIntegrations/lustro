"""Resurrecting a finished run over the API, and the CREATED / RESURRECTED events."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from ww_scraper.actor import Actor, run_actor
from ww_scraper.actors import ACTORS
from ww_scraper.api import ScraperAPI


class Once(Actor):
    name = "test-once"

    def run(self, ctx):
        state = ctx.use_state({"runs": 0})
        state["runs"] += 1
        ctx.push_data({"run": state["runs"]})


@pytest.fixture(autouse=True)
def actor(monkeypatch):
    monkeypatch.setitem(ACTORS, Once.name, Once())


class _Hook(BaseHTTPRequestHandler):
    got = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()


def test_start_events_only_when_asked_for(storage, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/h"
    monkeypatch.setenv("WW_SCRAPER_WEBHOOK_HOSTS", "127.0.0.1")
    monkeypatch.setenv("WW_SCRAPER_WEBHOOK_SECRET", "s")
    try:
        _Hook.got = []
        all_events = [{"requestUrl": url, "eventTypes": ["ACTOR.RUN.CREATED", "ACTOR.RUN.RESURRECTED",
                                                         "ACTOR.RUN.SUCCEEDED"]}]
        rec = run_actor("test-once", {}, storage, allow_loopback=True, webhooks=all_events)
        run_actor("test-once", None, storage, run_id=rec["id"], allow_loopback=True, webhooks=all_events)
        assert [e["eventType"] for e in _Hook.got] == ["ACTOR.RUN.CREATED", "ACTOR.RUN.SUCCEEDED",
                                                       "ACTOR.RUN.RESURRECTED", "ACTOR.RUN.SUCCEEDED"]
        assert _Hook.got[0]["resource"]["status"] == "RUNNING"
        _Hook.got = []
        run_actor("test-once", {}, storage, allow_loopback=True, webhooks=[url])     # default types
        assert [e["eventType"] for e in _Hook.got] == ["ACTOR.RUN.SUCCEEDED"]
    finally:
        srv.shutdown()


def test_resurrect_route(storage):
    api = ScraperAPI(storage, port=0, token="t")
    api.start()
    try:
        h = {"Authorization": "Bearer t", "Content-Type": "application/json"}
        first = requests.post(api.url + "/v2/acts/test-once/runs?waitForFinish=10", headers=h, data="{}",
                              timeout=15).json()["data"]
        r = requests.post(f"{api.url}/v2/actor-runs/{first['id']}/resurrect?waitForFinish=10&timeout=60",
                          headers=h, data="{}", timeout=15)
        again = r.json()["data"]
        assert r.status_code == 201 and again["id"] == first["id"] and again["status"] == "SUCCEEDED"
        assert again["resumedFrom"] == "SUCCEEDED" and again["options"] == {"timeoutSecs": 60.0}
        assert [i["run"] for i in storage.dataset(first["id"]).items()] == [1, 2]   # same storages
        assert requests.post(api.url + "/v2/actor-runs/nope/resurrect", headers=h, data="{}",
                             timeout=5).status_code == 404
        live = dict(first, status="RUNNING")
        storage.save_run(live)
        assert requests.post(f"{api.url}/v2/actor-runs/{first['id']}/resurrect", headers=h, data="{}",
                             timeout=5).status_code == 409
    finally:
        api.shutdown()
