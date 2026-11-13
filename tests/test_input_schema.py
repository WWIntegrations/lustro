"""Input schema: pattern, errorMessage, datepicker dates, prefill and secret fields."""
import json

import pytest

from ww_scraper.actor import Actor, InputError, prepare_run, run_actor, validate_input
from ww_scraper.actors import ACTORS
from ww_scraper.tasks import load_task, run_task, save_task

SCHEMA = {"type": "object", "properties": {
    "board": {"type": "string", "pattern": "^(greenhouse|lever):[a-z0-9-]+$",
              "errorMessage": {"pattern": "use greenhouse:<board> or lever:<company>"}},
    "tags": {"type": "array", "items": {"type": "string", "pattern": "^[a-z]+=[a-z]+$"}},
    "since": {"type": "string", "editor": "datepicker"},
    "note": {"type": "string", "prefill": "hello", "sectionCaption": "Extras"},
    "mode": {"type": "string", "enum": ["a", "b"], "default": "a", "errorMessage": "pick a or b"},
}}


def test_pattern_error_message_and_dates():
    clean, errors = validate_input(SCHEMA, {"board": "lever:acme", "tags": ["shop=bakery"], "since": "2026-11-01"})
    assert errors == [] and "note" not in clean                       # prefill is not a default
    _, errors = validate_input(SCHEMA, {"board": "workday:x", "tags": ["ok=ok", "bad tag"], "since": "1 Nov",
                                        "mode": "z"})
    assert "board: use greenhouse:<board> or lever:<company>" in errors
    assert "tags[1]: does not match the pattern ^[a-z]+=[a-z]+$" in errors
    assert "since: expected a date as YYYY-MM-DD" in errors
    assert "mode: pick a or b" in errors
    _, errors = validate_input(SCHEMA, {"since": "1 Nov"}, lang="pl")
    assert errors == ["since: oczekiwano daty w formacie RRRR-MM-DD"]


class Keyed(Actor):
    name = "test-keyed"
    input_schema = {"type": "object", "required": ["apiKey"], "properties": {
        "apiKey": {"type": "string", "secret": True, "env": "TEST_KEYED_API_KEY"},
        "q": {"type": "string", "default": ""}}}

    def run(self, ctx):
        ctx.log.info(f"calling the API with key {ctx.input['apiKey']}")
        ctx.push_data({"keyLength": len(ctx.input["apiKey"]), "q": ctx.input["q"]})


@pytest.fixture(autouse=True)
def actor(monkeypatch):
    monkeypatch.setitem(ACTORS, Keyed.name, Keyed())
    monkeypatch.delenv("TEST_KEYED_API_KEY", raising=False)


def test_secret_reaches_the_run_but_never_the_disk(storage):
    rec = run_actor("test-keyed", {"apiKey": "sk-very-secret-1234", "q": "x"}, storage)
    assert rec["status"] == "SUCCEEDED"
    assert storage.dataset(rec["id"]).items() == [{"keyLength": 19, "q": "x"}]
    assert storage.kv(rec["id"]).get("INPUT") == {"q": "x"}
    for p in storage.root.rglob("*"):
        if p.is_file():
            assert b"sk-very-secret-1234" not in p.read_bytes(), p
    assert "with key [secret]" in storage.log_path(rec["id"]).read_text()


def test_secret_from_the_environment_and_in_tasks(storage, monkeypatch):
    with pytest.raises(InputError):
        prepare_run("test-keyed", {}, storage)
    monkeypatch.setenv("TEST_KEYED_API_KEY", "env-key-5678")
    rec = run_actor("test-keyed", {}, storage)
    assert rec["status"] == "SUCCEEDED" and storage.dataset(rec["id"]).items()[0]["keyLength"] == 12
    save_task(storage, "keyed", "test-keyed", {"apiKey": "typed-in-key", "q": "t"})
    assert load_task(storage, "keyed")["input"] == {"q": "t"}         # the secret was not saved
    rec = run_task(storage, "keyed")
    assert rec["status"] == "SUCCEEDED" and storage.dataset(rec["id"]).items()[0]["keyLength"] == 12
    # a resumed run gets the secret from the environment again
    rec = run_actor("test-keyed", None, storage, run_id=rec["id"])
    assert rec["status"] == "SUCCEEDED"
    assert "typed-in-key" not in json.dumps([p.read_text(errors="replace") for p in storage.root.rglob("*")
                                             if p.is_file()])
