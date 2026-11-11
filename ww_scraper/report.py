"""A self-contained HTML report of one run: record, counters, failed requests and items.

Everything comes from local storage; the file has no scripts and no external references,
and every value is HTML-escaped (item text was written by other people's websites)."""
from __future__ import annotations

import json
from html import escape

MAX_ITEMS = 500
MAX_CELL = 600

CSS = """
:root{--bg:#fdfff5;--ink:#050507;--muted:#55555a;--line:#d9dbcf;--card:#ffffff;--accent:#322db8;
--ok:#1b6e3c;--bad:#a3261d}
@media (prefers-color-scheme:dark){:root{--bg:#050507;--ink:#fdfff5;--muted:#a8a8b0;--line:#2a2a33;
--card:#14141c;--accent:#8c8cf0;--ok:#6fd39b;--bad:#ff8b80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 10px}
.sub{color:var(--muted);margin:0 0 24px}.status{font-weight:700}.SUCCEEDED{color:var(--ok)}
.FAILED,.ABORTED{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.tile span{color:var(--muted);font-size:13px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--line);
max-width:420px;overflow-wrap:anywhere}th{position:sticky;top:0;background:var(--card);color:var(--muted);
font-weight:600}tr:last-child td{border-bottom:0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
footer{margin-top:40px;color:var(--muted);font-size:13px}
"""

_STAT_LABELS = [("items", "items"), ("pages", "pages parsed"), ("requests", "requests"),
                ("retries", "retries"), ("robotsBlocked", "refused by robots.txt"),
                ("ssrfBlocked", "refused by SSRF guard"), ("failed", "failed"),
                ("rendered", "rendered"), ("bytes", "bytes read"), ("durationMillis", "ms")]


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    if len(text) > MAX_CELL:
        text = text[:MAX_CELL] + " ..."
    return escape(text)


def _table(rows: list, columns: list) -> str:
    head = "".join(f"<th>{escape(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_cell(r.get(c))}</td>" for c in columns) + "</tr>"
                   for r in rows)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def build_report(storage, run_id: str) -> str:
    rec = storage.load_run(run_id)
    if rec is None:
        raise KeyError(run_id)
    kv = storage.kv(run_id)
    items = storage.dataset(rec.get("defaultDatasetId") or run_id).items(limit=MAX_ITEMS)
    columns: list = []
    for it in items:
        for k in it:
            if k not in columns:
                columns.append(k)
    stats = rec.get("stats") or {}
    tiles = "".join(f'<div class="tile"><b>{escape(str(stats[k]))}</b><span>{escape(label)}</span></div>'
                    for k, label in _STAT_LABELS if k in stats)
    status = str(rec.get("status", ""))
    parts = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>ww-scraper run {escape(run_id)}</title><style>{CSS}</style></head><body><main>",
        f"<h1>{escape(str(rec.get('actId', '')))}</h1>",
        f'<p class="sub">run <code>{escape(run_id)}</code> · '
        f'<span class="status {escape(status)}">{escape(status)}</span> · '
        f"started {escape(str(rec.get('startedAt') or ''))} · finished {escape(str(rec.get('finishedAt') or ''))}"
        + (f" · {escape(str(rec.get('statusMessage')))}" if rec.get("statusMessage") else "") + "</p>",
        f'<div class="grid">{tiles}</div>',
    ]
    output = kv.get("OUTPUT")
    if output is not None:
        parts += ["<h2>Summary</h2>",
                  f'<div class="wrap"><table><tbody><tr><td><code>{_cell(output)}</code></td></tr></tbody></table></div>']
    failures = kv.get("FAILED_REQUESTS") or []
    if failures:
        parts += [f"<h2>Not fetched ({len(failures)})</h2>", _table(failures[:MAX_ITEMS], ["url", "reason"])]
    total = rec.get("itemCount", len(items))
    parts.append(f"<h2>Items ({len(items)} of {total})</h2>")
    parts.append(_table(items, columns) if items else '<p class="sub">No items.</p>')
    parts += [f"<h2>Input</h2><div class=\"wrap\"><table><tbody><tr><td><code>{_cell(kv.get('INPUT'))}</code></td></tr></tbody></table></div>",
              "<footer>Generated by ww-scraper from local storage. Made by WW.</footer>",
              "</main></body></html>"]
    return "\n".join(parts)
