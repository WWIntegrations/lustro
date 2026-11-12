"""Web console: static files without data, strict CSP, the log route, live item counts,
and (with Chrome) a hostile scraped value rendered as text, never as markup."""
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import requests

from ww_scraper.actor import prepare_run
from ww_scraper.api import CONSOLE_DIR, ScraperAPI
from ww_scraper.render import find_browser

TOKEN = "c0ns0le-t0ken"


@pytest.fixture
def api(storage):
    a = ScraperAPI(storage, port=0, token=TOKEN, allow_loopback=True)
    a.start()
    yield a
    a.shutdown()


def _h():
    return {"Authorization": f"Bearer {TOKEN}"}


def _finished_run(storage, items, status="SUCCEEDED"):
    rec = prepare_run("website-content-crawler", {"startUrls": ["https://example.org/"]}, storage)
    storage.dataset(rec["id"]).push(items)
    rec.update(status=status, itemCount=len(items), startedAt=rec["createdAt"],
               finishedAt=rec["createdAt"], stats={"items": len(items), "requests": 3, "durationMillis": 420})
    storage.save_run(rec)
    with open(storage.log_path(rec["id"]), "w", encoding="utf-8") as fh:
        fh.write("10:00:00 INFO  run started\n10:00:01 INFO  run finished\n")
    return rec


def test_console_is_served_without_token_and_holds_no_data(api):
    r = requests.get(api.url + "/console", timeout=5)
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/html")
    csp = r.headers["Content-Security-Policy"]
    for part in ("default-src 'none'", "script-src 'self'", "connect-src 'self'", "frame-ancestors 'none'"):
        assert part in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["X-Frame-Options"] == "DENY"
    # no inline script: every <script> loads a same-origin file
    assert all(' src="/console/' in tag for tag in re.findall(r"<script[^>]*>", r.text))
    assert requests.get(api.url + "/console/app.js", timeout=5).headers["Content-Type"].startswith("text/javascript")
    assert requests.get(api.url + "/console/app.css", timeout=5).headers["Content-Type"].startswith("text/css")
    r = requests.get(api.url + "/", timeout=5, allow_redirects=False)
    assert r.status_code == 302 and r.headers["Location"] == "/console"


def test_console_does_not_open_the_data_routes(api, storage):
    _finished_run(storage, [{"url": "https://example.org/", "title": "x"}])
    assert requests.get(api.url + "/v2/actor-runs", timeout=5).status_code == 401
    for path in ("/console/../v2/actor-runs", "/console/app.js/../../v2/acts", "/console/secret.txt",
                 "/console/index.html", "/console/%2e%2e/api.py"):
        r = requests.get(api.url + path, timeout=5)
        assert r.status_code in (401, 404), path
        assert b"actId" not in r.content and b"import" not in r.content


def test_console_keeps_the_host_guard(api):
    r = requests.get(api.url + "/console", headers={"Host": "evil.example"}, timeout=5)
    assert r.status_code == 403


def test_console_script_never_parses_strings_as_html():
    src = (CONSOLE_DIR / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                 "new Function", "srcdoc", "DOMParser", "createContextualFragment"):
        assert sink not in src, sink
    assert "localStorage" not in src          # the token stays in this tab only
    # style-src 'self' blocks style attributes, so layout must live in app.css
    assert not re.search(r"\bstyle\s*:", src) and ".style." not in src


def test_log_route(api, storage):
    rec = _finished_run(storage, [])
    r = requests.get(f"{api.url}/v2/actor-runs/{rec['id']}/log", timeout=5)
    assert r.status_code == 401
    r = requests.get(f"{api.url}/v2/actor-runs/{rec['id']}/log", headers=_h(), timeout=5)
    assert r.status_code == 200 and "run finished" in r.text
    assert r.headers["Content-Security-Policy"] == "sandbox"
    assert requests.get(f"{api.url}/v2/actor-runs/nope/log", headers=_h(), timeout=5).status_code == 404
    assert requests.get(f"{api.url}/v2/actor-runs/..%2f..%2fx/log", headers=_h(), timeout=5).status_code == 404


def test_log_route_returns_only_the_tail(api, storage, monkeypatch):
    import ww_scraper.api as api_mod
    monkeypatch.setattr(api_mod, "MAX_LOG", 64)
    rec = _finished_run(storage, [])
    with open(storage.log_path(rec["id"]), "w", encoding="utf-8") as fh:
        fh.writelines(f"line {n:04d}\n" for n in range(100))
    text = requests.get(f"{api.url}/v2/actor-runs/{rec['id']}/log", headers=_h(), timeout=5).text
    assert text.endswith("line 0099\n") and len(text) <= 64 and text.startswith("line ")


def test_live_run_reports_items_stored_so_far(api, storage):
    rec = _finished_run(storage, [{"a": 1}, {"a": 2}], status="RUNNING")
    rec["itemCount"] = 0
    storage.save_run(rec)
    data = requests.get(f"{api.url}/v2/actor-runs/{rec['id']}", headers=_h(), timeout=5).json()["data"]
    assert data["itemCount"] == 2


def test_console_files_ship_with_the_package():
    assert {p.name for p in CONSOLE_DIR.iterdir()} >= {"index.html", "app.js", "app.css"}
    text = Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(encoding="utf-8")
    assert 'ww_scraper = ["console/*"]' in text


HOSTILE = '<img src=x onerror="document.title=\'pwned\'"><script>document.title="pwned"</script>'


@pytest.mark.skipif(find_browser() is None, reason="no Chrome or Chromium installed")
def test_console_renders_a_hostile_item_as_text_in_chrome(api, storage):
    rec = _finished_run(storage, [
        {"url": "https://example.org/a", "title": HOSTILE, "note": "javascript:alert(1)"},
        {"url": "javascript:alert(1)", "title": "plain"},
    ])
    url = f"{api.url}/console#/runs/{rec['id']}&token={TOKEN}"
    with tempfile.TemporaryDirectory() as profile:
        out = subprocess.run(
            [find_browser(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--disable-background-networking", f"--user-data-dir={profile}",
             "--virtual-time-budget=6000", "--dump-dom", url],
            capture_output=True, text=True, timeout=90).stdout
    assert 'id="items"' in out, out[-2000:]                    # the console really rendered the run
    assert "run finished" in out                               # log route reached with the token
    assert "<title>ww-scraper console</title>" in out          # no script ran to change the title
    assert "&lt;img src=x" in out and "<img src" not in out    # the value is text, not an element
    assert 'href="javascript:' not in out                      # only http(s) values become links
    assert 'href="https://example.org/a"' in out
    assert "token=" not in out.split("<body", 1)[1]            # the fragment was cleared, not echoed
