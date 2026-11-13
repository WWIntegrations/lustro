"""articles, events and package-info - offline, against one local server that imitates a
news site, an events site and the npm, RubyGems and Packagist JSON APIs."""
import datetime
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from ww_scraper.actor import InputError, run_actor
from ww_scraper.actors.package_info import PackageInfo

NEXT_YEAR = datetime.date.today().year + 1


def page(jsonld, head="", body="<main><h1>x</h1><p>Body text, write to news@paper.example</p></main>"):
    blocks = "".join(f'<script type="application/ld+json">{json.dumps(j)}</script>' for j in jsonld)
    return f"<html lang='pl'><head><title>T</title>{head}{blocks}</head><body>{body}</body></html>"


SITE = {
    "/news/": page([], body="<a href='/news/a'>a</a><a href='/news/b'>b</a><a href='/news/old'>old</a>"
                            "<a href='/news/og'>og</a><a href='/news/a?utm=1'>dup</a>"),
    "/news/a": page([{"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Port opens new terminal",
                      "datePublished": "2026-11-10T08:00:00+01:00", "dateModified": "2026-11-11T09:00:00+01:00",
                      "author": [{"@type": "Person", "name": "Anna Nowak"}, {"@type": "Person", "name": "Anna Nowak"},
                                 "Desk"],
                      "publisher": {"@type": "Organization", "name": "Daily Paper"},
                      "articleSection": ["Business", "Gdansk"], "keywords": "port, logistics",
                      "image": {"@type": "ImageObject", "url": "https://img.example/a.jpg"},
                      "wordCount": "640", "isAccessibleForFree": True,
                      "description": "<p>Call 601 234 567 for details</p>"}]),
    "/news/b": page([{"@graph": [{"@type": "WebPage"}, {"@type": "BlogPosting", "name": "Notes " + "x" * 120,
                                                       "datePublished": "2026-11-12"}]}]),
    "/news/old": page([{"@type": "Article", "headline": "Archive piece", "datePublished": "2019-01-01",
                        "author": "Ed", "image": "https://img.example/o.jpg"}]),
    "/news/og": "<html><head><meta property='og:type' content='article'><meta property='og:title' content='OG story'>"
                "<meta property='article:published_time' content='2026-11-09'><meta property='article:author' "
                "content='Jan Kowal'><meta property='og:image' content='https://img.example/og.jpg'></head><body>x</body></html>",
    "/events/": page([
        {"@type": "MusicEvent", "name": "Harbour Jazz", "startDate": f"{NEXT_YEAR}-06-01T19:00",
         "endDate": f"{NEXT_YEAR}-06-01T23:00", "eventStatus": "https://schema.org/EventScheduled",
         "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
         "location": {"@type": "Place", "name": "Shipyard Hall", "address": {"@type": "PostalAddress",
                      "streetAddress": "Doki 1", "postalCode": "80-863", "addressLocality": "Gdansk",
                      "addressCountry": {"@type": "Country", "name": "PL"}},
                      "geo": {"@type": "GeoCoordinates", "latitude": "54.36", "longitude": 18.64}},
         "organizer": {"@type": "Organization", "name": "Jazz Club"}, "performer": [{"@type": "MusicGroup", "name": "Trio"}],
         "offers": [{"@type": "Offer", "price": "120", "priceCurrency": "PLN", "url": "https://tickets.example/1",
                     "availability": "https://schema.org/InStock"}, {"@type": "Offer", "price": "80", "priceCurrency": "PLN"}],
         "image": "https://img.example/j.jpg"},
        {"@type": "Event", "name": "Harbour Jazz", "startDate": f"{NEXT_YEAR}-06-01T19:00",
         "location": {"@type": "Place", "name": "Shipyard Hall"}},
        {"@type": "BusinessEvent", "name": "Old Summit", "startDate": "2020-03-01", "location": "Online"},
        {"@type": "EducationEvent", "name": "Webinar", "startDate": f"{NEXT_YEAR}-02-01",
         "endDate": f"{NEXT_YEAR}-01-01",
         "location": {"@type": "VirtualLocation", "url": "https://meet.example/w"}},
        {"@type": "Organization", "name": "Not an event"}]),
}
NPM = {"name": "left-pad", "version": "1.3.0", "description": "String left pad", "license": "WTFPL",
       "homepage": "https://github.com/left-pad/left-pad#readme",
       "repository": {"type": "git", "url": "git+https://github.com/left-pad/left-pad.git"},
       "dependencies": {"b": "^2.0.0", "a": "~1.0.0"}, "deprecated": "use String.prototype.padStart()",
       "maintainers": [{"name": "someone", "email": "someone@example.com"}]}
