"""Regression tests: record key length, delete_run ids, one request per plain-HTTP
connection through the browser's filtering proxy."""
import json
import socket
import threading
import time

import pytest

from ww_scraper.actor import Actor, execute_run, prepare_run
from ww_scraper.actors import ACTORS
from ww_scraper.proxy import FilteringProxy
from ww_scraper.storage import StorageError, valid_key


class Quick(Actor):
    name = "test-low-quick"
    input_schema = {"type": "object", "properties": {"token": {"type": "string", "secret": True}}}

    def run(self, ctx):
        ctx.push_data([{"ok": True}])


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setitem(ACTORS, Quick.name, Quick())


# --- storage ------------------------------------------------------------------------------

def test_record_keys_are_capped_at_200_characters(storage):
    assert valid_key("k" * 200) and not valid_key("k" * 201)
    kv = storage.kv("store1")
    kv.set("k" * 200, {"a": 1})          # the data file, its .meta sidecar and temp names fit
    assert kv.get("k" * 200) == {"a": 1}


def test_delete_run_only_takes_run_ids(storage):
    named = storage.named_dataset("leads")
    named.push([{"a": 1}])
    storage.runs_dir.mkdir(parents=True, exist_ok=True)
    storage.run_path("leads").write_text(json.dumps({"id": "leads", "status": "SUCCEEDED"}))
    assert storage.delete_run("leads") is False
    assert named.count() == 1 and storage.run_path("leads").exists()
    rec = prepare_run(Quick.name, {}, storage)
    rec = execute_run(rec, storage)
    assert storage.delete_run(rec["id"]) is True and storage.load_run(rec["id"]) is None


def test_delete_run_refuses_a_live_run(storage):
    rec = prepare_run(Quick.name, {}, storage)
    rec["status"] = "RUNNING"
    storage.save_run(rec)
    with pytest.raises(StorageError):
        storage.delete_run(rec["id"])


# --- filtering proxy: one request per plain-HTTP connection ---------------------------------

class _Upstream:
    """Raw TCP server that records every byte and answers keep-alive (it ignores
    Connection: close on purpose, like a hostile server would)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.got = b""
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        c.settimeout(0.3)
        end = time.monotonic() + 2
        answered = 0
        while time.monotonic() < end:
            try:
                data = c.recv(65536)
            except socket.timeout:
                data = None
            except OSError:
                break
            if data == b"":
                break
            if data:
                self.got += data
            while self.got.count(b"\r\n\r\n") > answered:
                answered += 1
                try:
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok")
                except OSError:
                    break
        c.close()

    def close(self):
        self.sock.close()


def test_plain_http_proxy_sends_one_request_per_connection():
    up = _Upstream()
    try:
        with FilteringProxy(allow_loopback=True, loopback_port=up.port) as p:
            first = (f"GET http://127.0.0.1:{up.port}/a HTTP/1.1\r\nHost: 127.0.0.1:{up.port}\r\n"
                     "Connection: keep-alive\r\nProxy-Connection: keep-alive\r\nKeep-Alive: 300\r\n\r\n")
            second = "GET http://other.example/b HTTP/1.1\r\nHost: other.example\r\nCookie: for-other\r\n\r\n"
            with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
                s.sendall((first + second).encode())          # pipelined
                time.sleep(0.5)
                try:
                    s.sendall(second.encode())                 # and again on the kept-alive connection
                except OSError:
                    pass
                s.settimeout(3)
                out = b""
                try:
                    while chunk := s.recv(65536):
                        out += chunk
                except OSError:
                    pass
        assert out.startswith(b"HTTP/1.1 200 OK")
        assert up.got.count(b"GET ") == 1, up.got
        head = up.got.split(b"\r\n\r\n", 1)[0].lower()
        assert head.startswith(b"get /a http/1.1") and b"connection: close" in head
        assert b"keep-alive" not in head and b"other.example" not in up.got
    finally:
        up.close()


def test_plain_http_proxy_forwards_a_body_and_nothing_after_it():
    up = _Upstream()
    try:
        with FilteringProxy(allow_loopback=True, loopback_port=up.port) as p:
            req = (f"POST http://127.0.0.1:{up.port}/form HTTP/1.1\r\nHost: 127.0.0.1:{up.port}\r\n"
                   "Content-Type: text/plain\r\nContent-Length: 5\r\n\r\nhelloGET /smuggled HTTP/1.1\r\n\r\n")
            with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
                s.sendall(req.encode())
                s.settimeout(3)
                try:
                    while s.recv(65536):
                        pass
                except OSError:
                    pass
            for bad in (b"Transfer-Encoding: chunked\r\n", b"Content-Length: -1\r\n"):
                r = (f"POST http://127.0.0.1:{up.port}/x HTTP/1.1\r\nHost: 127.0.0.1\r\n").encode() + bad + b"\r\n"
                with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
                    s.sendall(r)
                    assert s.recv(100).startswith(b"HTTP/1.1 400")
        assert up.got.endswith(b"\r\n\r\nhello") and b"smuggled" not in up.got
    finally:
        up.close()
