"""Offline structural tests for the source registry - no network required."""
from lustro.models import Resource
from lustro.sources import SOURCES, get_source


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
