"""Actor state persisted across a failure, a resume and on an interval - offline."""
import time

import pytest

from ww_scraper.actor import Actor, run_actor
from ww_scraper.actors import ACTORS


class Counter(Actor):
    """Counts to 10, one step per run unless told to go on; remembers where it was."""
    name = "test-counter"
    input_schema = {"type": "object", "properties": {"stopAt": {"type": "integer", "default": 10}}}

    def run(self, ctx):
        state = ctx.use_state({"n": 0})
        while state["n"] < 10:
            state["n"] += 1
            ctx.push_data({"n": state["n"]})
            if state["n"] == ctx.input["stopAt"]:
                raise RuntimeError("crash on purpose")


class Ticker(Actor):
    name = "test-ticker"

    def run(self, ctx):
        state = ctx.use_state(persist_secs=0.05)
        state["started"] = True
        time.sleep(0.3)
        assert ctx.kv.get("STATE") == {"started": True}   # saved by the interval, run still going


@pytest.fixture(autouse=True)
def actors(monkeypatch):
    monkeypatch.setitem(ACTORS, Counter.name, Counter())
    monkeypatch.setitem(ACTORS, Ticker.name, Ticker())


def test_state_survives_a_failure_and_a_resume(storage):
    rec = run_actor("test-counter", {"stopAt": 4}, storage)
    assert rec["status"] == "FAILED" and storage.kv(rec["id"]).get("STATE") == {"n": 4}
    rec = run_actor("test-counter", {"stopAt": 99}, storage, run_id=rec["id"])
    assert rec["status"] == "SUCCEEDED"
    assert [i["n"] for i in storage.dataset(rec["id"]).items()] == list(range(1, 11))
    assert storage.kv(rec["id"]).get("STATE") == {"n": 10}


def test_state_is_saved_on_an_interval(storage):
    rec = run_actor("test-ticker", {}, storage)
    assert rec["status"] == "SUCCEEDED", rec["statusMessage"]
