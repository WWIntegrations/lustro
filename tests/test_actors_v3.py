"""robots-audit, structured-data, change-monitor and feed-reader against the test site."""
import pytest

from ww_scraper.actor import run_actor
from ww_scraper.actors.feed_reader import FeedError, parse_feed
from ww_scraper.actors.structured_data import flatten_jsonld


def _run(storage, name, inp, ok=True):
    rec = run_actor(name, dict(inp, minIntervalSecs=0), storage, allow_loopback=True)
    if ok:
        assert rec["status"] == "SUCCEEDED", rec["statusMessage"]
    return rec, storage.dataset(rec["id"]).items(), storage.kv(rec["id"])


def test_crawler_obeys_wildcard_rules_and_longest_allow(site, crawler):
    assert crawler.fetch(site.base + "/old.bak").error.startswith("robots:")
    assert crawler.fetch(site.base + "/private/secret").error.startswith("robots:")
    assert crawler.fetch(site.base + "/private/open").ok
    assert "/old.bak" not in site.hits and "/private/secret" not in site.hits


def test_robots_audit(site, storage):
    rec, items, kv = _run(storage, "robots-audit", {
        "startUrls": [site.base + "/"], "userAgents": ["badbot/2.0"],
        "testPaths": ["/", "/private/secret", "/private/open", "/old.bak", "https://other.test/x"]})
    (item,) = items
    assert item["status"] == "ok" and item["sitemaps"] == [site.base + "/sitemap_index.xml"]
    assert {tuple(g["userAgents"]) for g in item["groups"]} == {("*",), ("badbot",)}
    ours = {d["path"]: d for d in item["decisions"] if d["userAgent"] == "ww-scraper"}
    assert set(ours) == {"/", "/private/secret", "/private/open", "/old.bak"}   # other origin skipped
    assert ours["/"]["allowed"] and ours["/private/open"]["pattern"] == "/private/open"
    assert ours["/private/secret"] == {"allowed": False, "group": "*", "rule": "disallow",
                                       "pattern": "/private/", "userAgent": "ww-scraper",
                                       "path": "/private/secret"}
    assert ours["/old.bak"]["pattern"] == "/*.bak$"
    bad = [d for d in item["decisions"] if d["userAgent"] == "badbot"]
    assert bad and not any(d["allowed"] for d in bad)
    assert kv.get("OUTPUT") == {"origins": 1, "withRobots": 1, "blockedForUs": 2}
    assert site.hits == ["/robots.txt"]               # the audit fetches nothing else


def test_structured_data(site, storage):
    _, items, kv = _run(storage, "structured-data", {"startUrls": [site.base + "/"], "maxDepth": 0})
    (home,) = items
    assert home["jsonLdTypes"] == ["Organization", "WebSite"]
    assert [o["@type"] for o in home["jsonLd"]] == ["Organization", "WebSite"]
    assert home["openGraph"]["og:site_name"] == "ww-scraper Test Site"
    assert home["twitter"] == {"twitter:card": "summary"}
    assert home["issues"] == ["WebSite: no name, headline or @id"]
    assert kv.get("OUTPUT")["types"] == {"Organization": 1, "WebSite": 1}
    _, items, _ = _run(storage, "structured-data",
                       {"startUrls": [site.base + "/about"], "maxDepth": 0, "onlyWithData": True})
    assert items == []


def test_flatten_jsonld_is_bounded():
    nested = [{"@graph": [{"@type": "A", "name": "a"}] * 500}]
    assert len(flatten_jsonld(nested)) == 100
    assert flatten_jsonld("text") == []


def test_change_monitor_new_changed_removed(site, storage):
    inp = {"startUrls": [site.base + "/changing"], "maxDepth": 0, "monitorName": "t1"}
    _, items, kv = _run(storage, "change-monitor", inp)
    assert [(i["url"], i["change"]) for i in items] == [(site.base + "/changing", "new")]
    assert kv.get("OUTPUT")["firstRun"] is True

    _, items, kv = _run(storage, "change-monitor", inp)
    assert items == [] and kv.get("OUTPUT")["unchanged"] == 1

    site.changing = "2 with more words"
    _, items, _ = _run(storage, "change-monitor", inp)
    assert items[0]["change"] == "changed" and items[0]["wordCountDelta"] == 3

    site.changing = "gone"
    _, items, kv = _run(storage, "change-monitor", inp)
    assert [(i["change"], i["title"]) for i in items] == [("removed", "Changing")]
    assert kv.get("OUTPUT")["removed"] == 1
    snap = storage.kv("monitor-t1").get("SNAPSHOT")
    assert snap["pages"] == {}                         # a removed page leaves the snapshot
    assert "Version" not in str(snap)                  # no page text is kept


