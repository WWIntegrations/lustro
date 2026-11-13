// ww-scraper console. Plain DOM, no dependencies, served under script-src 'self'.
// Every value from storage (and so from scraped websites) is written with textContent or
// an attribute set through the DOM; nothing here parses a string as HTML.
"use strict";

const STAT_LABELS = [["items", "items"], ["pages", "pages parsed"], ["requests", "requests"],
  ["duplicates", "duplicate pages skipped"], ["retries", "retries"],
  ["throttled", "slowed down for an overloaded host"], ["robotsBlocked", "refused by robots.txt"],
  ["ssrfBlocked", "refused by SSRF guard"], ["failed", "failed"], ["rendered", "rendered"],
  ["bytes", "bytes read"], ["durationMillis", "ms"]];
const FORMATS = ["json", "jsonl", "csv", "xlsx", "xml", "html", "rss"];
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

function initial(spec) {
  // prefill is shown in the form but, unlike default, is not applied when the field is left out
  return spec.prefill !== undefined ? spec.prefill : spec.default;
}

function fieldFor(key, spec, required) {
  const id = "f-" + key;
  const type = spec.type;
  const editor = spec.editor || "";
  const itemType = (spec.items || {}).type;
  const title = spec.title || key;
  const hint = spec.description ? el("p", { class: "hint", text: spec.description }) : null;
  const start = initial(spec);
  let input, read;
  if (type === "boolean") {
    input = el("input", { type: "checkbox", id, checked: start === true });
    read = () => input.checked;
    return { node: el("div", { class: "field check" }, el("label", { for: id }, input, title), hint), read, key };
  }
  if (type === "string" && Array.isArray(spec.enum)) {
    input = el("select", { id }, spec.enum.map((v) => el("option", { value: String(v), text: String(v),
      selected: v === start })));
    read = () => input.value;
  } else if (type === "integer" || type === "number") {
    input = el("input", { type: "number", id, min: spec.minimum, max: spec.maximum,
      step: type === "integer" ? "1" : "any", placeholder: spec.default ?? "" });
    if (spec.prefill !== undefined) input.value = String(spec.prefill);
    read = () => input.value.trim() === "" ? undefined : Number(input.value);
  } else if (type === "array" && (!itemType || itemType === "string") && editor !== "json") {
    input = el("textarea", { id, spellcheck: "false", placeholder: "one per line" });
    if (Array.isArray(start)) input.value = start.join("\n");
    read = () => { const v = input.value.split("\n").map((s) => s.trim()).filter(Boolean); return v.length ? v : undefined; };
  } else if (type === "string" && editor !== "json") {
    const kind = spec.secret ? "password" : editor === "datepicker" ? "date" : "text";
    input = editor === "textarea"
      ? el("textarea", { id, spellcheck: "false", placeholder: spec.default ?? "" })
      : el("input", { type: kind, id, spellcheck: "false", autocomplete: spec.secret ? "off" : null,
        placeholder: spec.secret ? "kept in memory only, never stored" : (spec.default ?? "") });
    if (spec.pattern && input.tagName === "INPUT") input.setAttribute("pattern", spec.pattern);
    if (!spec.secret && spec.prefill !== undefined) input.value = String(spec.prefill);
    read = () => input.value === "" ? undefined : input.value;
  } else {
    input = el("textarea", { id, spellcheck: "false", placeholder: "JSON" });
    if (start !== undefined) input.value = JSON.stringify(start, null, 2);
    read = () => {
      if (input.value.trim() === "") return undefined;
      try { return JSON.parse(input.value); } catch (e) { throw new Error(key + ": not valid JSON"); }
    };
  }
  if (required) input.setAttribute("aria-required", "true");
  return { node: el("div", { class: "field" },
    el("label", { for: id }, title, required ? el("span", { class: "req", text: " *" }) : null), hint, input), read, key };
}

function formNodes(fields, specs) {
  // a field with sectionCaption starts a new group that runs until the next one
  const out = [];
  let group = null;
  fields.forEach((f) => {
    const spec = specs[f.key] || {};
    if (spec.sectionCaption) {
      group = el("fieldset", { class: "section" }, el("legend", { text: spec.sectionCaption }),
        spec.sectionDescription ? el("p", { class: "hint", text: spec.sectionDescription }) : null);
      out.push(group);
    }
    (group || { append: (n) => out.push(n) }).append(f.node);
  });
  return out;
}

