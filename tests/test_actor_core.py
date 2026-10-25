"""Input validation, run lifecycle and abort/failure states - offline."""
import threading

import pytest

from lustro.actor import Actor, InputError, prepare_run, run_actor, validate_input
from lustro.actors import ACTORS, get_actor

SCHEMA = {
    "type": "object",
    "required": ["startUrls"],
    "properties": {
        "startUrls": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "maxPages": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
        "ratio": {"type": "number", "default": 1.0},
        "mode": {"type": "string", "enum": ["a", "b"], "default": "a"},
        "flag": {"type": "boolean", "default": False},
    },
}


def test_defaults_applied_and_int_promoted_to_number():
    clean, errors = validate_input(SCHEMA, {"startUrls": ["https://a.com"], "ratio": 2})
    assert errors == []
    assert clean == {"startUrls": ["https://a.com"], "maxPages": 10, "ratio": 2.0,
                     "mode": "a", "flag": False}


def test_errors_in_english():
    _, errors = validate_input(SCHEMA, {"maxPages": 0, "mode": "z", "flag": "yes",
                                        "typo": 1, "startUrls": [1]}, lang="en")
    text = " | ".join(errors)
    assert "typo: unknown field" in text
    assert "maxPages: must be >= 1" in text
    assert "mode: must be one of a, b" in text
    assert "flag: expected boolean" in text
    assert "startUrls[0]: expected string" in text


def test_errors_in_polish():
    _, errors = validate_input(SCHEMA, {}, lang="pl")
    assert errors == ["startUrls: pole jest wymagane"]
    _, errors = validate_input(SCHEMA, "nope", lang="pl")
    assert errors == ["wejście musi być obiektem JSON"]


def test_bool_is_not_integer():
    _, errors = validate_input(SCHEMA, {"startUrls": ["x"], "maxPages": True})
    assert errors == ["maxPages: expected integer"]


def test_registry_has_seven_actors_with_schemas():
    assert set(ACTORS) == {"website-content-crawler", "sitemap", "seo-audit",
                           "security-headers", "llms-txt", "link-checker", "media-collector"}
    for a in ACTORS.values():
        d = a.describe()
        assert d["name"] and d["inputSchema"]["properties"]
        assert "redactPii" in d["inputSchema"]["properties"]
    assert get_actor("someone~sitemap") is ACTORS["sitemap"]
    assert get_actor("nope") is None


def test_invalid_input_raises_before_any_run(storage):
    with pytest.raises(InputError) as ei:
        run_actor("sitemap", {"startUrls": []}, storage, lang="en")
    assert "startUrls: needs at least 1 item(s)" in ei.value.errors
    assert storage.list_runs() == []
    with pytest.raises(KeyError):
        prepare_run("no-such-actor", {}, storage)


class _Boom(Actor):
    name = "boom"
    input_schema = {"type": "object", "properties": {}}

    def run(self, ctx):
        ctx.push_data({"before": "crash", "mail": "jan@example.com"})
        raise RuntimeError("kaput")


class _Slow(Actor):
    name = "slow"
    input_schema = {"type": "object", "properties": {}}

    def run(self, ctx):
        while True:
            ctx.check_abort()
            threading.Event().wait(0.01)


def test_failed_and_aborted_states(storage, monkeypatch):
    monkeypatch.setitem(ACTORS, "boom", _Boom())
    monkeypatch.setitem(ACTORS, "slow", _Slow())
    rec = run_actor("boom", {}, storage)
    assert rec["status"] == "FAILED" and "kaput" in rec["statusMessage"]
    assert rec["itemCount"] == 1 and rec["startedAt"] and rec["finishedAt"]
    item = storage.dataset(rec["id"]).items()[0]
    assert item["mail"] == "[email]"                       # PII redacted by default
    assert storage.kv(rec["id"]).get("INPUT") == {}
    assert "Traceback" in storage.log_path(rec["id"]).read_text()

    ev = threading.Event()
    threading.Timer(0.1, ev.set).start()
    rec = run_actor("slow", {}, storage, abort_event=ev)
    assert rec["status"] == "ABORTED"
    assert {r["status"] for r in storage.list_runs()} == {"FAILED", "ABORTED"}