NPM_SCOPED = {"name": "@scope/tool", "version": "0.1.0", "license": "MIT"}
GEM = {"name": "rails", "version": "8.1.0", "version_created_at": "2026-10-01T00:00:00Z", "licenses": ["MIT"],
       "info": "Full-stack web framework", "homepage_uri": "https://rubyonrails.org",
       "source_code_uri": "https://github.com/rails/rails/tree/v8.1.0", "downloads": 600000000,
       "authors": "Someone", "dependencies": {"runtime": [{"name": "activesupport", "requirements": "= 8.1.0"}],
                                              "development": [{"name": "x", "requirements": ">= 0"}]}}
PACKAGIST = {"package": {"name": "symfony/console", "description": "Eases the creation of CLIs",
                         "repository": "https://github.com/symfony/console", "abandoned": False,
                         "downloads": {"total": 900000000, "monthly": 1, "daily": 1},
                         "versions": {
                             "dev-main": {"version": "dev-main", "time": "2026-11-12T00:00:00+00:00"},
                             "v7.2.0-RC1": {"version": "v7.2.0-RC1", "time": "2026-11-01T00:00:00+00:00"},
                             "v7.1.5": {"version": "v7.1.5", "time": "2026-10-01T00:00:00+00:00", "license": ["MIT"],
                                        "require": {"php": ">=8.2", "symfony/string": "^7.1"}},
                             "v7.1.4": {"version": "v7.1.4", "time": "2026-09-01T00:00:00+00:00", "license": ["MIT"]}}}}


class _Server:
    def __init__(self):
        self.hits = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                srv.hits.append(self.path)
                ctype, code = "text/html", 200
                if u.path == "/robots.txt":
                    body, ctype = "User-agent: *\nDisallow: /packages/blocked/\n", "text/plain"
                elif u.path == "/npm/left-pad/latest":
                    body, ctype = json.dumps(NPM), "application/json"
                elif u.path == "/npm/@scope%2Ftool/latest":
                    body, ctype = json.dumps(NPM_SCOPED), "application/json"
                elif u.path == "/gems/api/v1/gems/rails.json":
                    body, ctype = json.dumps(GEM), "application/json"
                elif u.path == "/packages/symfony/console.json":
                    body, ctype = json.dumps(PACKAGIST), "application/json"
                elif u.path in SITE:
                    body = SITE[u.path]
                else:
                    body, ctype, code = "nf", "text/plain", 404
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def srv():
    s = _Server()
    yield s
    s.httpd.shutdown()


def _items(storage, rec):
    return list(storage.dataset(rec["defaultDatasetId"]).iter_items())


