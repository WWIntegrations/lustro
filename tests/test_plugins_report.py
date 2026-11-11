"""File actors, the scaffold, the runs list and the HTML report."""
import pytest

from ww_scraper.__main__ import main
from ww_scraper.actor import run_actor
from ww_scraper.actors import ACTORS
from ww_scraper.plugins import PluginError, load_actor_file, register, scaffold
from ww_scraper.report import build_report


@pytest.fixture(autouse=True)
def _clean_registry():
    before = dict(ACTORS)
    yield
    ACTORS.clear()
    ACTORS.update(before)


def test_scaffold_runs_end_to_end_through_the_cli(site, tmp_path, capsys):
    assert main(["init", "price-watch", "--dir", str(tmp_path)]) == 0
    path = tmp_path / "price_watch.py"
    assert path.is_file()
    assert main(["init", "price-watch", "--dir", str(tmp_path)]) == 2          # never overwrites
    capsys.readouterr()
    code = main(["run", str(path), "--quiet", "--allow-loopback", "--storage", str(tmp_path / "s"),
                 "--print-items", "--input",
                 '{"startUrls": ["%s/"], "minIntervalSecs": 0, "maxDepth": 1}' % site.base])
    out = capsys.readouterr().out
    assert code == 0 and '"h1": "Welcome to the test site"' in out
    assert "/private/secret" not in site.hits                                 # same gates as built-ins


def test_bad_actor_files_are_refused(tmp_path):
    with pytest.raises(PluginError, match="not found"):
        load_actor_file(tmp_path / "nope.py")
    (tmp_path / "none.py").write_text("x = 1\n")
    with pytest.raises(PluginError, match="exactly one"):
        load_actor_file(tmp_path / "none.py")
    (tmp_path / "boom.py").write_text("raise RuntimeError('no')\n")
    with pytest.raises(PluginError, match="RuntimeError"):
        load_actor_file(tmp_path / "boom.py")
    (tmp_path / "name.py").write_text(
        "from ww_scraper import Actor\nclass A(Actor):\n    name = 'Bad Name'\n")
    with pytest.raises(PluginError, match="name must"):
        load_actor_file(tmp_path / "name.py")
    (tmp_path / "shadow.py").write_text(
        "from ww_scraper import Actor\nclass A(Actor):\n    name = 'seo-audit'\n")
    with pytest.raises(PluginError, match="built-in"):
        register(load_actor_file(tmp_path / "shadow.py"))
    with pytest.raises(PluginError):
        scaffold("Bad Name", tmp_path)


def test_api_does_not_load_actor_paths(storage, tmp_path):
    from ww_scraper.actor import prepare_run
    path = scaffold("local-only", tmp_path)
    with pytest.raises(KeyError):
        prepare_run(str(path), {"startUrls": ["https://example.org/"]}, storage)


def test_report_is_self_contained_and_escapes_item_text(site, storage, tmp_path, capsys):
    rec = run_actor("seo-audit", {"startUrls": [site.base + "/"], "minIntervalSecs": 0},
                    storage, allow_loopback=True)
    storage.dataset(rec["id"]).push([{"url": "x", "title": '<script>alert(1)</script><img src=x onerror=1>'}])
    html = build_report(storage, rec["id"])
    assert "<script" not in html.lower() and "&lt;script&gt;alert(1)" in html
    assert "onerror=1&gt;" in html and "<img" not in html
    assert "http://" not in html.replace(site.base, "") and "https://" not in html.replace("https://example", "")
    assert "seo-audit" in html and "SUCCEEDED" in html and "refused by robots.txt" in html
    with pytest.raises(KeyError):
        build_report(storage, "20200101000000-deadbeef")

    out = tmp_path / "r.html"
    assert main(["report", rec["id"], "--storage", str(storage.root), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert main(["report", "../../etc/passwd", "--storage", str(storage.root)]) == 2
    capsys.readouterr()
    assert main(["runs", "--storage", str(storage.root)]) == 0
    assert rec["id"] in capsys.readouterr().out
