// A dependency-free client for a running `ww-scraper serve` (Node 18+ or a browser page on
// the same loopback origin). Mirrors ww_scraper/client.py: retries connection errors, 429
// and 5xx with exponential backoff (Retry-After honoured); other errors throw ApiError.
//
//   import { Client } from "./ww-scraper-client.mjs";
//   const c = new Client("http://127.0.0.1:8765", process.env.WW_SCRAPER_API_TOKEN);
//   const run = await c.call("sitemap", { startUrls: ["https://example.com/"] });
//   const items = await c.items(run.defaultDatasetId);

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"]);

// Same rule as compliance.is_loopback_host in Python: a literal 127.0.0.0/8 address or ::1,
// never a name. "localhost" and names such as 127.0.0.1.nip.io resolve through DNS, so the
// API token could be sent to another host.
export function isLoopbackHost(host) {
  const h = String(host || "").toLowerCase();
  if (h === "[::1]" || h === "::1") return true;
  const m = /^127\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  return m !== null && m.slice(1).every((o) => Number(o) <= 255);
}

// Characters on which URL parsers disagree about the host ("http://evil.com\@127.0.0.1" is
// evil.com to some and 127.0.0.1 to others) are refused outright, as in the Python client.
const BASE_URL_BAD = /[\\@\s\x00-\x1f\x7f]/;

export function checkBaseUrl(baseUrl) {
  const msg = "the ww-scraper API listens on a loopback address only (a literal 127.x.x.x or [::1])";
  if (typeof baseUrl !== "string" || BASE_URL_BAD.test(baseUrl)) {
    throw new Error(msg + "; base URL must not contain '\\', '@', whitespace or control characters");
  }
  let u;
  try { u = new URL(baseUrl); } catch (e) { throw new Error(msg); }
  if ((u.protocol !== "http:" && u.protocol !== "https:") || u.username || u.password || !isLoopbackHost(u.hostname)) {
    throw new Error(msg);
  }
  return baseUrl;
}

export class ApiError extends Error {
  constructor(status, body) {
    const err = (body && body.error) || {};
    super(`HTTP ${status}: ${err.type || ""} ${err.message || ""}`.trim());
    this.status = status;
    this.body = body;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const enc = encodeURIComponent;

export class Client {
  constructor(baseUrl = "http://127.0.0.1:8765", token = "", { retries = 4, backoff = 500, maxBackoff = 30000 } = {}) {
    this.base = checkBaseUrl(baseUrl).replace(/\/+$/, "");
    this.host = new URL(this.base).host;
    this.token = token;
    Object.assign(this, { retries, backoff, maxBackoff });
  }

  async request(method, path, { params, json, body, headers = {}, raw = false } = {}) {
    const url = new URL(this.base + path);
    if (url.host !== this.host) throw new Error("request URL left the API origin");
    for (const [k, v] of Object.entries(params || {})) {
      if (v !== undefined && v !== null) url.searchParams.set(k, typeof v === "boolean" ? (v ? "1" : "0") : String(v));
    }
    const h = { ...headers };
    if (this.token) h.Authorization = "Bearer " + this.token;
    if (json !== undefined) { body = JSON.stringify(json); h["Content-Type"] = "application/json"; }
    for (let attempt = 0; ; attempt++) {
      let res;
      try {
        res = await fetch(url, { method, headers: h, body, redirect: "manual" });
      } catch (e) {
        if (attempt >= this.retries) throw e;
        await sleep(this.delay(attempt, null));
        continue;
      }
      if ((res.status === 429 || res.status >= 500) && attempt < this.retries) {
        await sleep(this.delay(attempt, res.headers.get("Retry-After")));
        continue;
      }
      if (res.status >= 400) {
        let b; try { b = await res.json(); } catch (e) { b = null; }
        throw new ApiError(res.status, b);
      }
      if (raw || res.status === 204) return res;
      return (res.headers.get("Content-Type") || "").startsWith("application/json") ? res.json() : res.text();
    }
  }

  delay(attempt, retryAfter) {
    if (retryAfter && /^\d+$/.test(retryAfter)) return Math.min(this.maxBackoff, Number(retryAfter) * 1000);
    return Math.min(this.maxBackoff, this.backoff * 2 ** attempt);
  }

  async actors() { return (await this.request("GET", "/v2/acts")).data.items; }
  async actor(name) { return (await this.request("GET", "/v2/acts/" + enc(name))).data; }
  async start(actor, input = {}, { wait, ...options } = {}) {
    return (await this.request("POST", "/v2/acts/" + enc(actor) + "/runs", { params: { ...options, waitForFinish: wait }, json: input })).data;
  }
  async run(id) { return (await this.request("GET", "/v2/actor-runs/" + enc(id))).data; }
  async wait(id, { timeout, poll = 1000 } = {}) {
    const end = timeout ? Date.now() + timeout * 1000 : null;
    for (;;) {
      const rec = await this.run(id);
      if (TERMINAL.has(rec.status) || (end && Date.now() >= end)) return rec;
      await sleep(poll);
    }
  }
  async call(actor, input = {}, { timeout, ...options } = {}) {
    const rec = await this.start(actor, input, { ...options, wait: Math.min(timeout || 60, 60) });
    return TERMINAL.has(rec.status) ? rec : this.wait(rec.id, { timeout });
  }
  async callItems(actor, input = {}, params = {}) {
    return this.request("POST", "/v2/acts/" + enc(actor) + "/run-sync-get-dataset-items", { params: { format: "json", ...params }, json: input });
  }
  async abort(id) { return (await this.request("POST", "/v2/actor-runs/" + enc(id) + "/abort", { json: {} })).data; }
  async resurrect(id, options = {}) { return (await this.request("POST", "/v2/actor-runs/" + enc(id) + "/resurrect", { params: options, json: {} })).data; }
  async items(datasetId, params = {}) { return this.request("GET", "/v2/datasets/" + enc(datasetId) + "/items", { params: { format: "json", ...params } }); }
  async storages(kind = "datasets", named) { return (await this.request("GET", "/v2/" + kind, { params: { named } })).data.items; }
  async tasks() { return (await this.request("GET", "/v2/actor-tasks")).data.items; }
  async runTask(name, override = {}, options = {}) {
    return (await this.request("POST", "/v2/actor-tasks/" + enc(name) + "/runs", { params: options, json: override })).data;
  }
  async stats(days = 30, last = 200) { return (await this.request("GET", "/v2/stats/runs", { params: { days, last } })).data; }
}