def test_change_monitor_revisits_pages_the_crawl_no_longer_links(site, storage):
    _run(storage, "change-monitor", {"startUrls": [site.base + "/changing", site.base + "/about"],
                                     "maxDepth": 0, "monitorName": "t2"})
    site.changing = "gone"
    _, items, _ = _run(storage, "change-monitor", {"startUrls": [site.base + "/about"],
                                                   "maxDepth": 0, "monitorName": "t2"})
    assert [(i["url"], i["change"]) for i in items] == [(site.base + "/changing", "removed")]


def test_change_monitor_rejects_unsafe_names(site, storage):
    rec, _, _ = _run(storage, "change-monitor", {"startUrls": [site.base + "/"],
                                                 "monitorName": "../x"}, ok=False)
    assert rec["status"] == "FAILED" and "monitorName" in rec["statusMessage"]
    assert site.hits == []


def test_feed_reader_discovers_and_reads(site, storage):
    _, items, kv = _run(storage, "feed-reader", {"startUrls": [site.base + "/", site.base + "/atom.xml"]})
    rss = [i for i in items if i["feedType"] == "rss"]
    atom = [i for i in items if i["feedType"] == "atom"]
    assert [i["title"] for i in rss] == ["Post 1 & more", "Post 2"]
    assert rss[0]["link"] == site.base + "/blog/post-1" and rss[0]["id"] == "post-1"
    assert rss[0]["summary"] == "<p>First post.</p>" and rss[0]["feedTitle"] == "Test blog"
    assert rss[0]["published"] == "Thu, 01 Oct 2026 08:00:00 GMT"
    assert atom == [{"title": "Atom entry", "link": site.base + "/blog/post-1", "id": "urn:test:1",
                     "updated": "2026-10-01T08:00:00Z", "summary": "Short summary.",
                     "feedUrl": site.base + "/atom.xml", "feedTitle": "Atom blog", "feedType": "atom"}]
    assert kv.get("OUTPUT") == {"feeds": 2, "entries": 3, "invalid": 0}


def test_parse_feed_refuses_entities_and_non_feeds():
    with pytest.raises(FeedError):
        parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss></rss>')
    with pytest.raises(FeedError):
        parse_feed(b"<html><body>no</body></html>")
    with pytest.raises(FeedError):
        parse_feed(b"")
    assert parse_feed(b"<rss><channel><title>T</title></channel></rss>") == ("rss", "T", [])


def test_change_monitor_revalidates_with_etag_and_still_follows_links(site, storage):
    inp = {"startUrls": [site.base + "/cached"], "maxDepth": 1, "monitorName": "t3"}
    _, items, _ = _run(storage, "change-monitor", inp)
    assert {(i["url"], i["change"]) for i in items} == {(site.base + "/cached", "new"),
                                                        (site.base + "/about", "new")}
    assert site.conditional[-1] == {"ifNoneMatch": None, "ifModifiedSince": None}

    site.hits.clear()
    _, items, kv = _run(storage, "change-monitor", inp)
    assert items == []
    out = kv.get("OUTPUT")
    assert out["unchanged"] == 2 and out["notModified"] == 1
    assert site.conditional[-1] == {"ifNoneMatch": '"v1"', "ifModifiedSince": "Tue, 10 Nov 2026 08:00:00 GMT"}
    assert "/about" in site.hits                         # links of a 304 page are still followed

    site.cached = "2 with more words"
    _, items, kv = _run(storage, "change-monitor", inp)
    assert [(i["url"], i["change"]) for i in items] == [(site.base + "/cached", "changed")]
    assert kv.get("OUTPUT")["notModified"] == 0
    snap = storage.kv("monitor-t3").get("SNAPSHOT")["pages"][site.base + "/cached"]
    assert snap["etag"] == '"v2 with more words"' and snap["links"] == [site.base + "/about"]
    assert "Cached version" not in str(snap)             # still no page text kept


def test_change_monitor_can_skip_conditional_requests(site, storage):
    inp = {"startUrls": [site.base + "/cached"], "maxDepth": 0, "monitorName": "t4",
           "conditionalRequests": False}
    _run(storage, "change-monitor", inp)
    _, _, kv = _run(storage, "change-monitor", inp)
    assert kv.get("OUTPUT")["notModified"] == 0 and kv.get("OUTPUT")["unchanged"] == 1
    assert all(c["ifNoneMatch"] is None for c in site.conditional)


def test_validators_are_sanitized_before_they_are_sent():
    from ww_scraper.crawler import conditional_headers
    assert conditional_headers({"etag": '"abc"', "lastModified": "Tue, 10 Nov 2026 08:00:00 GMT"}) == {
        "If-None-Match": '"abc"', "If-Modified-Since": "Tue, 10 Nov 2026 08:00:00 GMT"}
    assert conditional_headers({"etag": 'W/"x"\r\nX-Evil: 1', "lastModified": "not a date"}) == {}
    assert conditional_headers({"etag": '"' + "a" * 300 + '"'}) == {}
    assert conditional_headers(None) == {} and conditional_headers({"etag": 5}) == {}
