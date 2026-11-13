"""API: actor detail, sync runs, storage list/get/delete, record writes, queue reads,
tasks, run statistics and the OpenAPI description - offline."""
import json
import re
from pathlib import Path

import pytest
import requests

import ww_scraper.api as api_mod
from ww_scraper.actor import Actor, run_actor
from ww_scraper.actors import ACTORS
from ww_scraper.api import ScraperAPI
from ww_scraper.openapi import spec


class Items(Actor):
    name = "test-items"
    input_schema = {"type": "object", "properties": {"n": {"type": "integer", "default": 3},
                                                     "fail": {"type": "boolean", "default": False}}}

    def run(self, ctx):
        if ctx.input["fail"]:
            raise RuntimeError("asked to fail")
        ctx.push_data([{"i": i, "meta": {"even": i % 2 == 0}} for i in range(ctx.input["n"])])
        ctx.queue.add_many([f"https://example.org/{i}" for i in range(ctx.input["n"])])
        ctx.set_value("OUTPUT", {"pushed": ctx.input["n"]})


@pytest.fixture
def api(storage, monkeypatch):
    monkeypatch.setitem(ACTORS, Items.name, Items())
    a = ScraperAPI(storage, port=0, token="t")
    a.start()
    yield a
    a.shutdown()


H = {"Authorization": "Bearer t", "Content-Type": "application/json"}


def _get(api, path, **kw):
    return requests.get(api.url + path, headers=H, timeout=10, **kw)


def test_actor_detail_and_sync_runs(api):
    d = _get(api, "/v2/acts/test-items").json()["data"]
    assert d["name"] == "test-items" and d["version"] and "outputSchema" in d
    assert _get(api, "/v2/acts/nope").status_code == 404
    r = requests.post(api.url + "/v2/acts/test-items/run-sync-get-dataset-items?format=csv&flatten=1",
                      headers=H, data='{"n": 2}', timeout=30)
    assert r.status_code == 200 and r.text.splitlines() == ["i,meta.even", "0,true", "1,false"]
    assert r.headers["X-WW-Scraper-Run-Status"] == "SUCCEEDED"
    r = requests.post(api.url + "/v2/acts/test-items/run-sync", headers=H, data='{"n": 4}', timeout=30)
    assert r.status_code == 200 and r.json() == {"pushed": 4}
    r = requests.post(api.url + "/v2/acts/test-items/run-sync-get-dataset-items", headers=H,
                      data='{"fail": true}', timeout=30)
    assert r.status_code == 400 and r.json()["error"]["type"] == "run-failed"
    r = requests.post(api.url + "/v2/acts/test-items/run-sync", headers=H, data='{"n": "x"}', timeout=30)
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid-input"


