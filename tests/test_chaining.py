"""ctx.call_actor(), the lead-list actor and the pipeline actor - offline, against local
fixture servers only (an Overpass imitation and two company websites)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from ww_scraper.actor import Actor, execute_run, prepare_run, request_abort, run_actor
from ww_scraper.actors import ACTORS

FIRM = {
    "/firm/": """<html><head><script type="application/ld+json">{"@type":"LocalBusiness","name":"Firm SA",
      "telephone":"+48 58 000 11 22","vatID":"PL5840000000","sameAs":["https://www.linkedin.com/company/firm-sa"]}
      </script></head><body><main><a href="/firm/kontakt">Kontakt</a><a href="/firm/zespol">Zespół</a></main>
      <footer>Napisz: biuro@firm.example</footer></body></html>""",
    "/firm/kontakt": """<html><body><main><a href="mailto:sprzedaz@firm.example">sprzedaz</a></main></body></html>""",
    "/firm/zespol": """<html><body><main>Jan Kowalski, jan.kowalski@firm.example, tel. 601 234 567</main></body></html>""",
    "/quiet/": "<html><body><main>We are a quiet bakery.</main></body></html>",
}


class _Server:
    def __init__(self):
        srv = self
        self.overpass_calls = 0

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/robots.txt":
                    body, ctype, code = "User-agent: *\nAllow: /\n", "text/plain", 200
                elif path == "/api/interpreter":
                    srv.overpass_calls += 1
                    body, ctype, code = json.dumps(srv.overpass()), "application/json", 200
                elif path in FIRM:
                    body, ctype, code = FIRM[path], "text/html", 200
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

    def overpass(self):
        return {"elements": [
            {"type": "node", "id": 1, "lat": 54.35, "lon": 18.65,
             "tags": {"amenity": "cafe", "name": "Kawiarnia Firm", "addr:street": "Długa", "addr:housenumber": "1",
                      "addr:postcode": "80-001", "addr:city": "Gdańsk", "website": self.base + "/firm/",
                      "phone": "+48 58 123 45 67", "opening_hours": "Mo-Fr 08:00-18:00"}},
            {"type": "node", "id": 2, "lat": 54.36, "lon": 18.64,
             "tags": {"amenity": "cafe", "name": "Quiet Bakery", "website": self.base + "/quiet"}},
            {"type": "node", "id": 3, "lat": 54.37, "lon": 18.63, "tags": {"amenity": "cafe", "name": "No Site"}},
        ]}


@pytest.fixture(scope="module")
def srv():
    s = _Server()
    yield s
    s.httpd.shutdown()


def _lead_run(storage, srv, **extra):
    inp = dict({"category": "cafe", "city": "Gdańsk", "endpoint": srv.base + "/api/interpreter",
                "minIntervalSecs": 0}, **extra)
    rec = run_actor("lead-list", inp, storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec.get("statusMessage")
    return rec, storage.dataset(rec["id"]).items(), storage.kv(rec["id"]).get("OUTPUT")


def test_lead_list_joins_places_and_contacts_with_masking(storage, srv):
    rec, leads, out = _lead_run(storage, srv)
    assert [x["name"] for x in leads] == ["Kawiarnia Firm", "Quiet Bakery"]     # no website: left out
    a = leads[0]
    assert a["category"] == "amenity=cafe" and a["address"] == "Długa 1, 80-001 Gdańsk"
    assert a["website"] == srv.base + "/firm/"
    assert set(a["emails"]) == {"biuro@firm.example", "sprzedaz@firm.example"}
    assert "jan.kowalski@firm.example" not in json.dumps(leads)                  # personal: masked
    assert a["maskedPersonalEmails"] == 1 and a["maskedTextPhones"] == 1
    assert a["phones"] == ["+48580001122"]                    # OSM phone masked by the default policy
    assert a["socialProfiles"] == {"linkedin": "https://linkedin.com/company/firm-sa"}
    assert a["source"]["osmUrl"] == "https://www.openstreetmap.org/node/1" and "ODbL" in a["source"]["license"]
    assert any(u.endswith("/firm/kontakt") for u in a["source"]["pagesRead"])
    assert leads[1]["emails"] == [] and leads[1]["phones"] == []
    assert out["leads"] == 2 and out["withEmail"] == 1
    # the called runs are ordinary runs with their own storages, linked to the lead-list run
    children = [storage.load_run(i) for i in rec["childRunIds"]]
    assert [c["actId"] for c in children] == ["places", "contact-details"]
    assert all(c["parentRunId"] == rec["id"] and c["status"] == "SUCCEEDED" for c in children)
    assert rec["stats"]["calledRuns"] == 2 and rec["statusMessage"] == "2 leads, 1 with an e-mail"


def test_lead_list_without_redaction_and_with_places_lacking_a_site(storage, srv):
    _, leads, _ = _lead_run(storage, srv, redactPii=False, onlyWithWebsite=False)
    assert [x["name"] for x in leads] == ["Kawiarnia Firm", "Quiet Bakery", "No Site"]
    a = leads[0]
    assert "jan.kowalski@firm.example" in a["emails"] and "+48 58 123 45 67" in a["phones"]
    assert "maskedPersonalEmails" not in a and leads[2]["website"] is None


class Caller(Actor):
    name = "test-caller"
    input_schema = {"type": "object", "properties": {"childTimeout": {"type": "number", "default": 30}}}

    def run(self, ctx):
        child = ctx.call_actor("test-sleeper", {}, timeout_secs=ctx.input["childTimeout"])
        ctx.push_data({"child": child["id"], "childStatus": child["status"]})


class Sleeper(Actor):
    name = "test-sleeper"

    def run(self, ctx):
        for i in range(500):
            ctx.check_abort()
            ctx.push_data({"n": i})
            time.sleep(0.01)


class Recursive(Actor):
    name = "test-recursive"

    def run(self, ctx):
        child = ctx.call_actor("test-recursive", {})
        if child["status"] != "SUCCEEDED":
            raise RuntimeError(f"child {child['status']}: {child['statusMessage']}")


@pytest.fixture
def test_actors(monkeypatch):
    for a in (Caller(), Sleeper(), Recursive()):
        monkeypatch.setitem(ACTORS, a.name, a)


def test_called_run_respects_the_callers_timeout(storage, test_actors):
    rec = run_actor("test-caller", {"childTimeout": 60}, storage, timeout_secs=0.3)
    assert rec["status"] == "TIMED-OUT"
    child = storage.load_run(rec["childRunIds"][0])
    assert child["status"] == "TIMED-OUT" and child["parentRunId"] == rec["id"]
    assert 0 < child["itemCount"] < 500                                   # partial items kept
    assert storage.dataset(rec["id"]).count() == 0                        # the caller stopped too


def test_called_run_with_a_shorter_timeout_returns_to_the_caller(storage, test_actors):
    rec = run_actor("test-caller", {"childTimeout": 0.2}, storage, timeout_secs=30)
    assert rec["status"] == "SUCCEEDED"
    assert storage.dataset(rec["id"]).items()[0]["childStatus"] == "TIMED-OUT"


def test_aborting_the_caller_stops_the_called_run(storage, test_actors):
    rec = prepare_run("test-caller", {}, storage)
    t = threading.Thread(target=execute_run, args=(rec, storage))
    t.start()
    for _ in range(300):
        if (storage.load_run(rec["id"]) or {}).get("childRunIds"):
            break
        time.sleep(0.01)
    request_abort(rec["id"])
    t.join(10)
    assert storage.load_run(rec["id"])["status"] == "ABORTED"
    assert storage.load_run(storage.load_run(rec["id"])["childRunIds"][0])["status"] == "ABORTED"


def test_call_depth_is_limited(storage, test_actors):
    rec = run_actor("test-recursive", {}, storage)
    assert rec["status"] == "FAILED"
    runs = storage.list_runs()
    assert len(runs) == 5
    assert any("nesting deeper than 4" in (r.get("statusMessage") or "") for r in runs)


def test_pipeline_feeds_sitemap_urls_into_seo_audit(storage, site):
    rec = run_actor("pipeline", {"steps": [
        {"actor": "sitemap", "input": {"startUrls": [site.base], "minIntervalSecs": 0}},
        {"actor": "seo-audit", "input": {"minIntervalSecs": 0, "maxPages": 5}, "urlsFrom": "url", "maxUrls": 2},
    ]}, storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec["statusMessage"]
    steps = storage.kv(rec["id"]).get("OUTPUT")["steps"]
    assert [s["actor"] for s in steps] == ["sitemap", "seo-audit"] and all(s["status"] == "SUCCEEDED" for s in steps)
    audit = storage.load_run(steps[1]["runId"])
    assert len(storage.kv(audit["id"]).get("INPUT")["startUrls"]) == 2
    assert rec["itemCount"] == audit["itemCount"] > 0


def test_pipeline_rejects_unknown_steps_and_stops_on_failure(storage):
    rec = run_actor("pipeline", {"steps": [{"actor": "nope"}]}, storage)
    assert rec["status"] == "FAILED" and "unknown actor" in rec["statusMessage"]
    rec = run_actor("pipeline", {"steps": [{"actor": "sitemap", "input": {}}]}, storage)
    assert rec["status"] == "FAILED" and "steps[0].input: startUrls: field is required" in rec["statusMessage"]
    assert len(storage.list_runs()) == 2                          # no step was started
    rec = run_actor("pipeline", {"steps": [{"actor": "places", "input": {"category": "cafe"}},
                                           {"actor": "sitemap", "urlsFrom": "website"}]}, storage)
    assert rec["status"] == "FAILED" and "step 1 (places) ended FAILED" in rec["statusMessage"]
