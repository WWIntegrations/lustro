"""The seven actors end to end against the local test site (offline)."""
import gzip

import pytest

from lustro.actor import run_actor
from lustro.actors.llms_txt import build_llms_txt, validate_llms_txt
from lustro.actors.security_headers import grade_headers
from lustro.actors.sitemap import SitemapError, parse_sitemap


def _run(storage, name, inp):
    inp = dict(inp, minIntervalSecs=0)
    rec = run_actor(name, inp, storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec["statusMessage"]
    return rec, storage.dataset(rec["id"]).items(), storage.kv(rec["id"])


def test_actors_refuse_loopback_without_test_switch(site, storage):
    rec = run_actor("website-content-crawler", {"startUrls": [site.base + "/"]}, storage)
    assert rec["status"] == "SUCCEEDED" and rec["itemCount"] == 0
    assert rec["stats"]["ssrfBlocked"] == 1
    assert storage.kv(rec["id"]).get("FAILED_REQUESTS")[0]["reason"].startswith("ssrf:")
    assert site.hits == []


def test_website_content_crawler(site, storage):
    rec, items, kv = _run(storage, "website-content-crawler", {"startUrls": [site.base + "/"]})
    by_url = {i["finalUrl"]: i for i in items}
    home = by_url[site.base + "/"]
    assert home["title"] == "Lustro Test Site - home page" and home["lang"] == "pl"
    assert home["markdown"].startswith("# Welcome to the test site")
    assert "[email]" in home["markdown"] and "[phone]" in home["markdown"]
    assert "kontakt@example.com" not in home["markdown"]
    assert "Footer text" not in home["markdown"]
    assert site.base + "/noindex" not in by_url                  # noindex not stored
    assert len(by_url) == len(items)                             # redirect -> no duplicate
    assert "/private/secret" not in site.hits
    assert rec["stats"]["robotsBlocked"] >= 1
    assert kv.get("INPUT")["startUrls"] == [site.base + "/"]


def test_website_content_crawler_pii_opt_out(site, storage):
    _, items, _ = _run(storage, "website-content-crawler",
                       {"startUrls": [site.base + "/"], "maxPages": 1, "redactPii": False})
    assert "kontakt@example.com" in items[0]["markdown"]


def test_sitemap(site, storage):
    rec, items, kv = _run(storage, "sitemap", {"startUrls": [site.base]})
    urls = sorted(i["url"] for i in items)
    assert urls == sorted([site.base + "/", site.base + "/about", site.base + "/blog/post-1"])
    home = next(i for i in items if i["url"] == site.base + "/")
    assert home["lastmod"] == "2026-10-01" and home["priority"] == "1.0"
    out = kv.get("OUTPUT")[site.base]
    assert out["robotsSitemaps"] == [site.base + "/sitemap_index.xml"]
    assert {s["kind"] for s in out["sitemaps"]} == {"index", "urlset"}


def test_parse_sitemap_variants():
    xml = (b'<?xml version="1.0"?><!-- c --><ns:urlset xmlns:ns="x"><ns:url><ns:loc>'
           b'https://a.com/?a=1&amp;b=2</ns:loc></ns:url><url><loc><![CDATA[https://a.com/c]]>'
           b'</loc></url></ns:urlset>')
    kind, entries = parse_sitemap(gzip.compress(xml))
    assert kind == "urlset"
    assert [e["url"] for e in entries] == ["https://a.com/?a=1&b=2", "https://a.com/c"]
    assert parse_sitemap(b"https://a.com/1\nhttps://a.com/2\n")[1][1]["url"] == "https://a.com/2"
    with pytest.raises(SitemapError):
        parse_sitemap(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset/>')
    with pytest.raises(SitemapError):
        parse_sitemap(b"<html><body>not a sitemap</body></html>")


def test_parse_sitemap_pathological_input_is_linear():
    import time
    bomb = b'<?xml version="1.0"?><urlset>' + b"<url><loc>" * 100_000 + b"<" * 100_000
    t0 = time.monotonic()
    kind, entries = parse_sitemap(bomb)
    assert time.monotonic() - t0 < 2.0
    assert kind == "urlset" and entries == []
    t0 = time.monotonic()
    parse_sitemap(b"<urlset>" + b"<loc" * 200_000)              # tag never closed
    parse_sitemap(b"<urlset><url><loc><![CDATA[" + b"x" * 1_000_000)
    assert time.monotonic() - t0 < 2.0


def test_seo_audit(site, storage):
    rec, items, kv = _run(storage, "seo-audit", {"startUrls": [site.base + "/"], "maxPages": 10})
    by_url = {i["finalUrl"]: i for i in items}
    home = by_url[site.base + "/"]
    assert home["h1Count"] == 1 and home["canonicalIsSelf"]
    assert home["jsonldTypes"] == ["Organization", "WebSite"]
    assert home["imagesMissingAlt"] == 1
    codes = {i["code"] for i in home["issues"]}
    assert "img-alt-missing" in codes and "og-missing" in codes
    about = by_url[site.base + "/about"]
    about_codes = {i["code"] for i in about["issues"]}
    assert {"description-missing", "h1-multiple", "title-short", "lang-missing"} <= about_codes
    assert by_url[site.base + "/noindex"]["noindex"] is True
    missing = [i for i in items if i["status"] == 404]
    assert missing and missing[0]["issues"][0]["code"] == "status"
    site_out = kv.get("OUTPUT")["sites"][site.base]
    assert site_out == {"robotsTxt": "ok", "robotsSitemaps": [site.base + "/sitemap_index.xml"],
                        "sitemapXml": True, "llmsTxt": True}


def test_security_headers_actor(site, storage):
    _, items, _ = _run(storage, "security-headers",
                       {"startUrls": [site.base + "/", site.base + "/about"]})
    assert len(items) == 1                               # one request per origin
    it = items[0]
    assert it["grade"] in "DEF" and it["checks"]["xContentTypeOptions"] == "nosniff"
    names = {c["name"] for c in it["checks"]["cookies"]}
    assert names == {"sid", "pref"}
    assert "SECRETVALUE" not in str(it)                  # cookie values never stored
    assert site.hits.count("/") == 1


def test_grade_headers_strong_and_weak():
    strong = {
        "strict-transport-security": "max-age=63072000; includeSubDomains",
        "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
        "x-content-type-options": "nosniff", "referrer-policy": "strict-origin-when-cross-origin",
        "permissions-policy": "camera=()", "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
    }
    cookies = [{"name": "s", "secure": True, "httponly": True, "samesite": "Lax"}]
    g = grade_headers(strong, "https://ex.com/", cookies)
    assert g["grade"] == "A" and g["score"] == 100 and g["reasons"] == []
    weak = grade_headers({"content-security-policy": "script-src 'unsafe-inline' 'unsafe-eval'",
                          "referrer-policy": "unsafe-url", "x-powered-by": "PHP/8.1"},
                         "https://ex.com/", [{"name": "t", "secure": False, "httponly": False,
                                              "samesite": None}])
    assert weak["grade"] == "F"
    joined = " ".join(weak["reasons"])
    for frag in ("HSTS", "unsafe-inline", "unsafe-eval", "clickjacking", "nosniff",
                 "leaks full URLs", "Secure", "HttpOnly", "SameSite", "discloses a version"):
        assert frag.lower() in joined.lower()


def test_llms_txt_check(site, storage):
    _, items, _ = _run(storage, "llms-txt", {"startUrls": [site.base]})
    it = items[0]
    assert it["llmsTxt"]["present"] and it["llmsTxt"]["valid"]
    assert it["llmsTxt"]["title"] == "Lustro Test Site"
    assert it["llmsTxt"]["linkCount"] == 3
    assert [s["optional"] for s in it["llmsTxt"]["sections"]] == [False, True]
    assert it["llmsFullTxt"]["present"] and it["llmsFullTxt"]["words"] > 3


def test_llms_txt_generate(site, storage):
    _, items, kv = _run(storage, "llms-txt", {"startUrls": [site.base + "/"], "mode": "generate"})
    text = kv.get("llms.txt")
    assert kv.get_record("llms.txt")[1].startswith("text/markdown")
    assert text.startswith("# Lustro Test Site\n\n> A tiny local website")
    assert f"[About]({site.base}/about)" in text
    assert items[0]["check"]["valid"]


def test_validate_llms_txt_rules():
    assert not validate_llms_txt("<!doctype html><p>spa</p>")["valid"]
    bad = validate_llms_txt("No heading\n\n## S\n\n- plain item\n")
    assert not bad["valid"]
    msgs = " ".join(i["message"] for i in bad["issues"])
    assert "H1" in msgs and "not a link entry" in msgs
    assert build_llms_txt("T", "", [{"url": "https://a.com/", "title": "Home",
                                      "description": ""}]) == "# T\n\n## Main\n\n- [Home](https://a.com/)\n"


def test_link_checker(site, storage):
    rec, items, kv = _run(storage, "link-checker", {"startUrls": [site.base + "/"]})
    by = {i["url"]: i for i in items}
    assert by[site.base + "/missing"]["broken"] and by[site.base + "/missing"]["status"] == 404
    assert by[site.base + "/img/no-alt.png"]["broken"]
    red = by[site.base + "/redirect-1"]
    assert red["redirectHops"] == 2 and not red["broken"] and red["finalUrl"] == site.base + "/about"
    sec = by[site.base + "/private/secret"]
    assert sec["unchecked"] and not sec["broken"]                   # robots: never fetched
    ext = by["https://nonexistent.invalid/page"]
    assert ext["broken"] and ext["error"] == "dns: unresolvable host"
    assert site.base + "/" in by[site.base + "/missing"]["foundOn"]
    summary = kv.get("OUTPUT")
    assert summary["broken"] == 3 and summary["redirectChains"] == 1
    assert "/private/secret" not in site.hits

    _, only, _ = _run(storage, "link-checker", {"startUrls": [site.base + "/"],
                                                "onlyProblems": True, "checkExternal": False})
    assert {i["url"] for i in only} == {site.base + "/missing", site.base + "/img/no-alt.png",
                                         site.base + "/redirect-1"}


def test_media_collector_wraps_search(storage, monkeypatch):
    import lustro
    from lustro.models import Resource

    def fake_search(query, sources=None, limit=12, verify=True, session=None):
        assert query == "koty" and sources == ["openverse"] and limit == 2
        return [Resource(source="openverse", title="Kot", url="https://api.openverse.org/x.jpg",
                         license="CC BY 4.0")]

    monkeypatch.setattr(lustro, "search", fake_search)
    rec = run_actor("media-collector", {"query": "koty", "sources": ["openverse"], "limit": 2},
                    storage)
    assert rec["status"] == "SUCCEEDED"
    assert storage.dataset(rec["id"]).items()[0]["license"] == "CC BY 4.0"
