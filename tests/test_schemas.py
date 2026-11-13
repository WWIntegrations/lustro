"""Output, dataset and key-value store schemas, views and actor versions - offline."""
import json

import pytest

from ww_scraper import __version__
from ww_scraper.__main__ import main
from ww_scraper.actor import Actor, run_actor
from ww_scraper.actors import ACTORS
from ww_scraper.schema import SchemaValidationError, check, check_record, resolve_output
from ww_scraper.storage import StorageError

ITEM = {"type": "object", "required": ["name", "price"], "additionalProperties": False,
        "properties": {"name": {"type": "string", "minLength": 1}, "price": {"type": ["number", "null"], "minimum": 0},
                       "tags": {"type": "array", "items": {"type": "string", "pattern": "^[a-z]+$"}}}}


def test_json_schema_subset():
    assert check(ITEM, {"name": "a", "price": None, "tags": ["x"]}) == []
    errors = check(ITEM, {"name": "", "price": -1, "tags": ["X", 2], "extra": 1})
    assert errors == ["item.name: shorter than 1", "item.price: must be >= 0", "item.tags[0]: does not match ^[a-z]+$",
                      "item.tags[1]: expected string", "item.extra: not allowed"]
    assert check(ITEM, {"name": "a"}) == ["item.price: required"]
    assert check(ITEM, []) == ["item: expected object"]


class Shop(Actor):
    name = "test-shop"
    version = "2.3.1"
    input_schema = {"type": "object", "properties": {"bad": {"type": "boolean", "default": False},
                                                     "badKv": {"type": "boolean", "default": False}}}
    dataset_schema = {"fields": ITEM, "views": {"cheap": {"title": "Cheap", "fields": ["name", "price"], "desc": True}}}
    kv_schema = {"collections": {"summary": {"key": "OUTPUT", "contentTypes": ["application/json"],
                                             "jsonSchema": {"type": "object", "required": ["count"]}},
                                 "pages": {"keyPrefix": "page-", "contentTypes": ["text/html"]}}}
    output_schema = {"cheap": {"title": "Cheap items", "template": "/v2/datasets/{defaultDatasetId}/items?view=cheap"},
                     "run": {"template": "/v2/actor-runs/{id}"}}

    def run(self, ctx):
        ctx.push_data([{"name": "a", "price": 1}, {"name": "b", "price": 2, "tags": ["new"]}])
        if ctx.input["bad"]:
            ctx.push_data([{"name": "c", "price": 3}, {"name": "d", "price": "free"}])
        ctx.set_value("page-1", "<p>x</p>", "text/html")
        ctx.set_value("OUTPUT", {"count": 2} if not ctx.input["badKv"] else {"total": 2})


@pytest.fixture(autouse=True)
def actor(monkeypatch):
    monkeypatch.setitem(ACTORS, Shop.name, Shop())


def test_valid_run_records_version_output_and_schemas(storage):
    rec = run_actor("test-shop", {}, storage)
    assert rec["status"] == "SUCCEEDED" and rec["actVersion"] == "2.3.1"
    assert rec["output"] == {"cheap": {"title": "Cheap items", "url": f"/v2/datasets/{rec['id']}/items?view=cheap"},
                             "run": {"title": "run", "url": f"/v2/actor-runs/{rec['id']}"}}
    ds = storage.dataset(rec["id"])
    assert ds.schema()["views"]["cheap"]["desc"] is True
    body, _ = ds.export("json", view="cheap")
    assert json.loads(body) == [{"name": "b", "price": 2}, {"name": "a", "price": 1}]
    with pytest.raises(StorageError):
        ds.export("json", view="nope")
    kv = storage.kv(rec["id"])
    assert kv.keys(collection="pages") == ["page-1"] and kv.keys(collection="summary") == ["OUTPUT"]
    with pytest.raises(StorageError):
        kv.keys(collection="nope")
    assert ACTORS["sitemap"].describe()["version"] == __version__
    assert ACTORS["test-shop"].describe()["datasetSchema"]["views"]["cheap"]["title"] == "Cheap"


def test_one_invalid_item_rejects_the_whole_push(storage):
    rec = run_actor("test-shop", {"bad": True}, storage)
    assert rec["status"] == "FAILED" and "SchemaValidationError" in rec["statusMessage"]
    assert "item[1].price: expected number or null" in rec["statusMessage"]
    assert [i["name"] for i in storage.dataset(rec["id"]).items()] == ["a", "b"]    # c was not stored either
    assert rec["stats"]["invalidItems"] == 2


def test_kv_collection_rules(storage):
    rec = run_actor("test-shop", {"badKv": True}, storage)
    assert rec["status"] == "FAILED" and "OUTPUT.count: required" in rec["statusMessage"]
    schema = Shop.kv_schema
    assert check_record(schema, "page-2", b"x", "image/png") == [
        "page-2: content type image/png not allowed in collection pages (text/html)"]
    assert check_record(schema, "other", b"x", "image/png") == []


def test_output_templates_and_cli_view(storage, capsys):
    assert resolve_output({"x": {"template": "/v2/datasets/{defaultDatasetId}/{unknown}"}},
                          {"defaultDatasetId": "d1"}) == {"x": {"title": "x", "url": "/v2/datasets/d1/{unknown}"}}
    rec = run_actor("test-shop", {}, storage)
    assert main(["export", rec["id"], "--view", "cheap", "--format", "csv", "--storage", str(storage.root)]) == 0
    assert capsys.readouterr().out.splitlines() == ["name,price", "b,2", "a,1"]
    assert main(["export", rec["id"], "--view", "nope", "--storage", str(storage.root)]) == 2


def test_ready_actors_declare_item_schemas():
    for name in ("job-postings", "places", "contact-details", "product-catalog", "lead-list"):
        d = ACTORS[name].describe()
        assert d["datasetSchema"]["fields"]["required"] and d["datasetSchema"]["views"], name
