"""Export views (fields, omit, flatten, unwind, desc, clean) and the RSS format - offline."""
import json
import xml.etree.ElementTree as ET

import pytest
import requests

from ww_scraper.__main__ import main
from ww_scraper.api import ScraperAPI
from ww_scraper.storage import StorageError, apply_view, flatten_item

ITEMS = [
    {"name": "Firm A", "address": {"city": "Gdańsk", "geo": {"lat": 54.3}}, "#debug": "x",
     "emails": [{"address": "info@a.example", "role": True}, {"address": "biuro@a.example", "role": True}],
     "url": "https://a.example/", "postedAt": "2026-11-10T09:00:00Z", "description": "<b>cafe</b>"},
    {"name": "Firm B", "address": {"city": "Sopot"}, "emails": [], "tags": ["x", "y"],
     "url": "javascript:alert(1)", "date": "Tue, 10 Nov 2026 08:00:00 GMT"},
    {"#only": "hidden"},
]


@pytest.fixture
def ds(storage):
    d = storage.dataset("views")
    d.push(ITEMS)
    return d


def test_flatten_and_unwind_rules():
    assert flatten_item({"a": {"b": {"c": 1}}, "l": [1], "e": {}}) == {"a.b.c": 1, "l": [1], "e": {}}
    rows = apply_view(ITEMS[:2], unwind="emails")
    assert [r.get("address") for r in rows] == ["info@a.example", "biuro@a.example", {"city": "Sopot"}]
    assert rows[0]["name"] == "Firm A" and "emails" not in rows[2]       # empty list: item kept, field gone
    rows = apply_view(ITEMS[1:2], unwind="tags")
    assert [r["tags"] for r in rows] == ["x", "y"]
    assert apply_view(ITEMS[:1], unwind="name") == ITEMS[:1]              # not a list: unchanged


def test_fields_omit_flatten_clean_and_desc(ds):
    body, _ = ds.export("json", fields=["name", "address.city"])
    assert json.loads(body)[:2] == [{"name": "Firm A", "address.city": "Gdańsk"},
                                    {"name": "Firm B", "address.city": "Sopot"}]
    body, _ = ds.export("json", omit=["address.geo", "emails", "description"], clean=True)
    rows = json.loads(body)
    assert len(rows) == 2 and rows[0]["address"] == {"city": "Gdańsk"} and "#debug" not in rows[0]
    body, _ = ds.export("csv", flatten=True, fields=["name", "address.city", "address.geo.lat"])
    assert body.decode().splitlines() == ["name,address.city,address.geo.lat", "Firm A,Gdańsk,54.3",
                                          "Firm B,Sopot,", ",,"]
    body, _ = ds.export("json", desc=True, limit=1, clean=True)
    assert json.loads(body) == []                                         # newest item is all hidden
    body, _ = ds.export("json", desc=True, limit=2)
    assert [r.get("name") for r in json.loads(body)] == [None, "Firm B"]
    body, _ = ds.export("csv", unwind="emails", fields=["name", "address"])
    lines = body.decode().splitlines()
    assert lines[:3] == ["name,address", "Firm A,info@a.example", "Firm A,biuro@a.example"]


def test_rss_feed(ds):
    body, ctype = ds.export("rss", clean=True)
    assert ctype.startswith("application/rss+xml")
    root = ET.fromstring(body)
    items = root.findall("./channel/item")
    assert [i.findtext("title") for i in items] == ["Firm A", "Firm B"]
    assert items[0].findtext("link") == "https://a.example/"
    assert items[0].findtext("pubDate") == "Tue, 10 Nov 2026 09:00:00 GMT"
    assert items[0].findtext("description") == "<b>cafe</b>"           # escaped in the XML, text here
    assert b"<b>" not in body
    assert items[1].find("link") is None                                  # javascript: is not a link
    assert items[1].findtext("pubDate") == "Tue, 10 Nov 2026 08:00:00 GMT"
    assert root.findtext("./channel/link") == "https://a.example/"


def test_rss_needs_a_title_or_url(storage):
    d = storage.dataset("numbers")
    d.push([{"n": 1}])
    with pytest.raises(StorageError):
        d.export("rss")


def test_cli_export_options(ds, storage, capsys):
    st = ["--storage", str(storage.root)]
    assert main(["export", "views", "--format", "csv", "--flatten", "--omit", "emails,tags",
                 "--fields", "name,address.city", "--clean", *st]) == 0
    assert capsys.readouterr().out.splitlines()[:3] == ["name,address.city", "Firm A,Gdańsk", "Firm B,Sopot"]
    assert main(["export", "views", "--format", "jsonl", "--unwind", "emails", "--fields", "address", *st]) == 0
    assert capsys.readouterr().out.count("@a.example") == 2
    assert main(["export", "views", "--format", "rss", "--desc", "--clean", *st]) == 0
    assert capsys.readouterr().out.index("Firm B") < 400
    storage.dataset("numbers").push({"n": 1})
    assert main(["export", "numbers", "--format", "rss", *st]) == 2


def test_api_export_options(ds, storage):
    api = ScraperAPI(storage, port=0, token="t")
    api.start()
    try:
        h = {"Authorization": "Bearer t"}
        r = requests.get(api.url + "/v2/datasets/views/items?format=json&flatten=1&fields=name,address.city"
                         "&clean=true&desc=1", headers=h, timeout=5)
        assert r.json() == [{"name": "Firm B", "address.city": "Sopot"}, {"name": "Firm A", "address.city": "Gdańsk"}]
        r = requests.get(api.url + "/v2/datasets/views/items?unwind=emails&omit=address,description",
                         headers=h, timeout=5)
        assert len(r.json()) == 4
        r = requests.get(api.url + "/v2/datasets/views/items?format=rss&clean=1", headers=h, timeout=5)
        assert r.headers["Content-Type"].startswith("application/rss+xml") and b"<rss" in r.content
    finally:
        api.shutdown()
