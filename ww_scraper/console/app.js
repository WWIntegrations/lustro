// ww-scraper console. Plain DOM, no dependencies, served under script-src 'self'.
// Every value from storage (and so from scraped websites) is written with textContent or
// an attribute set through the DOM; nothing here parses a string as HTML.
"use strict";

const STAT_LABELS = [["items", "items"], ["pages", "pages parsed"], ["requests", "requests"],
  ["duplicates", "duplicate pages skipped"], ["retries", "retries"],
  ["throttled", "slowed down for an overloaded host"], ["robotsBlocked", "refused by robots.txt"],
  ["ssrfBlocked", "refused by SSRF guard"], ["failed", "failed"], ["rendered", "rendered"],
  ["bytes", "bytes read"], ["durationMillis", "ms"]];
const FORMATS = ["json", "jsonl", "csv", "xlsx", "xml", "html"];
const LIVE = new Set(["READY", "RUNNING", "ABORTING", "TIMING-OUT"]);
const MAX_ITEMS = 100, MAX_COLUMNS = 12, MAX_CELL = 180, POLL_MS = 2000;

let token = "";
let timer = null;

// -- token: taken from the URL fragment once (never sent to the server), kept per tab --------
function readToken() {
  const m = location.hash.match(/(^#|&)token=([^&]*)/);
  if (m) {
    token = decodeURIComponent(m[2]);
    try { sessionStorage.setItem("ww-token", token); } catch (e) { /* private mode */ }
    const rest = location.hash.replace(m[0], m[1] === "#" ? "#" : "").replace(/^#&/, "#");
    history.replaceState(null, "", location.pathname + (rest === "#" ? "#/runs" : rest));
    return;
  }
  try { token = sessionStorage.getItem("ww-token") || ""; } catch (e) { token = ""; }
}

// -- DOM ------------------------------------------------------------------------------------
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function view(...children) {
  const app = document.getElementById("app");
  app.replaceChildren(...children.flat().filter(Boolean));
}

function cellText(value) {
  if (value === null || value === undefined) return "";
  let t = typeof value === "object" ? JSON.stringify(value) : String(value);
  return t.length > MAX_CELL ? t.slice(0, MAX_CELL) + " ..." : t;
}

function safeLink(value) {
  // a scraped URL becomes a link only when it is plain http(s); anything else stays text
  if (typeof value !== "string" || value.length > 2048) return null;
  try {
    const u = new URL(value);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return el("a", { href: u.href, target: "_blank", rel: "noopener noreferrer", text: cellText(value) });
  } catch (e) { return null; }
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString();
}

function duration(rec) {
  const ms = rec.stats && rec.stats.durationMillis;
  if (typeof ms === "number") return ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s";
  if (rec.startedAt && LIVE.has(rec.status)) return Math.round((Date.now() - new Date(rec.startedAt)) / 1000) + " s";
  return "";
}

function pill(status) {
  return el("span", { class: "pill " + String(status || ""), text: String(status || "") });
}

// -- API ------------------------------------------------------------------------------------
class AuthError extends Error {}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (token) headers.Authorization = "Bearer " + token;
  const res = await fetch(path, Object.assign({}, opts, { headers, cache: "no-store" }));
  if (res.status === 401) throw new AuthError("token");
  return res;
}

async function apiJson(path, opts) {
  const res = await api(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = new Error((body.error && body.error.message) || res.statusText);
    e.body = body;
    e.status = res.status;
    throw e;
  }
  return body.data;
}

// -- views ----------------------------------------------------------------------------------
function tokenForm(message) {
  const input = el("input", { type: "text", id: "tok", autocomplete: "off", spellcheck: "false",
    "aria-describedby": "tok-hint" });
  const save = (ev) => {
    ev.preventDefault();
    token = input.value.trim();
    try { sessionStorage.setItem("ww-token", token); } catch (e) { /* private mode */ }
    route();
  };
  view(
    el("h1", { text: "Sign in to your local API" }),
    el("p", { class: "sub", text: message || "Paste the Bearer token that `ww-scraper serve` printed." }),
    el("form", { class: "run", onsubmit: save },
      el("div", { class: "field" },
        el("label", { for: "tok", text: "Bearer token" }),
        el("p", { class: "hint", id: "tok-hint",
          text: "Kept in this tab only (sessionStorage) and sent as an Authorization header to 127.0.0.1." }),
        input),
      el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit", text: "Open console" }))));
  input.focus();
}

async function runsView() {
  const runs = (await apiJson("/v2/actor-runs")).items || [];
  runs.sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  const rows = runs.map((r) => el("tr", { class: "link", tabindex: "0",
      onclick: () => { location.hash = "#/runs/" + encodeURIComponent(r.id); },
      onkeydown: (e) => { if (e.key === "Enter") location.hash = "#/runs/" + encodeURIComponent(r.id); } },
    el("td", {}, pill(r.status)),
    el("td", {}, el("code", { text: r.actId || "" })),
    el("td", { class: "mono", text: r.id }),
    el("td", { text: fmtTime(r.createdAt) }),
    el("td", { class: "num", text: duration(r) }),
    el("td", { class: "num", text: String(r.itemCount ?? "") })));
  view(
    el("div", { class: "row" },
      el("div", {}, el("h1", { text: "Runs" }),
        el("p", { class: "sub", text: runs.length + " run" + (runs.length === 1 ? "" : "s") + " in this storage directory" })),
      el("a", { class: "btn primary push", href: "#/actors", text: "Start a run" })),
    runs.length ? el("div", { class: "wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Status", "Actor", "Run", "Created", "Duration", "Items"].map(
        (h, i) => el("th", { class: i >= 4 ? "num" : null, text: h })))),
      el("tbody", {}, rows)))
      : el("p", { class: "note", text: "No runs yet. Pick an actor to start one." }));
  if (runs.some((r) => LIVE.has(r.status))) poll(runsView);
}

async function actorsView() {
  const acts = (await apiJson("/v2/acts")).items || [];
  view(
    el("h1", { text: "Actors" }),
    el("p", { class: "sub", text: "Each one obeys robots.txt and paces itself per host. Pick one to fill in its input." }),
    el("div", { class: "cards" }, acts.map((a) => el("a", { class: "card", href: "#/actors/" + encodeURIComponent(a.name) },
      el("b", { text: a.title || a.name }), el("code", { text: a.name }), el("p", { text: a.description || "" })))));
}

function fieldFor(key, spec, required) {
  const id = "f-" + key;
  const type = spec.type;
  const itemType = (spec.items || {}).type;
  const title = spec.title || key;
  const hint = spec.description ? el("p", { class: "hint", text: spec.description }) : null;
  let input, read;
  if (type === "boolean") {
    input = el("input", { type: "checkbox", id, checked: spec.default === true });
    read = () => input.checked;
    return { node: el("div", { class: "field check" }, el("label", { for: id }, input, title), hint), read, key };
  }
  if (type === "string" && Array.isArray(spec.enum)) {
    input = el("select", { id }, spec.enum.map((v) => el("option", { value: String(v), text: String(v),
      selected: v === spec.default })));
    read = () => input.value;
  } else if (type === "integer" || type === "number") {
    input = el("input", { type: "number", id, min: spec.minimum, max: spec.maximum,
      step: type === "integer" ? "1" : "any", placeholder: spec.default ?? "" });
    read = () => input.value.trim() === "" ? undefined : Number(input.value);
  } else if (type === "array" && (!itemType || itemType === "string")) {
    input = el("textarea", { id, spellcheck: "false", placeholder: "one per line" });
    if (Array.isArray(spec.default)) input.value = spec.default.join("\n");
    read = () => { const v = input.value.split("\n").map((s) => s.trim()).filter(Boolean); return v.length ? v : undefined; };
  } else if (type === "string") {
    input = el("input", { type: "text", id, spellcheck: "false", placeholder: spec.default ?? "" });
    read = () => input.value === "" ? undefined : input.value;
  } else {
    input = el("textarea", { id, spellcheck: "false", placeholder: "JSON" });
    if (spec.default !== undefined) input.value = JSON.stringify(spec.default, null, 2);
    read = () => {
      if (input.value.trim() === "") return undefined;
      try { return JSON.parse(input.value); } catch (e) { throw new Error(key + ": not valid JSON"); }
    };
  }
  if (required) input.setAttribute("aria-required", "true");
  return { node: el("div", { class: "field" },
    el("label", { for: id }, title, required ? el("span", { class: "req", text: " *" }) : null), hint, input), read, key };
}

async function actorView(name) {
  const acts = (await apiJson("/v2/acts")).items || [];
  const a = acts.find((x) => x.name === name);
  if (!a) return view(el("h1", { text: "Actor not found" }), el("a", { href: "#/actors", text: "All actors" }));
  const schema = a.inputSchema || { properties: {} };
  const required = new Set(schema.required || []);
  const fields = Object.entries(schema.properties || {}).map(([k, s]) => fieldFor(k, s, required.has(k)));
  const errBox = el("div", { role: "alert" });
  const submit = el("button", { class: "btn primary", type: "submit", text: "Start run" });
  const onsubmit = async (ev) => {
    ev.preventDefault();
    errBox.replaceChildren();
    const input = {};
    try {
      for (const f of fields) { const v = f.read(); if (v !== undefined) input[f.key] = v; }
    } catch (e) {
      errBox.replaceChildren(el("div", { class: "err", text: e.message }));
      return;
    }
    submit.disabled = true;
    try {
      const rec = await apiJson("/v2/acts/" + encodeURIComponent(name) + "/runs", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
      location.hash = "#/runs/" + encodeURIComponent(rec.id);
    } catch (e) {
      if (e instanceof AuthError) throw e;
      const list = (e.body && e.body.error && e.body.error.errors) || [];
      errBox.replaceChildren(el("div", { class: "err" }, el("b", { text: "The input was refused" }),
        list.length ? el("ul", {}, list.map((m) => el("li", { text: m }))) : el("p", { text: e.message })));
      submit.disabled = false;
    }
  };
  view(
    el("p", {}, el("a", { href: "#/actors", text: "All actors" })),
    el("h1", { text: a.title || a.name }),
    el("p", { class: "sub", text: a.description || "" }),
    el("form", { class: "run", onsubmit }, fields.map((f) => f.node), errBox, el("div", { class: "row" }, submit)));
  const first = document.querySelector("form.run input, form.run textarea, form.run select");
  if (first) first.focus();
}

async function download(rec, fmt) {
  const res = await api("/v2/datasets/" + encodeURIComponent(rec.defaultDatasetId || rec.id) + "/items?format=" + fmt);
  if (!res.ok) return;
  const url = URL.createObjectURL(await res.blob());
  const a = el("a", { href: url, download: (rec.defaultDatasetId || rec.id) + "." + fmt });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function itemsTable(items) {
  const cols = [];
  for (const it of items) {
    for (const k of Object.keys(it || {})) if (!cols.includes(k) && cols.length < MAX_COLUMNS) cols.push(k);
  }
  return el("div", { class: "wrap" }, el("table", { id: "items" },
    el("thead", {}, el("tr", {}, cols.map((c) => el("th", { text: c })))),
    el("tbody", {}, items.map((it) => el("tr", {}, cols.map((c) => {
      const v = it ? it[c] : undefined;
      return el("td", {}, safeLink(v) || cellText(v));
    }))))));
}

async function runView(id) {
  const rec = await apiJson("/v2/actor-runs/" + encodeURIComponent(id));
  const ds = encodeURIComponent(rec.defaultDatasetId || rec.id);
  const [items, logText, input] = await Promise.all([
    api("/v2/datasets/" + ds + "/items?format=json&limit=" + MAX_ITEMS)
      .then((r) => (r.ok ? r.json() : [])).catch(() => []),
    api("/v2/actor-runs/" + encodeURIComponent(id) + "/log").then((r) => (r.ok ? r.text() : "")),
    api("/v2/key-value-stores/" + encodeURIComponent(rec.defaultKeyValueStoreId || rec.id) + "/records/INPUT")
      .then((r) => (r.ok ? r.text() : "")),
  ]);
  const stats = rec.stats || {};
  const live = LIVE.has(rec.status);
  const abort = live ? el("button", { class: "btn danger", type: "button", text: "Abort run",
    onclick: async (ev) => {
      ev.target.disabled = true;
      await api("/v2/actor-runs/" + encodeURIComponent(id) + "/abort", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: "{}" });
      route();
    } }) : el("button", { class: "btn", type: "button", id: "resurrect", text: "Run again",
    title: "Run this finished run again with the same input and storages",
    onclick: async (ev) => {
      ev.target.disabled = true;
      await api("/v2/actor-runs/" + encodeURIComponent(id) + "/resurrect", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: "{}" });
      route();
    } });
  const list = Array.isArray(items) ? items : [];
  let inputPretty = input;
  try { inputPretty = JSON.stringify(JSON.parse(input), null, 2); } catch (e) { /* keep text */ }
  view(
    el("p", {}, el("a", { href: "#/runs", text: "All runs" })),
    el("div", { class: "row" }, el("h1", {}, el("code", { text: rec.actId || "" })), pill(rec.status), abort),
    el("p", { class: "sub" }, "Run ", el("code", { text: rec.id }), " · created " + fmtTime(rec.createdAt),
      rec.finishedAt ? " · finished " + fmtTime(rec.finishedAt) : ""),
    rec.statusMessage ? el("p", { class: "status-message", id: "status-message", text: rec.statusMessage }) : null,
    el("div", { class: "grid", id: "stats" },
      live ? el("div", { class: "tile" }, el("b", { text: String(rec.itemCount ?? 0) }), el("span", { text: "items so far" })) : null,
      STAT_LABELS.filter(([k]) => k in stats).map(([k, label]) =>
        el("div", { class: "tile" }, el("b", { text: Number(stats[k]).toLocaleString() }), el("span", { text: label })))),
    el("h2", { text: "Items (" + list.length + (rec.itemCount > list.length ? " of " + rec.itemCount : "") + ")" }),
    el("div", { class: "row gap-below" }, el("span", { class: "muted", text: "Download" }),
      FORMATS.map((f) => el("button", { class: "btn", type: "button", text: f.toUpperCase(),
        onclick: () => download(rec, f) }))),
    list.length ? itemsTable(list) : el("p", { class: "note", text: live ? "No items yet." : "This run produced no items." }),
    el("h2", { text: "Log" }),
    el("pre", { class: "log", id: "log", text: logText || "(empty)" }),
    el("h2", { text: "Input" }),
    el("pre", { class: "log", text: inputPretty || "{}" }));
  const log = document.getElementById("log");
  log.scrollTop = log.scrollHeight;
  if (live) poll(() => runView(id));
}

// -- router ---------------------------------------------------------------------------------
function poll(fn) {
  clearTimeout(timer);
  timer = setTimeout(() => { if (!document.hidden) guarded(fn); else poll(fn); }, POLL_MS);
}

async function guarded(fn) {
  try {
    await fn();
  } catch (e) {
    clearTimeout(timer);
    if (e instanceof AuthError) return tokenForm(token ? "That token was refused. Paste the current one." : null);
    view(el("h1", { text: "Something went wrong" }), el("p", { class: "err", text: e.message || String(e) }));
  }
}

function route() {
  clearTimeout(timer);
  const parts = (location.hash.replace(/^#\/?/, "") || "runs").split("/").map(decodeURIComponent);
  for (const a of document.querySelectorAll("[data-nav]")) {
    if (a.dataset.nav === parts[0]) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  // no token yet: try anyway (the server may run with --no-auth); a 401 shows the form
  if (parts[0] === "actors" && parts[1]) return guarded(() => actorView(parts[1]));
  if (parts[0] === "actors") return guarded(actorsView);
  if (parts[0] === "runs" && parts[1]) return guarded(() => runView(parts[1]));
  return guarded(runsView);
}

readToken();
window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", route);
