"""Local API, signed webhooks, schedule printer and CLI - offline."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from lustro import webhooks
from lustro.api import LustroAPI
from lustro.schedule import cron_line, launchd_plist, render


@pytest.fixture
def api(storage):
    a = LustroAPI(storage, port=0, token="s3cret", allow_loopback=True, lang="pl")
    a.start()
    yield a
    a.shutdown()


def _h(token="s3cret"):
    return {"Authorization": f"Bearer {token}"}


def test_api_binds_loopback_only(storage):
    with pytest.raises(ValueError):
        LustroAPI(storage, host="0.0.0.0", port=0)


def test_api_auth_and_host_guard(api):
    assert requests.get(api.url + "/v2/acts", timeout=5).status_code == 401
    assert requests.get(api.url + "/v2/acts", headers=_h("wrong"), timeout=5).status_code == 401
    r = requests.get(api.url + "/v2/acts", headers=dict(_h(), Host="evil.example"), timeout=5)
    assert r.status_code == 403                       # DNS-rebinding guard
    r = requests.get(api.url + "/v2/acts", headers=_h(), timeout=5)
    assert r.status_code == 200 and r.json()["data"]["total"] == 7


def test_api_run_flow(api, site):
    inp = {"startUrls": [site.base + "/"], "maxPages": 3, "minIntervalSecs": 0}
    r = requests.post(api.url + "/v2/acts/website-content-crawler/runs?waitForFinish=30",
                      json=inp, headers=_h(), timeout=40)
    assert r.status_code == 201
    run = r.json()["data"]
    assert run["status"] == "SUCCEEDED" and run["itemCount"] >= 1
    r = requests.get(f"{api.url}/v2/actor-runs/{run['id']}", headers=_h(), timeout=5)
    assert r.json()["data"]["id"] == run["id"]
    ds = run["defaultDatasetId"]
    r = requests.get(f"{api.url}/v2/datasets/{ds}/items?format=csv", headers=_h(), timeout=5)
    assert r.headers["Content-Type"].startswith("text/csv") and r.text.startswith("url,")
    r = requests.get(f"{api.url}/v2/datasets/{ds}/items?format=jsonl&limit=1", headers=_h(), timeout=5)
    assert len(r.text.strip().split("\n")) == 1
    r = requests.get(f"{api.url}/v2/key-value-stores/{run['defaultKeyValueStoreId']}/records/INPUT",
                     headers=_h(), timeout=5)
    assert r.json()["maxPages"] == 3 and r.headers["Content-Security-Policy"] == "sandbox"
    assert requests.get(f"{api.url}/v2/datasets/..%2F..%2Fetc/items", headers=_h(),
                        timeout=5).status_code == 404
    assert requests.get(f"{api.url}/v2/actor-runs", headers=_h(), timeout=5).json()["data"]["total"] == 1


def test_api_input_errors_in_polish(api):
    r = requests.post(api.url + "/v2/acts/sitemap/runs", json={"maxSitemaps": 0},
                      headers=_h(), timeout=5)
    assert r.status_code == 400
    errs = r.json()["error"]["errors"]
    assert "startUrls: pole jest wymagane" in errs and "maxSitemaps: wartość musi być >= 1" in errs
    assert requests.post(api.url + "/v2/acts/nope/runs", json={}, headers=_h(),
                         timeout=5).status_code == 404
    r = requests.post(api.url + "/v2/acts/sitemap/runs", data=b"{bad",
                      headers=dict(_h(), **{"Content-Type": "application/json"}), timeout=5)
    assert r.status_code == 400


def test_api_token_required_by_default(storage, monkeypatch):
    monkeypatch.delenv("LUSTRO_API_TOKEN", raising=False)
    a = LustroAPI(storage, port=0)
    a.start()
    try:
        assert a.token_generated and len(a.token) >= 32
        assert requests.get(a.url + "/v2/acts", timeout=5).status_code == 401
        assert requests.get(a.url + "/v2/acts", headers=_h(a.token), timeout=5).status_code == 200
    finally:
        a.shutdown()
    b = LustroAPI(storage, port=0, no_auth=True)
    b.start()
    try:
        assert requests.get(b.url + "/v2/acts", timeout=5).status_code == 200
    finally:
        b.shutdown()


def test_api_requires_json_and_refuses_foreign_origin(api):
    r = requests.post(api.url + "/v2/acts/sitemap/runs", data=b"{}",
                      headers=dict(_h(), **{"Content-Type": "text/plain"}), timeout=5)
    assert r.status_code == 415
    r = requests.post(api.url + "/v2/acts/sitemap/runs", data=b"{}", headers=_h(), timeout=5)
    assert r.status_code == 415                       # no Content-Type at all
    r = requests.get(api.url + "/v2/acts", headers=dict(_h(), Origin="https://evil.example"), timeout=5)
    assert r.status_code == 403 and r.json()["error"]["type"] == "forbidden-origin"
    r = requests.get(api.url + "/v2/acts", headers=dict(_h(), Origin="null"), timeout=5)
    assert r.status_code == 403
    r = requests.get(api.url + "/v2/acts", headers=dict(_h(), Origin=api.url), timeout=5)
    assert r.status_code == 200


# --- webhooks --------------------------------------------------------------------------

class _Receiver(BaseHTTPRequestHandler):
    got = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.got.append((dict(self.headers), body))
        self.send_response(200)
        self.end_headers()


def test_webhook_signed_and_allowlisted(storage, monkeypatch, api, site):
    srv = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hook = f"http://127.0.0.1:{srv.server_address[1]}/hook"
    monkeypatch.setenv("LUSTRO_WEBHOOK_HOSTS", "127.0.0.1")
    monkeypatch.setenv("LUSTRO_WEBHOOK_SECRET", "whsec")
    _Receiver.got = []
    wh = base64.b64encode(json.dumps([{"requestUrl": hook,
                                       "eventTypes": ["ACTOR.RUN.SUCCEEDED"]}]).encode()).decode()
    r = requests.post(f"{api.url}/v2/acts/sitemap/runs?waitForFinish=30&webhooks={wh}",
                      json={"startUrls": [site.base], "minIntervalSecs": 0}, headers=_h(), timeout=40)
    assert r.json()["data"]["status"] == "SUCCEEDED"
    for _ in range(50):
        if _Receiver.got:
            break
        threading.Event().wait(0.05)
    headers, body = _Receiver.got[0]
    assert webhooks.verify("whsec", headers["X-Lustro-Timestamp"], body, headers["X-Lustro-Signature"])
    assert not webhooks.verify("other", headers["X-Lustro-Timestamp"], body, headers["X-Lustro-Signature"])
    payload = json.loads(body)
    assert payload["eventType"] == "ACTOR.RUN.SUCCEEDED" and payload["resource"]["actId"] == "sitemap"
    srv.shutdown()


def test_webhook_refusals():
    p = {"x": 1}
    assert webhooks.send("https://hooks.ex.com/a", p, secret="", allow_hosts={"hooks.ex.com"})[1].startswith("refused: LUSTRO_WEBHOOK_SECRET")
    assert "allowlist" in webhooks.send("https://evil.com/", p, secret="s", allow_hosts={"hooks.ex.com"})[1]
    assert "allowlist" in webhooks.send("https://evil.com/", p, secret="s", allow_hosts=set())[1]
    assert "https" in webhooks.send("http://hooks.ex.com/", p, secret="s", allow_hosts={"hooks.ex.com"})[1]
    # loopback needs the test switch, even when allowlisted
    assert webhooks.send("http://127.0.0.1:9/", p, secret="s", allow_hosts={"127.0.0.1"})[0] is False
    assert webhooks.dispatch({"status": "RUNNING"}, ["https://x"], allow_hosts=set(), secret="s") == []


# --- schedule + CLI --------------------------------------------------------------------

def test_schedule_prints_cron_and_plist():
    line = cron_line("sitemap", '{"startUrls":["https://ex.com"]}', "0 6 * * 1", "/srv/l", "/usr/bin/python3")
    assert line.startswith("0 6 * * 1 cd /srv/l && /usr/bin/python3 -m lustro run sitemap --input ")
    assert "'{\"startUrls\":[\"https://ex.com\"]}'" in line
    pl = launchd_plist("sitemap", '{"a":"<&>"}', "30 6 * * 1,3", "/srv/l", "/usr/bin/python3")
    flat = "".join(pl.split())
    assert ("<key>StartCalendarInterval</key><array><dict><key>Minute</key><integer>30</integer>"
            "<key>Hour</key><integer>6</integer><key>Weekday</key><integer>1</integer></dict>") in flat
    assert "<key>Weekday</key><integer>3</integer>" in flat
    assert "<string>/usr/bin/python3</string><string>-m</string><string>lustro</string>" in flat
    assert "<string>{\"a\":\"&lt;&amp;&gt;\"}</string>" in flat
    assert "<key>StartInterval</key><integer>900</integer>" in "".join(
        launchd_plist("sitemap", "{}", "*/15 * * * *", "/srv/l").split())
    out = render("sitemap", "{}", "0-5 * * * *", "/srv/l")
    assert "# launchd:" in out and "Nothing was installed" in out
    with pytest.raises(ValueError):
        cron_line("sitemap", "{}", "bad", "/srv/l")


def test_cli_run_actors_and_export(site, tmp_path, capsys):
    from lustro.__main__ import main
    st = str(tmp_path / "st")
    assert main(["actors"]) == 0
    assert "website-content-crawler" in capsys.readouterr().out
    rc = main(["run", "sitemap", "--input", json.dumps({"startUrls": [site.base], "minIntervalSecs": 0}),
               "--storage", st, "--allow-loopback", "--quiet"])
    out = capsys.readouterr().out
    assert rc == 0
    rec = json.loads(out)
    assert rec["status"] == "SUCCEEDED" and rec["itemCount"] == 3
    assert main(["export", rec["defaultDatasetId"], "--format", "csv", "--storage", st]) == 0
    assert capsys.readouterr().out.startswith("url,")
    assert main(["run", "sitemap", "--input", "{}", "--storage", st, "--lang", "pl"]) == 2
    assert "startUrls: pole jest wymagane" in capsys.readouterr().err
    assert main(["run", "sitemap", "--input", "{oops", "--storage", st]) == 2
    assert main(["schedule", "sitemap", "--cron", "0 6 * * *", "--input", "{}"]) == 0
    assert "Nothing was installed" in capsys.readouterr().out
