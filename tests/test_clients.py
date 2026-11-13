"""The Python client (sync and async, with retries) and the JavaScript client - offline."""
import asyncio
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ww_scraper.actor import Actor
from ww_scraper.actors import ACTORS
from ww_scraper.api import ScraperAPI
from ww_scraper.client import ApiError, AsyncClient, Client

JS_CLIENT = Path(__file__).resolve().parents[1] / "clients" / "js" / "ww-scraper-client.mjs"


class Words(Actor):
    name = "test-words"
    input_schema = {"type": "object", "properties": {"words": {"type": "array", "items": {"type": "string"},
                                                               "default": ["a", "b"]}}}

    def run(self, ctx):
        ctx.push_data([{"word": w} for w in ctx.input["words"]])
        ctx.set_value("OUTPUT", {"count": len(ctx.input["words"])})


@pytest.fixture
def api(storage, monkeypatch):
    monkeypatch.setitem(ACTORS, Words.name, Words())
    a = ScraperAPI(storage, port=0, token="tok")
    a.start()
    yield a
    a.shutdown()


def test_sync_client_round_trip(api):
    c = Client(api.url, "tok", backoff=0.01)
    assert "test-words" in {a["name"] for a in c.actors()}
    rec = c.call("test-words", {"words": ["x", "y", "z"]}, timeout=20)
    assert rec["status"] == "SUCCEEDED" and rec["itemCount"] == 3
    assert c.items(rec["defaultDatasetId"], fields=["word"], desc=True) == [{"word": "z"}, {"word": "y"}, {"word": "x"}]
    assert c.call_items("test-words", {"words": ["q"]}) == [{"word": "q"}]
    assert "finished: SUCCEEDED" in c.log(rec["id"])
    c.put_record("notes", "n1", {"a": 1})
    assert c.record("notes", "n1") == {"a": 1}                    # JSON records come back parsed
    assert [k["key"] for k in c.keys("notes")] == ["n1"]
    c.save_task("tw", "test-words", {"words": ["t"]})
    assert c.run_task("tw", wait=10)["itemCount"] == 1 and c.tasks()[0]["name"] == "tw"
    assert c.stats(days=1)["totals"]["SUCCEEDED"] >= 3
    with pytest.raises(ApiError) as e:
        c.start("test-words", {"words": "nope"})
    assert e.value.status == 400
    with pytest.raises(ApiError):
        Client(api.url, "wrong", retries=0).actors()
    with pytest.raises(ValueError):
        Client("http://example.org:8765", "tok")


def test_async_client(api):
    async def go():
        c = AsyncClient(api.url, "tok")
        rec = await c.call("test-words", {}, timeout=20)
        return rec, await c.items(rec["defaultDatasetId"])
    rec, items = asyncio.run(go())
    assert rec["status"] == "SUCCEEDED" and items == [{"word": "a"}, {"word": "b"}]


def test_retries_on_503_with_retry_after():
    hits = []

    class Flaky(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            if len(hits) < 3:
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            body = json.dumps({"data": {"items": [{"name": "ok"}]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(f"http://127.0.0.1:{srv.server_address[1]}", "t", backoff=0.01)
        assert c.actors() == [{"name": "ok"}] and len(hits) == 3
        hits.clear()
        with pytest.raises(ApiError):
            Client(f"http://127.0.0.1:{srv.server_address[1]}", "t", retries=1, backoff=0.01).actors()
        assert len(hits) == 2
    finally:
        srv.shutdown()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_javascript_client(api, tmp_path):
    script = tmp_path / "use.mjs"
    script.write_text(f"""
import {{ Client, ApiError }} from {json.dumps(JS_CLIENT.as_uri())};
const c = new Client(process.argv[2], "tok", {{ backoff: 10 }});
const run = await c.call("test-words", {{ words: ["j", "s"] }}, {{ timeout: 20 }});
const items = await c.items(run.defaultDatasetId, {{ desc: true }});
let refused = 0;
try {{ await c.start("test-words", {{ words: 1 }}); }} catch (e) {{ if (e instanceof ApiError) refused = e.status; }}
console.log(JSON.stringify({{ status: run.status, items, refused, tasks: await c.tasks() }}));
""", encoding="utf-8")
    out = subprocess.run(["node", str(script), api.url], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got == {"status": "SUCCEEDED", "items": [{"word": "s"}, {"word": "j"}], "refused": 400, "tasks": []}


REMOTE = ["http://127.evil.example:8765", "http://127.0.0.1.nip.io", "http://localhost:8765",
          "http://example.com:8765", "http://[::ffff:127.0.0.1]:8765", "http://10.0.0.1:8765"]
LOCAL = ["http://127.0.0.1:8765", "http://[::1]:8765", "http://127.1.2.3:8765"]


@pytest.mark.parametrize("url", REMOTE)
def test_python_client_refuses_non_loopback(url):
    with pytest.raises(ValueError):
        Client(url)
    with pytest.raises(ValueError):
        AsyncClient(url)


@pytest.mark.parametrize("url", LOCAL)
def test_python_client_accepts_literal_loopback(url):
    assert Client(url).base == url


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_javascript_client_loopback_rule(tmp_path):
    script = tmp_path / "hosts.mjs"
    script.write_text(f"""
import {{ Client, isLoopbackHost }} from {json.dumps(JS_CLIENT.as_uri())};
const out = {{}};
for (const u of JSON.parse(process.argv[2])) {{
  try {{ new Client(u); out[u] = true; }} catch (e) {{ out[u] = false; }}
}}
out.octet = isLoopbackHost("127.0.0.256");
console.log(JSON.stringify(out));
""", encoding="utf-8")
    urls = REMOTE + LOCAL
    out = subprocess.run(["node", str(script), json.dumps(urls)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert {u: got[u] for u in REMOTE} == dict.fromkeys(REMOTE, False)
    assert {u: got[u] for u in LOCAL} == dict.fromkeys(LOCAL, True)
    assert got["octet"] is False