async function actorView(name) {
  const acts = (await apiJson("/v2/acts")).items || [];
  const a = acts.find((x) => x.name === name);
  if (!a) return view(el("h1", { text: "Actor not found" }), el("a", { href: "#/actors", text: "All actors" }));
  const schema = a.inputSchema || { properties: {} };
  const required = new Set(schema.required || []);
  const specs = schema.properties || {};
  const fields = Object.entries(specs).filter(([, s]) => s.editor !== "hidden")
    .map(([k, s]) => fieldFor(k, s, required.has(k)));
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
    el("form", { class: "run", onsubmit }, formNodes(fields, specs), errBox, el("div", { class: "row" }, submit)));
  const first = document.querySelector("form.run input, form.run textarea, form.run select");
  if (first) first.focus();
}

async function downloadPath(path, filename) {
  // only paths on this API: the output schema of an actor can never send the token elsewhere
  if (typeof path !== "string" || !path.startsWith("/v2/")) return;
  const res = await api(path);
  if (!res.ok) return;
  const url = URL.createObjectURL(await res.blob());
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function outputsList(rec) {
  const outs = Object.entries(rec.output || {});
  if (!outs.length) return null;
  return el("ul", { class: "outputs", id: "outputs" }, outs.map(([name, o]) => el("li", {},
    el("button", { class: "btn", type: "button", text: (o && o.title) || name,
      onclick: () => downloadPath(o && o.url, (rec.id || "run") + "-" + name) }),
    " ", el("code", { text: (o && o.url) || "" }))));
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
    rec.output ? el("h2", { text: "Outputs" }) : null,
    outputsList(rec),
    el("h2", { text: "Log" }),
    el("pre", { class: "log", id: "log", text: logText || "(empty)" }),
    el("h2", { text: "Input" }),
    el("pre", { class: "log", text: inputPretty || "{}" }));
  const log = document.getElementById("log");
  log.scrollTop = log.scrollHeight;
  if (live) poll(() => runView(id));
}

// -- storages: named datasets and key-value stores, with their items and keys ---------------
const KINDS = [["datasets", "Datasets"], ["key-value-stores", "Key-value stores"]];