def test_storage_listing_reading_and_deleting(api, storage):
    rec = run_actor("test-items", {"n": 3}, storage)
    storage.named_dataset("keep").push({"x": 1})
    ds = _get(api, "/v2/datasets").json()["data"]["items"]
    assert {d["id"] for d in ds} == {rec["id"], "keep"}
    assert [d["id"] for d in _get(api, "/v2/datasets?named=1").json()["data"]["items"]] == ["keep"]
    info = _get(api, f"/v2/datasets/{rec['id']}").json()["data"]
    assert info["itemCount"] == 3 and info["runId"] == rec["id"] and info["name"] is None
    keys = _get(api, f"/v2/key-value-stores/{rec['id']}/keys").json()["data"]["items"]
    assert [k["key"] for k in keys] == ["INPUT", "OUTPUT"]
    q = _get(api, f"/v2/request-queues/{rec['id']}").json()["data"]
    assert q["totalRequestCount"] == 3 and q["pendingRequestCount"] == 3
    head = _get(api, f"/v2/request-queues/{rec['id']}/head?limit=2").json()["data"]["items"]
    assert [h["url"] for h in head] == ["https://example.org/0", "https://example.org/1"]
    reqs = _get(api, f"/v2/request-queues/{rec['id']}/requests?offset=1&limit=5").json()["data"]["items"]
    assert len(reqs) == 2 and reqs[0]["handled"] is False
    # write and delete records in a named store
    r = requests.put(api.url + "/v2/key-value-stores/notes/records/today", data=b"hello",
                     headers={"Authorization": "Bearer t", "Content-Type": "text/plain"}, timeout=5)
    assert r.status_code == 201 and storage.named_kv("notes").get("today") == "hello"
    r = requests.put(api.url + "/v2/key-value-stores/notes/records/bad", data=b"{oops", headers=H, timeout=5)
    assert r.status_code == 400
    assert requests.delete(api.url + "/v2/key-value-stores/notes/records/today", headers=H, timeout=5).status_code == 204
    assert storage.named_kv("notes").get("today") is None
    assert requests.delete(api.url + "/v2/datasets/keep", headers=H, timeout=5).status_code == 204
    assert requests.delete(api.url + "/v2/datasets/keep", headers=H, timeout=5).status_code == 404
    assert requests.delete(api.url + f"/v2/actor-runs/{rec['id']}", headers=H, timeout=5).status_code == 204
    assert storage.load_run(rec["id"]) is None and not (storage.root / "datasets" / rec["id"]).exists()
    # a live run's storages cannot be deleted
    live = run_actor("test-items", {}, storage)
    storage.save_run(dict(live, status="RUNNING"))
    assert requests.delete(api.url + f"/v2/datasets/{live['id']}", headers=H, timeout=5).status_code == 409
    assert requests.delete(api.url + f"/v2/actor-runs/{live['id']}", headers=H, timeout=5).status_code == 409


def test_task_routes(api, storage):
    r = requests.post(api.url + "/v2/actor-tasks", headers=H, timeout=5,
                      data=json.dumps({"name": "three", "actId": "test-items", "input": {"n": 3}}))
    assert r.status_code == 201 and r.json()["data"]["name"] == "three"
    assert [t["name"] for t in _get(api, "/v2/actor-tasks").json()["data"]["items"]] == ["three"]
    r = requests.post(api.url + "/v2/actor-tasks/three/runs?waitForFinish=10", headers=H, data='{"n": 5}', timeout=15)
    rec = r.json()["data"]
    assert r.status_code == 201 and rec["taskName"] == "three" and rec["itemCount"] == 5
    assert requests.post(api.url + "/v2/actor-tasks", headers=H, timeout=5,
                         data=json.dumps({"name": "bad", "actId": "test-items", "input": {"n": "x"}})).status_code == 400
    assert requests.post(api.url + "/v2/actor-tasks/missing/runs", headers=H, data="{}", timeout=5).status_code == 404
    assert requests.delete(api.url + "/v2/actor-tasks/three", headers=H, timeout=5).status_code == 204
    assert _get(api, "/v2/actor-tasks/three").status_code == 404


def test_run_stats(api, storage):
    for n in (1, 2):
        run_actor("test-items", {"n": n}, storage)
    run_actor("test-items", {"fail": True}, storage)
    data = _get(api, "/v2/stats/runs?days=7&last=2").json()["data"]
    assert len(data["days"]) == 7 and data["days"][-1]["SUCCEEDED"] == 2 and data["days"][-1]["FAILED"] == 1
    assert data["totals"] == {"SUCCEEDED": 2, "FAILED": 1} and len(data["recent"]) == 2


def test_openapi_covers_every_documented_route_and_is_published(api):
    paths = spec()["paths"]
    doc = api_mod.__doc__
    for route in re.findall(r"(/v2/[^\s\[?]+)", doc):
        templ = re.sub(r"<(\w+)>", lambda m: "{" + {"key": "key", "name": "name"}.get(m.group(1), "id") + "}", route)
        templ = templ.rstrip(".")
        assert templ in paths, templ
    assert _get(api, "/v2/openapi.json").json()["openapi"] == "3.1.0"
    published = json.loads(Path(__file__).resolve().parents[1].joinpath("docs", "openapi.json").read_text())
    assert published == json.loads(json.dumps(spec())), "run: python -m ww_scraper.openapi"
