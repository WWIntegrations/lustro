"""Post-run checks: evaluated after the run, stored on the record, exit code 3, webhook alert."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from ww_scraper.__main__ import main
from ww_scraper.actor import Actor, InputError, run_actor
from ww_scraper.actors import ACTORS
from ww_scraper.api import ScraperAPI
from ww_scraper.checks import evaluate, validate
from ww_scraper.tasks import run_task, save_task


class People(Actor):
    name = "test-people"
    input_schema = {"type": "object", "properties": {"n": {"type": "integer", "default": 4}}}

    def run(self, ctx):
        ctx.push_data([{"name": f"p{i}", "email": "x@example.com" if i % 2 else None,
                        "address": {"city": "Gdansk"}} for i in range(ctx.input["n"])])


@pytest.fixture(autouse=True)
def actor(monkeypatch):
    monkeypatch.setitem(ACTORS, People.name, People())


def test_evaluate_reports_every_check():
    rec = {"status": "SUCCEEDED", "itemCount": 4, "stats": {"pages": 8, "failed": 2, "durationMillis": 1500}}
    items = [{"email": "a" if i % 2 else "", "address": {"city": "X"}} for i in range(4)]
    out = evaluate({"minItems": 5, "maxItems": 10, "maxFailed": 1, "maxFailedRatio": 0.25,
                    "maxDurationSecs": 2, "statuses": ["SUCCEEDED"],
                    "fieldFillRate": {"email": 0.5, "address.city": 1, "phone": 0.1}}, rec, items)
    by = {r["check"]: r for r in out["results"]}
    assert out["passed"] is False
    assert by["minItems"] == {"check": "minItems", "expected": 5, "actual": 4, "passed": False}
    assert by["maxItems"]["passed"] and by["statuses"]["passed"] and by["maxDurationSecs"]["passed"]
    assert by["maxFailed"]["passed"] is False
    assert by["maxFailedRatio"]["actual"] == 0.2 and by["maxFailedRatio"]["passed"]
    assert by["fieldFillRate.email"]["actual"] == 0.5 and by["fieldFillRate.email"]["passed"]
    assert by["fieldFillRate.address.city"]["passed"]
    assert by["fieldFillRate.phone"]["actual"] == 0.0 and not by["fieldFillRate.phone"]["passed"]


def test_fill_rate_of_an_empty_dataset_is_zero():
    out = evaluate({"fieldFillRate": {"email": 0.1}}, {"itemCount": 0}, [])
    assert out["results"][0]["actual"] == 0.0 and not out["passed"]


def test_validate_rejects_bad_checks():
    assert validate({"minItems": 1, "fieldFillRate": {"a": 0.5}}) == []
    errs = validate({"minItem": 1, "minItems": -1, "maxFailedRatio": 2, "fieldFillRate": {"a": 3},
                     "statuses": "SUCCEEDED"})
    assert len(errs) == 5
    assert validate([]) == ["checks: expected an object"]


def test_run_with_checks_stores_the_result(storage):
    rec = run_actor("test-people", {}, storage, options={"checks": {"minItems": 3,
                                                                    "fieldFillRate": {"email": 0.4}}})
    assert rec["status"] == "SUCCEEDED"
    assert rec["checks"]["passed"] is True
    assert storage.load_run(rec["id"])["checks"] == rec["checks"]
    bad = run_actor("test-people", {"n": 1}, storage, options={"checks": {"minItems": 3}})
    assert bad["status"] == "SUCCEEDED" and bad["checks"]["passed"] is False
    assert bad["checks"]["results"][0]["actual"] == 1


def test_invalid_checks_refuse_the_run(storage):
    with pytest.raises(InputError) as e:
        run_actor("test-people", {}, storage, options={"checks": {"minItems": "x"}})
    assert any("checks.minItems" in err for err in e.value.errors)


def test_cli_exit_code_3_when_a_check_fails(storage, capsys):
    root = str(storage.root)
    assert main(["run", "test-people", "--storage", root, "--quiet", "--checks", '{"minItems": 2}']) == 0
    assert main(["run", "test-people", "--storage", root, "--quiet", "--checks", '{"minItems": 9}']) == 3
    assert "checks failed: minItems" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["run", "test-people", "--storage", root, "--checks", "{nope"])


def test_task_carries_checks(storage, capsys):
    save_task(storage, "people-check", "test-people", {"n": 2}, {"checks": {"minItems": 3}})
    rec = run_task(storage, "people-check")
    assert rec["checks"]["passed"] is False
    assert main(["task", "run", "people-check", "--storage", str(storage.root), "--quiet"]) == 3
    assert main(["task", "run", "people-check", "--storage", str(storage.root), "--quiet",
                 "--checks", '{"minItems": 1}']) == 0


class _Hook(BaseHTTPRequestHandler):
    got = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()


def test_failed_checks_send_an_alert(storage, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/h"
    monkeypatch.setenv("WW_SCRAPER_WEBHOOK_HOSTS", "127.0.0.1")
    monkeypatch.setenv("WW_SCRAPER_WEBHOOK_SECRET", "s")
    try:
        _Hook.got = []
        run_actor("test-people", {}, storage, allow_loopback=True, webhooks=[url],
                  options={"checks": {"minItems": 1}})
        assert [e["eventType"] for e in _Hook.got] == ["ACTOR.RUN.SUCCEEDED"]
        _Hook.got = []
        run_actor("test-people", {}, storage, allow_loopback=True, webhooks=[url],
                  options={"checks": {"minItems": 100}})
        assert [e["eventType"] for e in _Hook.got] == ["ACTOR.RUN.SUCCEEDED", "ACTOR.RUN.CHECKS_FAILED"]
        assert _Hook.got[1]["resource"]["checks"]["passed"] is False
        _Hook.got = []
        run_actor("test-people", {}, storage, allow_loopback=True,
                  webhooks=[{"requestUrl": url, "eventTypes": ["ACTOR.RUN.SUCCEEDED"]}],
                  options={"checks": {"minItems": 100}})
        assert [e["eventType"] for e in _Hook.got] == ["ACTOR.RUN.SUCCEEDED"]
    finally:
        srv.shutdown()


def test_api_checks_parameter(storage):
    api = ScraperAPI(storage, port=0, token="t")
    api.start()
    try:
        h = {"Authorization": "Bearer t"}
        r = requests.post(f"{api.url}/v2/acts/test-people/runs",
                          params={"waitForFinish": 20, "checks": json.dumps({"minItems": 10})}, json={}, headers=h)
        assert r.status_code == 201
        assert r.json()["data"]["checks"]["passed"] is False
        r = requests.post(f"{api.url}/v2/acts/test-people/runs", params={"checks": "{bad"}, json={}, headers=h)
        assert r.status_code == 400
    finally:
        api.shutdown()