function fmtBytes(n) {
  if (typeof n !== "number") return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

async function storagesView() {
  const named = !/[?&]all=1/.test(location.hash);
  const lists = await Promise.all(KINDS.map(([kind]) =>
    apiJson("/v2/" + kind + (named ? "?named=1" : "")).then((d) => d.items || [])));
  const sections = KINDS.map(([kind, title], i) => {
    const rows = lists[i].sort((a, b) => String(b.modifiedAt || "").localeCompare(String(a.modifiedAt || "")));
    const href = (s) => "#/storage/" + kind + "/" + encodeURIComponent(s.id);
    return [el("h2", { text: title + " (" + rows.length + ")" }),
      rows.length ? el("div", { class: "wrap" }, el("table", { id: "storage-" + kind },
        el("thead", {}, el("tr", {}, ["Name or run", "Kind", kind === "datasets" ? "Items" : "Keys", "Size", "Modified"]
          .map((h, j) => el("th", { class: j === 2 || j === 3 ? "num" : null, text: h })))),
        el("tbody", {}, rows.map((s) => el("tr", { class: "link", tabindex: "0",
            onclick: () => { location.hash = href(s); },
            onkeydown: (e) => { if (e.key === "Enter") location.hash = href(s); } },
          el("td", {}, el("code", { text: s.name || s.id })),
          el("td", { text: s.name ? "named" : "run" }),
          el("td", { class: "num", text: String(kind === "datasets" ? s.itemCount ?? "" : s.keyCount ?? "") }),
          el("td", { class: "num", text: fmtBytes(s.bytes) }),
          el("td", { text: fmtTime(s.modifiedAt) }))))))
        : el("p", { class: "note", text: named ? "No named " + title.toLowerCase() + " yet. A run with \"datasetName\" or ctx.dataset(\"name\") creates one." : "None yet." })];
  });
  view(
    el("div", { class: "row" },
      el("div", {}, el("h1", { text: "Storage" }),
        el("p", { class: "sub", text: named ? "Named storages are shared by runs and never purged." : "Every storage, named and per run." })),
      el("a", { class: "btn push", id: "storage-toggle", href: named ? "#/storage?all=1" : "#/storage",
        text: named ? "Show run storages too" : "Named only" })),
    sections.flat());
}

async function datasetView(id) {
  const ds = encodeURIComponent(id);
  const [info, items] = await Promise.all([
    apiJson("/v2/datasets/" + ds),
    api("/v2/datasets/" + ds + "/items?format=json&desc=1&limit=" + MAX_ITEMS).then((r) => (r.ok ? r.json() : [])),
  ]);
  const list = Array.isArray(items) ? items : [];
  const views = Object.keys((info.schema && info.schema.views) || {});
  view(
    el("p", {}, el("a", { href: "#/storage", text: "All storages" })),
    el("h1", {}, el("code", { text: info.name || info.id })),
    el("p", { class: "sub", text: (info.itemCount ?? 0) + " items · " + fmtBytes(info.bytes) + " · modified " + fmtTime(info.modifiedAt)
      + (info.runId ? " · written by run " + info.runId : "") }),
    views.length ? el("p", { class: "muted", text: "Views: " + views.join(", ") }) : null,
    el("div", { class: "row gap-below" }, el("span", { class: "muted", text: "Download" }),
      FORMATS.map((f) => el("button", { class: "btn", type: "button", text: f.toUpperCase(),
        onclick: () => downloadPath("/v2/datasets/" + ds + "/items?format=" + f, id + "." + f) }))),
    el("h2", { text: "Newest items (" + list.length + (info.itemCount > list.length ? " of " + info.itemCount : "") + ")" }),
    list.length ? itemsTable(list) : el("p", { class: "note", text: "This dataset is empty." }));
}

async function kvView(id) {
  const kv = encodeURIComponent(id);
  const [info, keys] = await Promise.all([
    apiJson("/v2/key-value-stores/" + kv),
    apiJson("/v2/key-value-stores/" + kv + "/keys").then((d) => d.items || []),
  ]);
  const collections = Object.keys((info.schema && info.schema.collections) || {});
  view(
    el("p", {}, el("a", { href: "#/storage", text: "All storages" })),
    el("h1", {}, el("code", { text: info.name || info.id })),
    el("p", { class: "sub", text: keys.length + " keys · " + fmtBytes(info.bytes) + " · modified " + fmtTime(info.modifiedAt) }),
    collections.length ? el("p", { class: "muted", text: "Collections: " + collections.join(", ") }) : null,
    keys.length ? el("div", { class: "wrap" }, el("table", { id: "keys" },
      el("thead", {}, el("tr", {}, el("th", { text: "Key" }), el("th", { text: "Type" }),
        el("th", { class: "num", text: "Size" }), el("th", { text: "" }))),
      el("tbody", {}, keys.map((k) => el("tr", {},
        el("td", {}, el("code", { text: k.key })),
        el("td", { class: "muted", text: k.contentType || "" }),
        el("td", { class: "num", text: fmtBytes(k.size) }),
        el("td", {}, el("button", { class: "btn", type: "button", text: "Download",
          onclick: () => downloadPath("/v2/key-value-stores/" + kv + "/records/" + encodeURIComponent(k.key), k.key) })))))))
      : el("p", { class: "note", text: "This store has no records." }));
}

// -- tasks: saved input and options, run with one click -------------------------------------
async function tasksView() {
  const tasks = (await apiJson("/v2/actor-tasks")).items || [];
  const err = el("div", { class: "err", id: "task-error", hidden: true });
  const run = async (ev, t) => {
    ev.target.disabled = true;
    try {
      const rec = await apiJson("/v2/actor-tasks/" + encodeURIComponent(t.name) + "/runs", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: "{}" });
      location.hash = "#/runs/" + encodeURIComponent(rec.id);
    } catch (e) {
      if (e instanceof AuthError) throw e;
      err.hidden = false;
      err.replaceChildren(el("b", { text: t.name + ": " + (e.message || "could not start") }),
        el("ul", {}, ((e.body && e.body.error && e.body.error.errors) || []).map((m) => el("li", { text: m }))));
      ev.target.disabled = false;
    }
  };
  view(
    el("h1", { text: "Tasks" }),
    el("p", { class: "sub", text: "A task is an actor with saved input and run options. Save one with `ww-scraper task save` or POST /v2/actor-tasks." }),
    err,
    tasks.length ? el("div", { class: "wrap" }, el("table", { id: "tasks" },
      el("thead", {}, el("tr", {}, ["Task", "Actor", "Options", "Input", "Modified", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, tasks.map((t) => el("tr", {},
        el("td", {}, el("b", { text: t.name })),
        el("td", {}, el("a", { href: "#/actors/" + encodeURIComponent(t.actId || "") }, el("code", { text: t.actId || "" }))),
        el("td", {}, el("code", { text: cellText(t.options || {}) })),
        el("td", {}, el("code", { text: cellText(t.input || {}) })),
        el("td", { text: fmtTime(t.modifiedAt) }),
        el("td", {}, el("button", { class: "btn primary", type: "button", text: "Run",
          onclick: (ev) => guarded(() => run(ev, t)) })))))))
      : el("p", { class: "note", text: "No tasks yet." }));
}

// -- history: runs per day by status, and the latest runs -----------------------------------
const STATUS_ORDER = ["SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED", "OTHER"];
const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined) continue;
    if (k === "text") node.textContent = v; else node.setAttribute(k, String(v));
  }
  for (const c of children.flat()) if (c) node.append(c);
  return node;
}

