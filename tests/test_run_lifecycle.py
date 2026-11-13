"""Run timeout, TIMED-OUT, ABORTING / TIMING-OUT and live status messages - offline."""
import threading
import time

import pytest
import requests

from ww_scraper.__main__ import main
from ww_scraper.actor import Actor, InputError, prepare_run, request_abort, run_actor
from ww_scraper.actors import ACTORS
from ww_scraper.api import ScraperAPI
from ww_scraper.webhooks import EVENTS


class Slow(Actor):
    """Pushes one item, then works until it is told to stop."""
    name = "test-slow"
    input_schema = {"type": "object", "properties": {"steps": {"type": "integer", "default": 200},
                                                     "email": {"type": "string", "default": ""}}}

    def run(self, ctx):
        for i in range(ctx.input["steps"]):
            ctx.check_abort()
            ctx.push_data({"n": i})
            ctx.set_status_message(f"{i + 1}/{ctx.input['steps']} done {ctx.input['email']}".strip())
            time.sleep(0.02)


class Stubborn(Actor):
    """Ignores the stop request and finishes on its own."""
    name = "test-stubborn"

    def run(self, ctx):
        time.sleep(0.4)
        ctx.push_data({"late": True})


@pytest.fixture(autouse=True)
def actors(monkeypatch):
    monkeypatch.setitem(ACTORS, Slow.name, Slow())
    monkeypatch.setitem(ACTORS, Stubborn.name, Stubborn())


def test_timeout_ends_timed_out_and_keeps_partial_items(storage):
    rec = run_actor("test-slow", {}, storage, timeout_secs=0.3)
    assert rec["status"] == "TIMED-OUT"
    assert rec["options"] == {"timeoutSecs": 0.3}
    items = storage.dataset(rec["defaultDatasetId"]).items()
    assert 0 < len(items) < 200 and rec["itemCount"] == len(items)
    assert rec["statusMessage"].startswith("timed out after 0.3 s (last status: ")
    assert storage.load_run(rec["id"])["status"] == "TIMED-OUT"


def test_timeout_given_in_the_input_is_a_run_option(storage):
    rec = run_actor("test-slow", {"timeoutSecs": 0.2, "steps": 1000}, storage)
    assert rec["status"] == "TIMED-OUT"
    assert "timeoutSecs" not in storage.kv(rec["id"]).get("INPUT")
    with pytest.raises(InputError):
        prepare_run("test-slow", {"timeoutSecs": -1}, storage)
    with pytest.raises(InputError):
        prepare_run("test-slow", {"timeoutSecs": "soon"}, storage)


def test_a_run_inside_its_timeout_succeeds(storage):
    rec = run_actor("test-slow", {"steps": 3}, storage, timeout_secs=30)
    assert rec["status"] == "SUCCEEDED" and rec["statusMessage"] == "3/3 done"


def test_actor_that_ignores_the_stop_still_ends_timed_out(storage):
    rec = run_actor("test-stubborn", {}, storage, timeout_secs=0.1)
    assert rec["status"] == "TIMED-OUT" and rec["itemCount"] == 1


def test_status_message_is_live_and_redacted(storage):
    rec = prepare_run("test-slow", {"steps": 100, "email": "jan.kowalski@example.org"}, storage)
    from ww_scraper.actor import execute_run
    t = threading.Thread(target=execute_run, args=(rec, storage))
    t.start()
    seen = ""
    for _ in range(200):
        seen = (storage.load_run(rec["id"]) or {}).get("statusMessage") or ""
        if seen:
            break
        time.sleep(0.01)
    assert seen.endswith("done [email]") and "/100" in seen
    assert storage.load_run(rec["id"])["status"] == "RUNNING"
    assert request_abort(rec["id"]) is True
    t.join(10)
    final = storage.load_run(rec["id"])
    assert final["status"] == "ABORTED" and final["statusMessage"] == "aborted"
    assert request_abort(rec["id"]) is False


def test_stop_request_shows_aborting_until_the_actor_stops(storage):
    rec = prepare_run("test-stubborn", {}, storage)
    from ww_scraper.actor import execute_run
    t = threading.Thread(target=execute_run, args=(rec, storage))
    t.start()
    while storage.load_run(rec["id"])["status"] != "RUNNING":
        time.sleep(0.01)
    request_abort(rec["id"])
    assert storage.load_run(rec["id"])["status"] == "ABORTING"
    t.join(10)
    assert storage.load_run(rec["id"])["status"] == "ABORTED"


def test_timed_out_has_a_webhook_event():
    assert EVENTS["TIMED-OUT"] == "ACTOR.RUN.TIMED_OUT"


def test_cli_timeout_and_runs_listing_show_the_status(storage, capsys):
    code = main(["run", "test-slow", "--timeout", "0.2", "--quiet", "--storage", str(storage.root)])
    assert code == 1
    capsys.readouterr()
    assert main(["runs", "--storage", str(storage.root)]) == 0
    out = capsys.readouterr().out
    assert "TIMED-OUT" in out and "timed out after 0.2 s" in out


def test_api_timeout_parameter_and_run_view(storage):
    api = ScraperAPI(storage, port=0, token="t", allow_loopback=True)
    api.start()
    try:
        h = {"Authorization": "Bearer t", "Content-Type": "application/json"}
        r = requests.post(api.url + "/v2/acts/test-slow/runs?timeout=0.2&waitForFinish=10",
                          headers=h, data="{}", timeout=15)
        data = r.json()["data"]
        assert r.status_code == 201 and data["status"] == "TIMED-OUT"
        view = requests.get(f"{api.url}/v2/actor-runs/{data['id']}", headers=h, timeout=5).json()["data"]
        assert view["statusMessage"].startswith("timed out after 0.2 s")
        r = requests.post(api.url + "/v2/acts/test-slow/runs?timeout=x", headers=h, data="{}", timeout=5)
        assert r.status_code == 400
    finally:
        api.shutdown()
