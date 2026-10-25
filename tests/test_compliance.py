"""Offline unit tests for the compliance spine - no network required."""
from lustro.compliance import host_allowed, is_public_address
from lustro.models import Resource, clean_url, sanitize


def test_clean_url_rejects_non_http():
    assert clean_url("javascript:alert(1)") == ""
    assert clean_url("ftp://example.com/a.jpg") == ""
    assert clean_url("https://example.com/a.jpg") == "https://example.com/a.jpg"
    assert clean_url("https://example.com/\n") == ""  # control char


def test_sanitize_strips_controls_and_caps():
    assert sanitize("a\x00b\nc") == "a b c"
    assert len(sanitize("x" * 500, 100)) == 100
    assert sanitize(None) == ""


def test_host_allowed_matches_host_and_subdomains():
    allow = frozenset({"api.openverse.org"})
    assert host_allowed("api.openverse.org", allow)
    assert host_allowed("cdn.api.openverse.org", allow)
    assert not host_allowed("evil.com", allow)
    assert not host_allowed("api.openverse.org.evil.com", allow)


def test_private_addresses_refused():
    assert not is_public_address("127.0.0.1")
    assert not is_public_address("localhost")
    assert not is_public_address("169.254.169.254")  # cloud metadata
    assert not is_public_address("")


def test_resource_sanitizes_on_construction():
    r = Resource(source="openverse", title="  hi\x07there ", url="javascript:x",
                 license="CC BY 4.0", tags=["a", "b\x00c"])
    assert r.title == "hi there"
    assert r.url == ""            # unsafe scheme dropped
    assert r.license == "CC BY 4.0"
    assert r.tags == ["a", "b c"]
