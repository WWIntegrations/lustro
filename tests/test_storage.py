"""Dataset, KeyValueStore, RequestQueue and run records - offline."""
import json

import pytest

from lustro.storage import (MemoryRequestQueue, RequestQueue, StorageError, normalize_url,
                            valid_id, valid_key)


def test_dataset_append_and_exports(storage):
    ds = storage.dataset("ds1")
    assert ds.push({"a": 1, "b": "x"}) == 1
    assert ds.push([{"a": 2, "c": {"n": 1}}, {"a": 3, "b": "=HYPERLINK(1)"}]) == 2
    assert ds.count() == 3
    assert [i["a"] for i in ds.items(offset=1, limit=1)] == [2]
    body, ctype = ds.export("json")
    assert ctype.startswith("application/json") and len(json.loads(body)) == 3
    body, ctype = ds.export("jsonl")
    assert len(body.decode().strip().split("\n")) == 3
    body, ctype = ds.export("csv")
    lines = body.decode().strip().split("\r\n")
    assert lines[0] == "a,b,c"
    assert "'=HYPERLINK(1)" in lines[3]          # formula injection neutralised
    assert '"{""n"": 1}"' in lines[2]            # nested values JSON-encoded
    with pytest.raises(StorageError):
        ds.export("xml")
    with pytest.raises(StorageError):
        ds.push(["not an object"])


def test_dataset_survives_torn_last_line(storage):
    ds = storage.dataset("torn")
    ds.push({"ok": 1})
    with open(ds.file, "a") as fh:
        fh.write('{"half":')
    assert ds.items() == [{"ok": 1}]


def test_kv_store_content_types(storage):
    kv = storage.kv("kv1")
    kv.set("OUTPUT", {"x": 1})
    kv.set("note.txt", "hello")
    kv.set("blob", b"\x00\x01", "image/png")
    assert kv.get("OUTPUT") == {"x": 1}
    assert kv.get_record("OUTPUT")[1].startswith("application/json")
    assert kv.get("note.txt") == "hello"
    assert kv.get_record("blob") == (b"\x00\x01", "image/png")
    assert kv.keys() == ["OUTPUT", "blob", "note.txt"]
    kv.delete("blob")
    assert kv.get("blob") is None
    for bad in ("../x", "a/b", ".hidden", "", "x" * 300):
        with pytest.raises(StorageError):
            kv.set(bad, "v")


def test_ids_and_keys_validated(storage):
    assert valid_id("20261025-ab12") and not valid_id("../etc") and not valid_id("")
    assert valid_key("llms.txt") and not valid_key("a..b") and not valid_key("a/b")
    with pytest.raises(StorageError):
        storage.dataset("../../etc")
    assert storage.load_run("../x") is None


def test_normalize_url():
    assert normalize_url("HTTP://Example.COM:80#frag") == "http://example.com/"
    assert normalize_url("https://a.com:443/x?q=1#y") == "https://a.com/x?q=1"
    assert normalize_url("https://a.com:8443/") == "https://a.com:8443/"


def test_request_queue_dedup_and_resume(storage):
    q = storage.queue("q1")
    assert q.add("https://a.com/") is True
    assert q.add("https://A.com/#top") is False          # same unique_key
    assert q.add("https://a.com/2", depth=1) is True
    assert q.add("https://a.com/3", depth=1) is True
    r1 = q.fetch_next()
    q.mark_handled(r1)
    r2 = q.fetch_next()          # in progress when the "crash" happens
    assert r2["url"] == "https://a.com/2"
    q.mark_handled(q.fetch_next(), error="boom")
    # reopen from disk: r1 and r3 handled, r2 pending again
    q2 = RequestQueue(storage.root, "q1")
    assert q2.total_count == 3 and q2.handled_count == 2 and q2.pending_count == 1
    nxt = q2.fetch_next()
    assert nxt["url"] == "https://a.com/2" and nxt["depth"] == 1
    assert q2.fetch_next() is None
    q2.reclaim(nxt)
    assert q2.fetch_next()["url"] == "https://a.com/2"


def test_memory_queue_same_api():
    q = MemoryRequestQueue()
    q.add("https://a.com/")
    r = q.fetch_next()
    q.mark_handled(r)
    assert q.is_finished()


def test_run_records_roundtrip(storage):
    storage.save_run({"id": "run-1", "status": "READY"})
    assert storage.load_run("run-1")["status"] == "READY"
    assert [r["id"] for r in storage.list_runs()] == ["run-1"]
