"""Browser interaction over the DevTools pipe: waitForSelector, allowed actions, screenshots
and PDFs into the key-value store, and "render": "auto". Chrome tests skip without Chrome."""
import pytest

from ww_scraper.actor import InputError, run_actor
from ww_scraper.render import (RenderError, find_browser, needs_render, render_page, validate_actions,
                               visible_words)
from ww_scraper.report import build_report

needs_browser = pytest.mark.skipif(find_browser() is None, reason="no Chrome or Chromium installed")


def test_only_the_allowed_actions_pass():
    assert validate_actions([{"click": "#more"}, {"scroll": "bottom"}, {"scroll": 400}, {"wait": 500},
                             {"waitForSelector": ".item"}]) == []
    bad = validate_actions([{"type": "secret"}, {"click": ""}, {"wait": 99999}, {"scroll": -1},
                            {"click": "a", "wait": 1}, "click", {"evaluate": "alert(1)"}])
    assert len(bad) == 7 and "unknown action 'evaluate'" in bad[-1]
    assert validate_actions([{"wait": 1}] * 21)


def test_bad_browser_input_is_refused_before_the_run(storage):
    for bad in ({"browserActions": [{"evaluate": "x"}]}, {"render": "yes"}, {"render": 1},
                {"screenshot": "huge"}):
        with pytest.raises(InputError):
            run_actor("website-content-crawler", dict({"startUrls": ["https://example.org/"]}, **bad), storage)


def test_render_page_refuses_bad_arguments():
    with pytest.raises(RenderError):
        render_page("file:///etc/passwd")
    with pytest.raises(RenderError, match="unknown action"):
        render_page("https://example.org/", actions=[{"evaluate": "1"}])


def test_auto_render_heuristic():
    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    notice = "<html><body><noscript>You need to enable JavaScript to run this app.</noscript><p>x</p></body></html>"
    article = "<html><body><main>" + "<p>word " * 200 + "</main><script>x()</script></body></html>"
    thin = "<html><body><p>Hi</p><script>render()</script></body></html>"
    assert needs_render(shell) and needs_render(notice) and needs_render(thin)
    assert not needs_render(article) and not needs_render("<html><body><p>plain</p></body></html>")
    assert visible_words("<p>a b</p><script>var c = d e f;</script><style>.x{}</style>") == 2


@needs_browser
def test_actions_wait_screenshot_and_pdf(site):
    out = render_page(site.base + "/app", allow_loopback=True, wait_ms=100, wait_for="#more",
                      actions=[{"click": "#more"}, {"waitForSelector": "#extra"}, {"scroll": "bottom"},
                               {"click": "#nothing-here"}],
                      screenshot="fullPage", pdf=True, timeout=60)
    assert "Loaded after a click." in out.html
    assert out.screenshot.startswith(b"\x89PNG") and out.pdf.startswith(b"%PDF-")
    assert out.notes == ["click '#nothing-here': no such element"]
    js = render_page(site.base + "/js", allow_loopback=True, wait_ms=800, timeout=60)
    assert any(d["host"] == "169.254.169.254" for d in js.denied)     # still behind the filtering proxy
    assert 'id="probe">probe refused' in js.html


@needs_browser
def test_crawler_actions_and_the_screenshot_actor(site, storage):
    rec = run_actor("website-content-crawler", {"startUrls": [site.base + "/app"], "maxDepth": 0,
                                                "minIntervalSecs": 0, "waitForSelector": "#more",
                                                "browserActions": [{"click": "#more"}, {"waitForSelector": "#extra"}],
                                                "screenshot": "viewport", "screenshotFormat": "jpeg"},
                    storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec.get("statusMessage")
    (item,) = storage.dataset(rec["id"]).items()
    assert "Loaded after a click." in item["markdown"]
    assert item["screenshotKey"].endswith(".jpg") and item["screenshotUrl"].endswith(item["screenshotKey"])
    data, ctype = storage.kv(rec["id"]).get_record(item["screenshotKey"])
    assert data[:2] == b"\xff\xd8" and ctype.startswith("image/jpeg")

    shots = run_actor("screenshot", {"startUrls": [site.base + "/app", site.base + "/about"], "pdf": True,
                                     "fullPage": True, "minIntervalSecs": 0, "waitMillis": 600},
                      storage, allow_loopback=True)
    assert shots["status"] == "SUCCEEDED", shots.get("statusMessage")
    items = storage.dataset(shots["id"]).items()
    assert len(items) == 2 and all(i["screenshotKey"] and i["pdfKey"] for i in items)
    assert storage.kv(shots["id"]).get("OUTPUT") == {"pages": 2, "screenshots": 2, "pdfs": 2, "failed": 0}
    assert set(shots["output"]) == {"items", "files"}
    html = build_report(storage, shots["id"])
    assert html.count('<img class="thumb" alt="screenshot" src="data:image/png;base64,') == 2
    assert all(a.startswith("ww-scraper/") for a in site.agents)


@needs_browser
def test_auto_renders_only_script_built_pages(site, storage):
    rec = run_actor("website-content-crawler", {"startUrls": [site.base + "/app", site.base + "/about"],
                                                "maxDepth": 0, "minIntervalSecs": 0, "render": "auto"},
                    storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED"
    by = {i["url"].rsplit("/", 1)[1]: i for i in storage.dataset(rec["id"]).items()}
    assert "built by the app" in by["app"]["markdown"]
    assert rec["stats"]["rendered"] == 1 and rec["stats"]["renderSkipped"] == 1


def test_auto_stops_rendering_a_host_where_it_never_helped():
    from ww_scraper.crawler import FetchResult, PoliteCrawler
    c = PoliteCrawler()
    c.render = "auto"
    shell = FetchResult(url="https://a.example/x", final_url="https://a.example/x", status=200,
                        content_type="text/html", body=b'<div id="root"></div><script></script>')
    assert c._should_render(shell)
    c._auto["a.example"] = (PoliteCrawler.AUTO_PROBE, 0)
    assert not c._should_render(shell) and c.stats["renderSkipped"] == 1
    c._auto["a.example"] = (PoliteCrawler.AUTO_PROBE, 1)
    assert c._should_render(shell)
    c.render = False
    assert not c._should_render(shell)