function dayCounts(day) {
  const out = { OTHER: 0 };
  for (const [k, v] of Object.entries(day)) {
    if (k === "date") continue;
    if (STATUS_ORDER.includes(k)) out[k] = v; else out.OTHER += v;
  }
  return out;
}

function historyChart(days) {
  const W = 760, H = 220, top = 12, bottom = 26, left = 32, right = 8;
  const totals = days.map((d) => Object.entries(d).reduce((s, [k, v]) => (k === "date" ? s : s + v), 0));
  const max = Math.max(1, ...totals);
  const step = Math.max(1, Math.ceil(max / 4));
  const ceil = step * Math.ceil(max / step);
  const y = (v) => top + (H - top - bottom) * (1 - v / ceil);
  const slot = (W - left - right) / days.length;
  const bw = Math.max(4, Math.min(18, slot - 4));
  const nodes = [];
  for (let v = 0; v <= ceil; v += step) {
    nodes.push(svg("line", { class: v ? "grid" : "axis", x1: left, x2: W - right, y1: y(v), y2: y(v) }));
    nodes.push(svg("text", { class: "tick", x: left - 6, y: y(v) + 4, "text-anchor": "end", text: String(v) }));
  }
  days.forEach((d, i) => {
    const x = left + i * slot + (slot - bw) / 2;
    const counts = dayCounts(d);
    let base = 0;
    const segs = [];
    const parts = [];
    STATUS_ORDER.forEach((st) => {
      const n = counts[st] || 0;
      if (!n) return;
      const y1 = y(base + n), y0 = y(base);
      const h = Math.max(1, y0 - y1 - (base ? 2 : 0));       // 2px surface gap between segments
      segs.push(svg("rect", { class: "seg s-" + st, x, y: y1, width: bw, height: h, rx: 1 }));
      parts.push(n + " " + st.toLowerCase());
      base += n;
    });
    const label = d.date + ": " + (parts.length ? parts.join(", ") : "no runs");
    // hit target taller and wider than the bar, with a native tooltip
    nodes.push(svg("g", { class: "day", tabindex: "0", role: "img", "aria-label": label },
      svg("title", { text: label }),
      svg("rect", { class: "hit", x: left + i * slot, y: top, width: slot, height: H - top - bottom }), segs));
    // a tick every 7 days and on the last day, never two labels side by side
    if ((i % 7 === 0 && days.length - 1 - i >= 4) || i === days.length - 1) {
      nodes.push(svg("text", { class: "tick", x: x + bw / 2, y: H - 8, "text-anchor": "middle", text: d.date.slice(5) }));
    }
  });
  return svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", id: "history-chart", role: "group",
    "aria-label": "Runs started per day over the last " + days.length + " days, by final status" }, nodes);
}

