"""Regression tests for request parsing: Content-Length, non-finite numbers, client base URL."""
import math
import socket

import pytest
import requests

from ww_scraper.actor import Actor, InputError, loads_strict, prepare_run, split_options, validate_input
from ww_scraper.actors import ACTORS
from ww_scraper.api import ScraperAPI
from ww_scraper.checks import validate as validate_checks
from ww_scraper.client import Client, check_base_url


class Items(Actor):
    name = "test-hardening-items"
    input_schema = {"type": "object", "properties": {"n": {"type": "integer", "default": 1},
                                                     "ratio": {"type": "number", "default": 0.5}}}

    def run(self, ctx):
        ctx.push_data([{"i": i} for i in range(ctx.input["n"])])


@pytest.fixture
def api(storage, monkeypatch):
    monkeypatch.setitem(ACTORS, Items.name, Items())
    a = ScraperAPI(storage, port=0, token="t")
    a.start()
    yield a
    a.shutdown()


H = {"Authorization": "Bearer t", "Content-Type": "application/json"}


def _raw(api, request: bytes) -> bytes:
    """Send raw bytes, return everything the server answers until it closes or goes quiet
    for a second (a server stuck reading a body answers nothing)."""
    with socket.create_connection((api.host, api.port), timeout=5) as s:
        s.sendall(request)
        s.settimeout(1)
        out = b""
        try:
            while chunk := s.recv(65536):
                out += chunk
        except OSError:
            pass
        return out


# --- T1: base URL parser confusion ----------------------------------------------------

def test_base_url_parser_confusion_is_refused():
    # urlsplit reads 127.0.0.1 here, urllib3 (what requests connects to) reads evil.com
    for url in ("http://evil.com\\@127.0.0.1:8765", "http://user@127.0.0.1:8765",
                "http://127.0.0.1:8765 @evil.com", "http://127.0.0.1:8765\r\nX: y", "http://127.0.0.1:8765\x7f"):
        with pytest.raises(ValueError):
            check_base_url(url)
        with pytest.raises(ValueError):
            Client(url, token="secret")
    assert check_base_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert check_base_url("http://[::1]:8765") == "http://[::1]:8765"


# --- T2: Content-Length ---------------------------------------------------------------

@pytest.mark.parametrize("length", ["-1", "+5", " -0", "1_0", "0x10", "5.0", "١"])
def test_content_length_must_be_plain_digits(api, length):
    for method, path in (("POST", f"/v2/acts/{Items.name}/runs"),
                         ("PUT", "/v2/key-value-stores/store1/records/k"),
                         ("POST", f"/v2/acts/{Items.name}/run-sync")):
        req = (f"{method} {path} HTTP/1.1\r\nHost: {api.host}:{api.port}\r\nAuthorization: Bearer t\r\n"
               f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n{{}}").encode("utf-8")
        got = _raw(api, req)
        assert got.split(b"\r\n", 1)[0].endswith(b" 400 Bad Request"), (method, path, got[:80])
        assert b"bad Content-Length" in got
        # the handler stops at the error: no second answer, nothing written
        assert got.count(b"HTTP/1.") == 1, got
    assert api.storage.kv("store1").get_record("k") is None


def test_content_length_over_the_cap_is_413(api):
    req = (f"POST /v2/acts/{Items.name}/runs HTTP/1.1\r\nHost: {api.host}:{api.port}\r\n"
           f"Authorization: Bearer t\r\nContent-Type: application/json\r\n"
           f"Content-Length: {2 * 1024 * 1024}\r\n\r\n{{}}").encode()
    got = _raw(api, req)
    assert b" 413 " in got.split(b"\r\n", 1)[0] and got.count(b"HTTP/1.") == 1
    req = (f"PUT /v2/key-value-stores/store1/records/big HTTP/1.1\r\nHost: {api.host}:{api.port}\r\n"
           f"Authorization: Bearer t\r\nContent-Type: text/plain\r\n"
           f"Content-Length: {2 * 1024 * 1024}\r\n\r\nxx").encode()
    got = _raw(api, req)
    assert b" 413 " in got.split(b"\r\n", 1)[0] and got.count(b"HTTP/1.") == 1
    assert api.storage.kv("store1").get_record("big") is None       # refused, not stored empty


# --- T3: NaN and infinity ---------------------------------------------------------------

def test_loads_strict_refuses_non_finite_numbers():
    for text in ("NaN", "Infinity", "-Infinity", '{"a": NaN}', "[1e999]", '{"t": -1e400}'):
        with pytest.raises(ValueError):
            loads_strict(text)
    assert loads_strict('{"a": 1.5, "b": [1, 2e3]}') == {"a": 1.5, "b": [1, 2000.0]}


def test_validate_input_refuses_nan_and_infinity_for_numbers():
    schema = {"properties": {"x": {"type": "number", "minimum": 0, "maximum": 10}}}
    for bad in (math.nan, math.inf, -math.inf):
        clean, errors = validate_input(schema, {"x": bad})
        assert errors and "x" not in clean
    assert validate_input(schema, {"x": 3})[1] == []


def test_run_options_refuse_non_finite_timeout():
    actor = Items()
    for bad in (math.nan, math.inf):
        _, opts, errors = split_options(actor, {"timeoutSecs": bad}, None)
        assert errors and "timeoutSecs" not in opts
        _, opts, errors = split_options(actor, {}, {"timeoutSecs": bad})
        assert errors
    assert validate_checks({"maxFailedRatio": math.nan})
    assert validate_checks({"maxDurationSecs": math.inf})
    assert validate_checks({"fieldFillRate": {"email": math.nan}})


def test_prepare_run_refuses_nan_timeout(storage, monkeypatch):
    monkeypatch.setitem(ACTORS, Items.name, Items())
    with pytest.raises(InputError):
        prepare_run(Items.name, {}, storage, options={"timeoutSecs": math.nan})


@pytest.mark.parametrize("query", ["timeout=nan", "timeout=inf", "timeout=-inf", "timeout=NaN"])
def test_api_refuses_non_finite_timeout(api, query):
    for route in ("runs", "run-sync", "run-sync-get-dataset-items"):
        r = requests.post(f"{api.url}/v2/acts/{Items.name}/{route}?{query}", headers=H, data="{}", timeout=30)
        assert r.status_code == 400, (route, r.text)


@pytest.mark.parametrize("body", ['{"ratio": NaN}', '{"ratio": Infinity}', '{"timeoutSecs": NaN}',
                                  '{"ratio": 1e999}'])
def test_api_refuses_non_finite_json(api, body):
    for route in ("runs", "run-sync"):
        r = requests.post(f"{api.url}/v2/acts/{Items.name}/{route}", headers=H, data=body, timeout=30)
        assert r.status_code == 400 and r.json()["error"]["type"] == "invalid-input", (route, r.text)
    r = requests.put(f"{api.url}/v2/key-value-stores/store1/records/k", headers=H, data=body, timeout=10)
    assert r.status_code == 400


def test_api_refuses_non_finite_checks_and_ignores_nan_wait(api):
    r = requests.post(f"{api.url}/v2/acts/{Items.name}/runs?checks=" + requests.utils.quote('{"maxFailedRatio": NaN}'),
                      headers=H, data="{}", timeout=30)
    assert r.status_code == 400
    r = requests.post(f"{api.url}/v2/acts/{Items.name}/runs?waitForFinish=nan", headers=H, data="{}", timeout=30)
    assert r.status_code == 201
