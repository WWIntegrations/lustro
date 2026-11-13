"""Tasks (saved inputs run by name) and schedules that point at a task - offline."""
import json

import pytest

from ww_scraper.__main__ import main
from ww_scraper.actor import Actor, InputError
from ww_scraper.actors import ACTORS
from ww_scraper.schedule import cron_line, expand_cron, launchd_plist, render
from ww_scraper.tasks import TaskError, list_tasks, load_task, run_task, save_task


class Echo(Actor):
    name = "test-echo"
    input_schema = {"type": "object", "required": ["words"], "properties": {
        "words": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "upper": {"type": "boolean", "default": False}}}

    def run(self, ctx):
        for w in ctx.input["words"]:
            ctx.push_data({"word": w.upper() if ctx.input["upper"] else w})


@pytest.fixture(autouse=True)
def actor(monkeypatch):
    monkeypatch.setitem(ACTORS, Echo.name, Echo())


def test_save_list_and_run_a_task(storage):
    t = save_task(storage, "greet", "test-echo", {"words": ["hi", "yo"], "timeoutSecs": 30})
    assert t["input"] == {"words": ["hi", "yo"]} and t["options"] == {"timeoutSecs": 30.0}
    assert [x["name"] for x in list_tasks(storage)] == ["greet"]
    rec = run_task(storage, "greet")
    assert rec["status"] == "SUCCEEDED" and rec["taskName"] == "greet"
    assert rec["options"] == {"timeoutSecs": 30.0}
    assert [i["word"] for i in storage.dataset(rec["id"]).items()] == ["hi", "yo"]
    rec = run_task(storage, "greet", {"upper": True}, {"datasetName": "greetings"})
    assert [i["word"] for i in storage.dataset(rec["id"]).items()] == ["HI", "YO"]
    assert rec["options"] == {"timeoutSecs": 30.0, "datasetName": "greetings"}
    assert load_task(storage, "greet")["input"] == {"words": ["hi", "yo"]}   # override not saved


def test_task_input_is_validated_when_saved_and_run(storage):
    with pytest.raises(InputError):
        save_task(storage, "bad", "test-echo", {"words": []})
    with pytest.raises(TaskError):
        save_task(storage, "x", "no-such-actor", {})
    with pytest.raises(TaskError):
        save_task(storage, "../evil", "test-echo", {"words": ["a"]})
    save_task(storage, "ok", "test-echo", {"words": ["a"]})
    with pytest.raises(InputError):
        run_task(storage, "ok", {"words": "not a list"})
    with pytest.raises(TaskError):
        run_task(storage, "missing")


def test_task_cli(storage, capsys):
    st = ["--storage", str(storage.root)]
    assert main(["task", "save", "cli", "test-echo", "--input", '{"words": ["a"]}', "--timeout", "60", *st]) == 0
    assert main(["task", "list", *st]) == 0
    out = capsys.readouterr().out
    assert "cli" in out and "test-echo" in out and "timeoutSecs=60.0" in out
    assert main(["task", "run", "cli", "--input", '{"words": ["b", "c"]}', "--quiet", *st]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["taskName"] == "cli" and rec["itemCount"] == 2
    assert main(["task", "show", "cli", *st]) == 0 and '"actId": "test-echo"' in capsys.readouterr().out
    assert main(["task", "run", "nope", *st]) == 2
    assert main(["task", "save", "bad", "test-echo", "--input", "{}", *st]) == 2
    assert "words: field is required" in capsys.readouterr().err
    assert main(["task", "delete", "cli", *st]) == 0
    assert main(["task", "delete", "cli", *st]) == 2


def test_schedule_macros_task_target_and_time_zone(storage, tmp_path, capsys):
    assert expand_cron("@daily") == "0 0 * * *" and expand_cron("@hourly") == "0 * * * *"
    assert expand_cron(" 0  6 * * 1 ") == "0 6 * * 1"
    with pytest.raises(ValueError):
        expand_cron("@reboot")
    line = cron_line(None, "{}", "@weekly", "/srv/l", "/usr/bin/python3", task="greet")
    assert line.startswith("0 0 * * 0 cd /srv/l && /usr/bin/python3 -m ww_scraper task run greet --storage ")
    pl = "".join(launchd_plist(None, "{}", "@daily", "/srv/l", "/usr/bin/python3", task="greet").split())
    assert "<string>task</string><string>run</string><string>greet</string>" in pl
    assert "io.github.wwintegrations.ww-scraper.task.greet" in pl
    out = render(None, "{}", "@monthly", "/srv/l", task="greet", tz="Europe/Warsaw")
    assert "CRON_TZ=Europe/Warsaw" in out and "Nothing was installed" in out
    assert "machine's local time zone" in render("test-echo", "{}", "@daily", "/srv/l")
    with pytest.raises(ValueError):
        render("test-echo", "{}", "@daily", "/srv/l", tz="Mars/Base")

    work = tmp_path / "work"
    save_task(type(storage)(work / "storage"), "greet", "test-echo", {"words": ["a"]})
    assert main(["schedule", "--task", "greet", "--cron", "@daily", "--workdir", str(work)]) == 0
    assert "task run greet" in capsys.readouterr().out
    assert main(["schedule", "--task", "missing", "--cron", "@daily", "--workdir", str(work)]) == 2
    assert main(["schedule", "--cron", "@daily"]) == 2
    assert main(["schedule", "test-echo", "--cron", "@sometimes"]) == 2