async function historyView() {
  const stats = await apiJson("/v2/stats/runs?days=30&last=200");
  const days = stats.days || [];
  const totals = stats.totals || {};
  const recent = stats.recent || [];
  const all = Object.values(totals).reduce((a, b) => a + b, 0);
  const done = recent.filter((r) => typeof r.durationMillis === "number");
  const median = (xs) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : null; };
  const medMs = median(done.map((r) => r.durationMillis));
  const legend = el("ul", { class: "legend" }, STATUS_ORDER.map((st) => el("li", {},
    el("span", { class: "swatch s-" + st, "aria-hidden": "true" }),
    (st === "OTHER" ? "running or waiting" : st.toLowerCase()) + " " + (st === "OTHER"
      ? Object.entries(totals).filter(([k]) => !STATUS_ORDER.includes(k)).reduce((a, [, v]) => a + v, 0)
      : totals[st] || 0))));
  view(
    el("h1", { text: "History" }),
    el("p", { class: "sub", text: all + " run" + (all === 1 ? "" : "s") + " started in the last " + days.length + " days" }),
    el("div", { class: "grid" },
      el("div", { class: "tile" }, el("b", { text: String(all) }), el("span", { text: "runs, 30 days" })),
      el("div", { class: "tile" }, el("b", { text: all ? Math.round(100 * (totals.SUCCEEDED || 0) / all) + "%" : "-" }),
        el("span", { text: "succeeded" })),
      el("div", { class: "tile" }, el("b", { text: medMs === null ? "-" : medMs < 1000 ? medMs + " ms" : (medMs / 1000).toFixed(1) + " s" }),
        el("span", { text: "median duration, last " + recent.length })),
      el("div", { class: "tile" }, el("b", { text: String(recent.reduce((a, r) => a + (r.itemCount || 0), 0)) }),
        el("span", { text: "items, last " + recent.length + " runs" }))),
    el("h2", { text: "Runs per day by status" }),
    legend,
    el("div", { class: "chart-wrap" }, historyChart(days)),
    el("details", {}, el("summary", { text: "Show as a table" }),
      el("div", { class: "wrap" }, el("table", { id: "history-table" },
        el("thead", {}, el("tr", {}, el("th", { text: "Day" }), STATUS_ORDER.map((st) => el("th", { class: "num", text: st.toLowerCase() })))),
        el("tbody", {}, days.map((d) => { const c = dayCounts(d);
          return el("tr", {}, el("td", { text: d.date }), STATUS_ORDER.map((st) => el("td", { class: "num", text: String(c[st] || 0) }))); }))))),
    el("h2", { text: "Latest runs" }),
    recent.length ? el("div", { class: "wrap" }, el("table", { id: "recent" },
      el("thead", {}, el("tr", {}, ["Status", "Actor", "Started", "Duration", "Items", "Requests", "Failed"]
        .map((h, i) => el("th", { class: i >= 3 ? "num" : null, text: h })))),
      el("tbody", {}, recent.slice(0, 50).map((r) => el("tr", { class: "link", tabindex: "0",
          onclick: () => { location.hash = "#/runs/" + encodeURIComponent(r.id); },
          onkeydown: (e) => { if (e.key === "Enter") location.hash = "#/runs/" + encodeURIComponent(r.id); } },
        el("td", {}, pill(r.status)), el("td", {}, el("code", { text: r.actId || "" })),
        el("td", { text: fmtTime(r.startedAt) }), el("td", { class: "num", text: duration({ stats: { durationMillis: r.durationMillis } }) }),
        el("td", { class: "num", text: String(r.itemCount ?? "") }), el("td", { class: "num", text: String(r.requests ?? "") }),
        el("td", { class: "num", text: String(r.failed ?? "") }))))))
      : el("p", { class: "note", text: "No runs yet." }));
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
  const parts = (location.hash.replace(/^#\/?/, "").replace(/\?.*$/, "") || "runs").split("/").map(decodeURIComponent);
  for (const a of document.querySelectorAll("[data-nav]")) {
    if (a.dataset.nav === parts[0]) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  // no token yet: try anyway (the server may run with --no-auth); a 401 shows the form
  if (parts[0] === "actors" && parts[1]) return guarded(() => actorView(parts[1]));
  if (parts[0] === "actors") return guarded(actorsView);
  if (parts[0] === "runs" && parts[1]) return guarded(() => runView(parts[1]));
  if (parts[0] === "storage" && parts[1] === "datasets" && parts[2]) return guarded(() => datasetView(parts[2]));
  if (parts[0] === "storage" && parts[1] === "key-value-stores" && parts[2]) return guarded(() => kvView(parts[2]));
  if (parts[0] === "storage") return guarded(storagesView);
  if (parts[0] === "tasks") return guarded(tasksView);
  if (parts[0] === "history") return guarded(historyView);
  return guarded(runsView);
}

readToken();
window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", route);
