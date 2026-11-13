"""Named storages shared across runs, the datasetName option and purge - offline."""
import datetime
import json

import pytest

from ww_scraper.__main__ import main
from ww_scraper.actor import Actor, InputError, prepare_run, run_actor
from ww_scraper.actors import ACTORS
from ww_scraper.storage import StorageError, check_name, is_run_id, new_id, parse_duration


class Collector(Actor):
    name = "test-collector"
    input_schema = {"type": "object", "properties": {"tag": {"type": "string", "default": "x"}}}

    def run(self, ctx):
        ctx.push_data({"tag": ctx.input["tag"], "where": "run"})
        ctx.dataset("shared-leads").push({"tag": ctx.input["tag"], "contact": "jan.kowalski@example.org"})
        seen = ctx.kv("seen").get("count", 0)
        ctx.kv("seen").set("count", seen + 1)
        ctx.queue("todo").add(f"https://example.org/{ctx.input['tag']}")
        ctx.set_value("OUTPUT", {"seenBefore": seen})


@pytest.fixture(autouse=True)
def actor(monkeypatch):
    monkeypatch.setitem(ACTORS, Collector.name, Collector())


def test_named_storages_persist_and_unnamed_stay_per_run(storage):
    a = run_actor("test-collector", {"tag": "a"}, storage)
    b = run_actor("test-collector", {"tag": "b"}, storage)
    assert storage.dataset(a["id"]).items() == [{"tag": "a", "where": "run"}]
    assert storage.dataset(b["id"]).items() == [{"tag": "b", "where": "run"}]
    shared = storage.named_dataset("shared-leads").items()
    assert [i["tag"] for i in shared] == ["a", "b"]
    assert all(i["contact"] == "[email]" for i in shared)          # same masking as push_data
    assert storage.kv(b["id"]).get("OUTPUT") == {"seenBefore": 1}
    assert storage.named_kv("seen").get("count") == 2
    assert storage.named_queue("todo").total_count == 2


def test_storage_names_cannot_pass_for_run_ids_or_paths():
    rid = new_id()
    assert is_run_id(rid) and not is_run_id("leads")
    for bad in (rid, "../x", "a/b", "", "x" * 65):
        with pytest.raises(StorageError):
            check_name(bad)
    assert check_name("weekly-jobs_2026") == "weekly-jobs_2026"


def test_dataset_name_option_appends_every_run(storage):
    for tag in ("one", "two"):
        rec = run_actor("test-collector", {"tag": tag}, storage, options={"datasetName": "weekly"})
        assert rec["options"]["datasetName"] == "weekly"
    assert [i["tag"] for i in storage.named_dataset("weekly").items()] == ["one", "two"]
    rec = run_actor("test-collector", {"tag": "three", "datasetName": "weekly"}, storage)
    assert len(storage.named_dataset("weekly").items()) == 3
    assert "datasetName" not in storage.kv(rec["id"]).get("INPUT")
    with pytest.raises(InputError):
        prepare_run("test-collector", {}, storage, options={"datasetName": "../etc"})


def _age(storage, rec, days):
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    rec["finishedAt"] = when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"
    storage.save_run(rec)


def test_purge_removes_only_old_unnamed_run_storages(storage, capsys):
    old = run_actor("test-collector", {"tag": "old"}, storage, options={"datasetName": "keep"})
    new = run_actor("test-collector", {"tag": "new"}, storage)
    _age(storage, old, 40)
    live = prepare_run("test-collector", {}, storage)      # READY: never purged
    live["createdAt"] = "2020-01-01T00:00:00.00Z"
    storage.save_run(live)
    root = storage.root

    assert main(["purge", "--older-than", "30d", "--dry-run", "--storage", str(root)]) == 0
    out = capsys.readouterr().out
    assert f"would delete {old['id']}" in out and new["id"] not in out and live["id"] not in out
    assert (root / "datasets" / old["id"]).is_dir()           # dry run deleted nothing

    assert main(["purge", "--older-than", "30d", "--storage", str(root)]) == 0
    assert "deleted 1 run(s)" in capsys.readouterr().out
    assert storage.load_run(old["id"]) is None
    for kind in ("datasets", "key_value_stores", "request_queues"):
        assert not (root / kind / old["id"]).exists()
    assert (root / "datasets" / new["id"]).exists() and (root / "key_value_stores" / new["id"]).exists()
    assert not storage.log_path(old["id"]).exists()
    assert storage.load_run(live["id"]) is not None
    # named storages survive
    assert len(storage.named_dataset("keep").items()) == 1
    assert len(storage.named_dataset("shared-leads").items()) == 2
    assert storage.named_kv("seen").get("count") == 2


def test_purge_rejects_a_bad_duration(storage, capsys):
    assert main(["purge", "--older-than", "soon", "--storage", str(storage.root)]) == 2
    assert parse_duration("12h") == 43200 and parse_duration("2w") == 14 * 86400


def test_cli_dataset_option(storage, capsys):
    assert main(["run", "test-collector", "--dataset", "cli-set", "--quiet",
                 "--storage", str(storage.root)]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["options"] == {"datasetName": "cli-set"}
    assert len(storage.named_dataset("cli-set").items()) == 1
