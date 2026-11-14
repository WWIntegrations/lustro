"""Offline structural tests for the source registry - no network required."""
from ww_scraper.models import Resource
from ww_scraper.sources import SOURCES, get_source


def test_all_sources_declare_contract():
    assert set(SOURCES) >= {"openverse", "wikimedia", "pexels", "met"}
    for name, src in SOURCES.items():
        assert src.name == name
        assert isinstance(src.allow_hosts, frozenset) and src.allow_hosts
        assert callable(getattr(src, "search", None))


def test_get_source():
    assert get_source("openverse") is SOURCES["openverse"]
    assert get_source("nope") is None


def test_pexels_without_key_yields_nothing():
    import os
    key = os.environ.pop("PEXELS_API_KEY", None)
    try:
        assert get_source("pexels").search(None, "anything", 5) == []
    finally:
        if key is not None:
            os.environ["PEXELS_API_KEY"] = key


def test_sources_assets_stay_on_allowlisted_hosts_fields():
    # allow_hosts are plain hostnames (no scheme/path), ready for the SSRF check
    for src in SOURCES.values():
        for h in src.allow_hosts:
            assert "/" not in h and ":" not in h and h == h.lower()


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Sess:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for prefix, resp in self.answers:
            if url.startswith(prefix):
                return resp
        raise AssertionError(url)


def test_met_uses_the_paginated_search_that_replaced_v1():
    # /v1/search answers 410 since 2026-10-01; /v1.1/search takes limit and offset
    obj = {"isPublicDomain": True, "primaryImage": "https://images.metmuseum.org/a.jpg",
           "primaryImageSmall": "https://images.metmuseum.org/a-s.jpg", "title": "Peak",
           "objectURL": "https://www.metmuseum.org/art/collection/search/1"}
    s = _Sess([("https://collectionapi.metmuseum.org/public/collection/v1.1/search",
                _Resp({"total": 2, "objectIDs": [1, 2]})),
               ("https://collectionapi.metmuseum.org/public/collection/v1/objects/", _Resp(obj))])
    out = get_source("met").search(s, "mountain", 1)
    url, params = s.calls[0]
    assert url.endswith("/v1.1/search") and params["limit"] >= 1 and params["offset"] == 0
    assert [r.title for r in out] == ["Peak"]


def test_wikimedia_thumbnail_host_is_allowed():
    assert "thumb.wikimedia.org" in get_source("wikimedia").allow_hosts


def test_search_reports_each_source(monkeypatch):
    import os

    import ww_scraper
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)

    class Broken:
        name, allow_hosts = "broken", frozenset({"x.example"})

        def search(self, session, query, limit):
            raise RuntimeError("HTTP 410 Gone")

    class Good:
        name, allow_hosts = "good", frozenset({"img.example"})

        def search(self, session, query, limit):
            return [Resource(source="good", title="a", url="https://img.example/a.jpg"),
                    Resource(source="good", title="b", url="https://img.example/b.jpg")]

    srcs = {"broken": Broken(), "good": Good(), "pexels": get_source("pexels")}
    monkeypatch.setattr(ww_scraper, "get_source", srcs.get)
    monkeypatch.setattr("ww_scraper.verify.verify_resource", lambda s, r, allowlist=None: r.title == "a")
    report = {}
    out = ww_scraper.search("q", sources=list(srcs), report=report, session=object())
    assert [r.title for r in out] == ["a"]
    assert report["broken"]["error"] == "RuntimeError: HTTP 410 Gone"
    assert (report["good"]["found"], report["good"]["verified"]) == (2, 1)
    assert "PEXELS_API_KEY" in report["pexels"]["skipped"]
    assert os.environ.get("PEXELS_API_KEY") is None


def test_openverse_asks_once_more_after_a_gateway_timeout(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok = _Resp({"results": [{"title": "Peak", "thumbnail": "https://api.openverse.org/v1/images/x/thumb/",
                             "license": "by", "license_version": "4.0"}]})
    answers = iter([_Resp({}, 504), ok])

    class S:
        def get(self, url, params=None, timeout=None):
            return next(answers)
    assert [r.title for r in get_source("openverse").search(S(), "mountain", 1)] == ["Peak"]
    answers = iter([_Resp({}, 504), _Resp({}, 504)])
    import pytest
    with pytest.raises(RuntimeError, match="504"):
        get_source("openverse").search(S(), "mountain", 1)
