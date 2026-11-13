"""Run budgets: maxItems, maxRequests and maxBytes stop a run cleanly - offline."""
import pytest

from ww_scraper.__main__ import main
from ww_scraper.actor import Actor, InputError, prepare_run, run_actor
from ww_scraper.actors import ACTORS


class Many(Actor):
    name = "test-many"

    def run(self, ctx):
        for i in range(100):
            ctx.check_abort()
            ctx.push_data([{"n": i}, {"n": i, "copy": True}])


@pytest.fixture(autouse=True)
def actor(monkeypatch):
    monkeypatch.setitem(ACTORS, Many.name, Many())


def test_max_items_succeeds_with_exactly_that_many(storage):
    rec = run_actor("test-many", {"maxItems": 7}, storage)
    assert rec["status"] == "SUCCEEDED" and rec["itemCount"] == 7
    assert rec["statusMessage"] == "stopped at maxItems=7" and rec["options"] == {"maxItems": 7}


def test_max_requests_and_bytes_stop_a_crawl(storage, site):
    inp = {"startUrls": [site.base], "minIntervalSecs": 0, "maxPages": 50}
    rec = run_actor("website-content-crawler", inp, storage, allow_loopback=True, options={"maxRequests": 3})
    assert rec["status"] == "SUCCEEDED" and rec["statusMessage"] == "stopped at maxRequests=3"
    assert rec["stats"]["requests"] <= 4                     # one may already be in flight
    rec = run_actor("website-content-crawler", dict(inp, maxBytes=1), storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED" and rec["statusMessage"] == "stopped at maxBytes=1"
    assert rec["stats"]["pages"] <= 2


def test_budgets_are_validated(storage):
    for bad in ({"maxItems": 0}, {"maxRequests": "x"}, {"maxBytes": 1.5}):
        with pytest.raises(InputError):
            prepare_run("test-many", bad, storage)


def test_a_timeout_still_wins_over_a_budget_not_reached(storage):
    rec = run_actor("test-many", {}, storage, options={"maxItems": 10_000, "timeoutSecs": 30})
    assert rec["status"] == "SUCCEEDED" and rec["itemCount"] == 200 and rec["statusMessage"] == ""


def test_cli_and_api_budget_options(storage, capsys):
    import json
    import requests
    from ww_scraper.api import ScraperAPI
    assert main(["run", "test-many", "--max-items", "3", "--quiet", "--storage", str(storage.root)]) == 0
    assert json.loads(capsys.readouterr().out)["itemCount"] == 3
    api = ScraperAPI(storage, port=0, token="t")
    api.start()
    try:
        h = {"Authorization": "Bearer t", "Content-Type": "application/json"}
        r = requests.post(api.url + "/v2/acts/test-many/runs?maxItems=5&waitForFinish=10", headers=h,
                          data="{}", timeout=15)
        assert r.json()["data"]["itemCount"] == 5
        assert requests.post(api.url + "/v2/acts/test-many/runs?maxItems=lots", headers=h, data="{}",
                             timeout=5).status_code == 400
    finally:
        api.shutdown()
