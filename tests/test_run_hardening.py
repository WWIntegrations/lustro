"""Regression tests: webhook entries of the wrong type, secrets held for prepared runs,
secret values kept out of run records, budgets of runs started with ctx.call_actor()."""
import base64
import json
import threading
from urllib.parse import quote

import pytest
import requests

import ww_scraper.actor as actor_mod
from ww_scraper import webhooks
from ww_scraper.actor import Actor, execute_run, prepare_run, run_actor
from ww_scraper.actors import ACTORS
from ww_scraper.api import ScraperAPI

SECRET = "sk-live/0123+4567&89abcdef"


class Quick(Actor):
    name = "test-low-quick"
    input_schema = {"type": "object", "properties": {"token": {"type": "string", "secret": True}}}

    def run(self, ctx):
        ctx.push_data([{"ok": True}])


class Keyed(Actor):
    """Leaks its secret into every place that ends up on the run record."""
    name = "test-secret-leak"
    input_schema = {"type": "object", "properties": {
        "apiKey": {"type": "string", "secret": True},
        "mode": {"type": "string", "default": "fail"}}}

    def run(self, ctx):
        key = ctx.input["apiKey"]
        ctx.set_status_message(f"calling the API with {key}")
        ctx.record_failure(f"https://api.example.org/v1?key={key}", f"HTTP 401 for token {key}")
        if ctx.input["mode"] == "fail":
            raise RuntimeError(f"auth failed for key={key} (url-encoded: {quote(key, safe='')})")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setitem(ACTORS, Quick.name, Quick())
    monkeypatch.setitem(ACTORS, Keyed.name, Keyed())


# --- webhook entries of the wrong type ----------------------------------------------------

BAD_HOOKS = [123, None, ["https://x"], {"requestUrl": 5}, {"requestUrl": ""},
             {"requestUrl": "https://hooks.example.org/", "eventTypes": "ACTOR.RUN.SUCCEEDED"}]


def test_bad_webhook_entries_do_not_leave_the_run_running(storage):
    rec = prepare_run(Quick.name, {}, storage)
    rec = execute_run(rec, storage, webhooks=BAD_HOOKS + ["http://[::1"])
    assert rec["status"] == "SUCCEEDED"
    assert storage.load_run(rec["id"])["status"] == "SUCCEEDED"
    log = storage.log_path(rec["id"]).read_text()
    assert "entry 0 skipped" in log and "entry 5 skipped" in log
    assert webhooks.dispatch({"status": "SUCCEEDED"}, {"requestUrl": "https://x"}, allow_hosts=set(),
                             secret="s") == ["webhook ACTOR.RUN.SUCCEEDED: skipped, webhooks must be a list"]


def test_validate_webhooks():
    assert webhooks.validate(["https://hooks.example.org/a",
                              {"requestUrl": "https://hooks.example.org/b", "eventTypes": ["ACTOR.RUN.FAILED"]}]) == []
    assert len(webhooks.validate(BAD_HOOKS)) == len(BAD_HOOKS)
    assert webhooks.validate({"requestUrl": "https://x"}) == ["webhooks must be a list"]


def test_api_refuses_bad_webhook_entries(storage):
    api = ScraperAPI(storage, port=0, token="t")
    api.start()
    try:
        for hooks in ([123], [{"requestUrl": 5}], [{"requestUrl": "https://x", "eventTypes": "x"}]):
            wh = base64.b64encode(json.dumps(hooks).encode()).decode()
            r = requests.post(f"{api.url}/v2/acts/{Quick.name}/runs?webhooks={wh}", data="{}", timeout=10,
                              headers={"Authorization": "Bearer t", "Content-Type": "application/json"})
            assert r.status_code == 400, r.text
        assert storage.list_runs() == []
    finally:
        api.shutdown()


# --- secrets of prepared runs -----------------------------------------------------------------

def test_secrets_of_a_run_aborted_before_it_starts_are_dropped(storage):
    api = ScraperAPI(storage, port=0, token="t", max_runs=1)
    assert api._slots.acquire(blocking=False)        # the one slot is taken
    rec = prepare_run(Quick.name, {"token": "held-secret-value"}, storage)
    assert rec["id"] in actor_mod._SECRETS
    abort = threading.Event()
    abort.set()
    out = api._execute(rec, abort, None)
    assert out["status"] == "ABORTED" and rec["id"] not in actor_mod._SECRETS
    api._slots.release()
    api.httpd.server_close()


def test_held_secrets_are_bounded(storage, monkeypatch):
    monkeypatch.setattr(actor_mod, "_SECRETS", {})
    monkeypatch.setattr(actor_mod, "_SECRETS_MAX", 3)
    ids = [prepare_run(Quick.name, {"token": f"secret-{i}"}, storage)["id"] for i in range(5)]
    assert list(actor_mod._SECRETS) == ids[2:]
    actor_mod.discard_secrets(ids[4])
    assert ids[4] not in actor_mod._SECRETS


# --- T5: secrets never reach the run record ---------------------------------------------------

def _no_secret(blob):
    text = blob if isinstance(blob, str) else json.dumps(blob)
    return SECRET not in text and quote(SECRET, safe="") not in text


