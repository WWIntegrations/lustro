"""serve --max-runs: extra runs wait READY; a waiting run can be aborted."""
import time

import pytest
import requests

from ww_scraper.actor import Actor
from ww_scraper.actors import ACTORS
from ww_scraper.api import ScraperAPI


class Nap(Actor):
    name = "test-nap"

    def run(self, ctx):
        for _ in range(30):
            ctx.check_abort()
            time.sleep(0.01)


@pytest.fixture
def api(storage, monkeypatch):
    monkeypatch.setitem(ACTORS, Nap.name, Nap())
    a = ScraperAPI(storage, port=0, token="t", max_runs=1)
    a.start()
    yield a
    a.shutdown()


H = {"Authorization": "Bearer t", "Content-Type": "application/json"}


def test_runs_over_the_cap_wait_ready(api, storage):
    ids = [requests.post(api.url + "/v2/acts/test-nap/runs", headers=H, data="{}", timeout=5).json()["data"]["id"]
           for _ in range(3)]
    time.sleep(0.1)
    statuses = [storage.load_run(i)["status"] for i in ids]
    assert statuses.count("RUNNING") == 1 and statuses.count("READY") == 2
    waiting = [storage.load_run(i) for i in ids if storage.load_run(i)["status"] == "READY"]
    assert all("waiting for a free run slot" in r["statusMessage"] for r in waiting)
    r = requests.post(f"{api.url}/v2/actor-runs/{ids[2]}/abort", headers=H, data="{}", timeout=15)
    assert r.json()["data"]["status"] == "ABORTED"
    for i in ids[:2]:
        api.runs[i][0].join(10)
    assert [storage.load_run(i)["status"] for i in ids] == ["SUCCEEDED", "SUCCEEDED", "ABORTED"]
    assert storage.load_run(ids[2])["statusMessage"] == "aborted before it started"
    a, b = sorted((storage.load_run(i) for i in ids[:2]), key=lambda r: r["startedAt"])
    assert b["startedAt"] >= a["finishedAt"]                 # one at a time