def test_articles(storage, srv):
    rec = run_actor("articles", {"startUrls": [srv.base + "/news/"], "minIntervalSecs": 0},
                    storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec
    by = {i["headline"][:12]: i for i in _items(storage, rec)}
    a = by["Port opens n"]
    assert a["type"] == "NewsArticle" and a["authors"] == ["Anna Nowak", "Desk"]
    assert a["publisher"] == "Daily Paper" and a["section"] == "Business, Gdansk"
    assert a["keywords"] == ["port", "logistics"] and a["wordCount"] == 640 and a["language"] == "pl"
    assert a["image"] == "https://img.example/a.jpg" and a["issues"] == []
    assert "601 234 567" not in a["description"]                 # PII masking still applies
    assert "markdown" not in a
    b = by["Notes xxxxxx"]
    assert b["type"] == "BlogPosting" and "no author" in b["issues"] and "headline longer than 110 characters" in b["issues"]
    og = by["OG story"]
    assert og["source"] == "open-graph" and og["authors"] == ["Jan Kowal"] and og["datePublished"] == "2026-11-09"
    assert len(by) == 4                                            # utm duplicate read once
    newer = run_actor("articles", {"startUrls": [srv.base + "/news/"], "minIntervalSecs": 0,
                                   "publishedAfter": "2026-01-01", "includeText": True}, storage, allow_loopback=True)
    items = _items(storage, newer)
    assert "Archive piece" not in [i["headline"] for i in items] and len(items) == 3
    assert storage.kv(newer["defaultKeyValueStoreId"]).get("OUTPUT")["skippedOlder"] == 1
    assert all("news@paper.example" not in i["markdown"] for i in items)
    with pytest.raises(InputError):
        run_actor("articles", {"startUrls": [srv.base + "/news/"], "publishedAfter": "11/2026"}, storage)


def test_events(storage, srv):
    rec = run_actor("events", {"startUrls": [srv.base + "/events/"], "minIntervalSecs": 0, "maxDepth": 0},
                    storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec
    items = _items(storage, rec)
    assert [i["name"] for i in items] == ["Harbour Jazz", "Webinar"]
    jazz, web = items
    assert jazz["type"] == "MusicEvent" and jazz["status"] == "EventScheduled"
    assert jazz["attendanceMode"] == "OfflineEventAttendanceMode"
    assert jazz["location"] == {"name": "Shipyard Hall", "address": "Doki 1, 80-863, Gdansk, PL", "locality": "Gdansk",
                                "country": "PL", "latitude": 54.36, "longitude": 18.64, "onlineUrl": None}
    assert jazz["organizer"] == "Jazz Club" and jazz["performers"] == ["Trio"]
    assert jazz["price"] == 80 and jazz["highPrice"] == 120 and jazz["currency"] == "PLN"
    assert jazz["ticketUrl"] == "https://tickets.example/1" and jazz["issues"] == []
    assert web["location"]["onlineUrl"] == "https://meet.example/w"
    assert "endDate before startDate" in web["issues"]
    out = storage.kv(rec["defaultKeyValueStoreId"]).get("OUTPUT")
    assert out["skippedPast"] == 1 and out["duplicates"] == 1
    every = run_actor("events", {"startUrls": [srv.base + "/events/"], "minIntervalSecs": 0, "maxDepth": 0,
                                 "upcomingOnly": False}, storage, allow_loopback=True)
    old = [i for i in _items(storage, every) if i["name"] == "Old Summit"][0]
    assert old["location"]["name"] == "Online"


def test_package_info(storage, srv, monkeypatch):
    monkeypatch.setattr(PackageInfo, "NPM", srv.base + "/npm/{}/latest")
    monkeypatch.setattr(PackageInfo, "RUBYGEMS", srv.base + "/gems/api/v1/gems/{}.json")
    monkeypatch.setattr(PackageInfo, "PACKAGIST", srv.base + "/packages/{}.json")
    rec = run_actor("package-info", {"packages": ["npm:left-pad", "npm:@scope/tool", "rubygems:rails",
                                                  "packagist:symfony/console", "packagist:blocked/pkg",
                                                  "npm:../../etc", "npm:left-pad"]},
                    storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec
    by = {i["name"]: i for i in _items(storage, rec)}
    lp = by["left-pad"]
    assert lp["version"] == "1.3.0" and lp["license"] == "WTFPL" and lp["repository"] == "https://github.com/left-pad/left-pad"
    assert lp["dependencies"] == ["a ~1.0.0", "b ^2.0.0"] and lp["dependencyCount"] == 2
    assert lp["deprecated"] and "deprecated" in lp["issues"]
    assert "someone@example.com" not in json.dumps(lp)              # maintainers are never read
    assert by["@scope/tool"]["version"] == "0.1.0"
    rails = by["rails"]
    assert rails["dependencies"] == ["activesupport = 8.1.0"] and rails["downloads"] == 600000000
    assert "authors" not in rails and rails["publishedAt"] == "2026-10-01T00:00:00Z"
    sc = by["symfony/console"]
    assert sc["version"] == "v7.1.5" and sc["license"] == "MIT" and sc["dependencies"] == ["php >=8.2", "symfony/string ^7.1"]
    out = storage.kv(rec["defaultKeyValueStoreId"]).get("OUTPUT")
    assert out == {"requested": 6, "found": 4, "failed": 2, "ecosystems": {"npm": 2, "rubygems": 1, "packagist": 1}}
    failures = storage.kv(rec["defaultKeyValueStoreId"]).get("FAILED_REQUESTS")
    assert any("robots" in json.dumps(f) for f in failures)          # robots.txt of the registry applies
    assert not any("blocked" in h for h in srv.hits)
    with pytest.raises(InputError):
        run_actor("package-info", {"packages": ["pypi:requests"]}, storage)