def test_secret_is_masked_in_status_message_failures_and_record(storage, monkeypatch):
    monkeypatch.setenv("WW_SCRAPER_WEBHOOK_HOSTS", "")
    rec = run_actor(Keyed.name, {"apiKey": SECRET}, storage)
    assert rec["status"] == "FAILED"
    assert "[secret]" in rec["statusMessage"] and _no_secret(rec["statusMessage"])
    failures = storage.kv(rec["id"]).get("FAILED_REQUESTS")
    assert failures and _no_secret(failures) and "[secret]" in failures[0]["reason"]
    assert _no_secret(storage.run_path(rec["id"]).read_text())
    assert _no_secret(storage.log_path(rec["id"]).read_text())
    # the MCP summary and a webhook payload are built from this record
    from ww_scraper.mcp import _summary
    assert _no_secret(_summary(storage.load_run(rec["id"])))


def test_set_status_message_masks_the_secret(storage):
    rec = run_actor(Keyed.name, {"apiKey": SECRET, "mode": "ok"}, storage)
    assert rec["status"] == "SUCCEEDED"
    assert rec["statusMessage"] == "calling the API with [secret]"


def test_webhook_payload_has_no_secret(storage, monkeypatch):
    sent = []
    import ww_scraper.webhooks as wh
    monkeypatch.setattr(wh, "send", lambda url, payload, **kw: (sent.append(payload), (True, "HTTP 200"))[1])
    rec = run_actor(Keyed.name, {"apiKey": SECRET}, storage, webhooks=["https://hooks.example.org/x"])
    assert rec["status"] == "FAILED" and sent
    assert all(_no_secret(p) for p in sent)


class Spender(Actor):
    """Counts four requests without any network."""
    name = "test-budget-spender"
    input_schema = {"type": "object", "properties": {}}
    seen: list = []

    def run(self, ctx):
        Spender.seen.append(dict(ctx.record.get("options") or {}))
        ctx.stats["requests"] += 4
        ctx.stats["bytes"] += 100


class Caller(Actor):
    name = "test-budget-caller"
    input_schema = {"type": "object", "properties": {}}

    def run(self, ctx):
        ctx.call_actor(Spender.name, {})
        ctx.call_actor(Spender.name, {"maxRequests": 3})
        ctx.call_actor(Spender.name, {}, options={"maxRequests": 500, "maxItems": 500})


def test_called_runs_inherit_what_is_left_of_the_budget(storage, monkeypatch):
    """A pipeline step under a capped (MCP) run is capped too, and its use counts upward."""
    monkeypatch.setitem(ACTORS, Spender.name, Spender())
    monkeypatch.setitem(ACTORS, Caller.name, Caller())
    Spender.seen = []
    rec = run_actor(Caller.name, {}, storage, options={"maxRequests": 10, "maxBytes": 10_000, "maxItems": 20})
    assert [o["maxRequests"] for o in Spender.seen] == [10, 3, 2]
    assert [o["maxItems"] for o in Spender.seen] == [20, 20, 20]
    assert Spender.seen[0]["maxBytes"] == 10_000 and Spender.seen[2]["maxBytes"] == 10_000 - 200
    assert rec["stats"]["calledRequests"] == 12 and rec["stats"]["calledBytes"] == 300
    assert rec["status"] == "SUCCEEDED" and rec["statusMessage"] == "stopped at maxRequests=10"


def test_secret_forms_and_credential_urls_are_masked():
    from ww_scraper.actor import mask_secrets, scrub_url
    s = 'sk-live/0123+4567&89ab"cd'
    forms = [s, quote(s), quote(s, safe=""), quote(s, safe="").lower(), quote(s).lower(),
             json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1]]
    for form in forms:
        out = mask_secrets(f"before {form} after", [s])
        assert out == "before [secret] after", (form, out)
    assert scrub_url("https://user:pw@api.example.org/v1?api_key=abc&page=2#top") == \
        "https://api.example.org/v1?api_key=[secret]&page=2#top"
    assert scrub_url("https://example.org/list?page=2&sort=a") == "https://example.org/list?page=2&sort=a"
    text = mask_secrets("GET https://a:b@x.example.org/p?Token=t0k&x=1 failed: 401", [])
    assert "a:b@" not in text and "t0k" not in text and "x=1" in text


def test_failures_keep_no_userinfo_or_credential_query(storage, monkeypatch):
    class Leaky(Actor):
        name = "test-url-leak"
        input_schema = {"type": "object", "properties": {}}

        def run(self, ctx):
            ctx.record_failure("https://bob:hunter2@api.example.org/v1?access_token=abc123&page=3",
                               "HTTP 403 at https://api.example.org/v1?signature=deadbeef")
            raise RuntimeError("redirected to https://api.example.org/cb?code=oauth-code-1")

    monkeypatch.setitem(ACTORS, Leaky.name, Leaky())
    rec = run_actor(Leaky.name, {}, storage)
    blob = json.dumps(storage.kv(rec["id"]).get("FAILED_REQUESTS")) + rec["statusMessage"]
    for leaked in ("hunter2", "abc123", "deadbeef", "oauth-code-1"):
        assert leaked not in blob and leaked not in storage.log_path(rec["id"]).read_text()
    assert "page=3" in blob
