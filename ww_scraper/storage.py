"""Local storage primitives, kept on disk under ./storage/
(override with WW_SCRAPER_STORAGE_DIR):

    storage/datasets/<id>/items.jsonl          Dataset (append-only JSONL)
    storage/key_value_stores/<id>/<key>        KeyValueStore record
    storage/key_value_stores/<id>/.meta/<key>.json   its content-type
    storage/request_queues/<id>/requests.jsonl RequestQueue (append-only event log)
    storage/runs/<id>.json, <id>.log           run records and logs

IDs and keys are validated, so nothing outside the storage root can be addressed.

Unnamed storages belong to one run and share its id. Named storages (a name that is not
shaped like a run id) are shared by every run that opens them and are never purged.
"""
from __future__ import annotations

import collections
import csv
import datetime
import html
import io
import json
import os
import re
import secrets
import threading
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape, quoteattr as _xml_attr
from urllib.parse import unquote_plus, urlsplit, urlunsplit

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9!_'()-][A-Za-z0-9!_.'()-]{0,255}$")
_RUN_ID_RE = re.compile(r"^\d{14}-[0-9a-f]{8}$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


class StorageError(ValueError):
    pass


def default_root() -> Path:
    return Path(os.environ.get("WW_SCRAPER_STORAGE_DIR", "storage"))


def new_id() -> str:
    """Sortable, unguessable id: UTC timestamp + random suffix."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{ts}-{secrets.token_hex(4)}"


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def valid_key(value) -> bool:
    return isinstance(value, str) and bool(_KEY_RE.match(value)) and ".." not in value


def is_run_id(value) -> bool:
    """True for ids made by new_id(): those storages belong to one run."""
    return isinstance(value, str) and bool(_RUN_ID_RE.match(value))


def check_name(name) -> str:
    """A storage name: a valid id that cannot be mistaken for a run id."""
    if not valid_id(name) or is_run_id(name):
        raise StorageError(f"invalid storage name: {name!r} (letters, digits, '-' and '_', "
                           "up to 64, not shaped like a run id)")
    return name


def parse_duration(text) -> float:
    """'30d', '12h', '90m', '45s', '2w' -> seconds."""
    m = _DURATION_RE.match(str(text).strip().lower())
    if not m:
        raise ValueError(f"duration {text!r}: use a number and s, m, h, d or w, e.g. 30d")
    return float(m.group(1)) * _UNITS[m.group(2)]


def _parse_time(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return 0


def _check_id(value) -> str:
    if not valid_id(value):
        raise StorageError(f"invalid storage id: {value!r}")
    return value


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --- Dataset ---------------------------------------------------------------------------

_CSV_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    elif isinstance(v, bool):
        v = "true" if v else "false"
    else:
        v = str(v)
    if v.startswith(_CSV_FORMULA):
        v = "'" + v  # neutralise spreadsheet formula injection
    return v


EXPORT_FORMATS = ("json", "jsonl", "csv", "xlsx", "xml", "html", "rss", "chunks")
# Characters XML 1.0 cannot hold at all (they are not escapable): dropped, never sent raw.
_XML_INVALID = re.compile("[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_XLSX_CELL_MAX = 32767          # Excel refuses longer cells
_XLSX_ROWS_MAX = 1_048_576


def _columns(items) -> list:
    cols: list = []
    seen = set()
    for it in items:
        for k in it:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols


def _text(v) -> str:
    """One cell as text: nested values as JSON, booleans lower-case, None empty."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _xml_text(v) -> str:
    return _xml_escape(_XML_INVALID.sub("", _text(v)))


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx(items, cols) -> bytes:
    """A minimal SpreadsheetML workbook, standard library only. Text goes in inline-string
    cells, which Excel never evaluates, so a value starting with '=' stays text."""
    if len(items) + 1 > _XLSX_ROWS_MAX:
        raise StorageError(f"too many rows for xlsx ({len(items)}); use csv or jsonl")

    def cell(ref, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf")):
            return f'<c r="{ref}"><v>{v!r}</v></c>'
        s = _XML_INVALID.sub("", _text(v))[:_XLSX_CELL_MAX]
        if s == "":
            return ""
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml_escape(s)}</t></is></c>'

    rows = []
    for r, values in enumerate([cols] + [[it.get(c) for c in cols] for it in items], start=1):
        cells = "".join(cell(f"{_col_letter(i)}{r}", v) for i, v in enumerate(values))
        rows.append(f'<row r="{r}">{cells}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<sheetData>{"".join(rows)}</sheetData></worksheet>')
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="items" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'),
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))  # same items, same bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, body.encode("utf-8"))
    return buf.getvalue()


def _xml(items) -> bytes:
    """<items><item><field name="key">value</field>...</item></items>. Keys go in an
    attribute because a scraped key is rarely a valid element name."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<items>']
    for it in items:
        out.append("  <item>")
        for k, v in it.items():
            out.append(f"    <field name={_xml_attr(_XML_INVALID.sub('', str(k)))}>{_xml_text(v)}</field>")
        out.append("  </item>")
    out.append("</items>\n")
    return "\n".join(out).encode("utf-8")


def _html(items, cols, title: str) -> bytes:
    """A self-contained table: every value escaped, no scripts, a CSP that forbids them."""
    esc = lambda v: html.escape(_text(v), quote=True)
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{esc(it.get(c))}</td>" for c in cols) + "</tr>" for it in items)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            f"<title>{esc(title)}</title><style>body{{font:14px system-ui,sans-serif;margin:24px}}"
            "table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:4px 8px;text-align:left;"
            "vertical-align:top;max-width:40em;overflow-wrap:anywhere}th{background:#f2f2f2}</style></head>"
            f"<body><h1>{esc(title)}</h1><p>{len(items)} item(s)</p><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table></body></html>\n").encode("utf-8")


# --- export views ----------------------------------------------------------------------

def _get_path(obj, path):
    """obj["a"]["b"] for "a.b"; a key that itself contains dots wins over the path."""
    if path in obj:
        return True, obj[path]
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _omit_path(obj, path):
    if path in obj:
        return {k: v for k, v in obj.items() if k != path}
    head, _, rest = path.partition(".")
    if rest and isinstance(obj.get(head), dict):
        return dict(obj, **{head: _omit_path(obj[head], rest)})
    return obj


def flatten_item(obj, prefix="") -> dict:
    """Nested objects to dotted keys: {"a": {"b": 1}} -> {"a.b": 1}. Lists stay values."""
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out.update(flatten_item(v, key + "."))
        else:
            out[key] = v
    return out


def unwind_items(items, field):
    """One row per element of item[field]. Object elements are merged into the row,
    other elements replace the field; an object field is merged; anything else, or a
    missing field, leaves the item as it is."""
    for it in items:
        v = it.get(field)
        rest = {k: x for k, x in it.items() if k != field}
        if isinstance(v, list):
            if not v:
                yield rest
            for el in v:
                yield dict(rest, **el) if isinstance(el, dict) else dict(rest, **{field: el})
        elif isinstance(v, dict):
            yield dict(rest, **v)
        else:
            yield it


def apply_view(items, *, fields=None, omit=None, flatten=False, unwind=None, clean=False):
    """The export view: clean (no '#hidden' keys, no empty items), unwind, omit, flatten,
    then fields (dotted paths reach into objects)."""
    out = []
    for it in (unwind_items(items, unwind) if unwind else items):
        if clean:
            it = {k: v for k, v in it.items() if not str(k).startswith("#")}
            if not it:
                continue
        for path in omit or ():
            it = _omit_path(it, path)
        if flatten:
            it = flatten_item(it)
        if fields:
            picked = {}
            for f in fields:
                found, v = _get_path(it, f)
                if found:
                    picked[f] = v
            it = picked
        out.append(it)
    return out


_RSS_TITLE = ("title", "name", "headline", "organization", "url")
_RSS_LINK = ("url", "link", "applyUrl", "website", "startUrl", "osmUrl", "finalUrl")
_RSS_DATE = ("date", "postedAt", "datePublished", "published", "publishedAt", "pubDate",
             "updated", "updatedAt", "dateModified", "lastModified", "createdAt", "startDate")
_RSS_TEXT = ("description", "summary", "text", "snippet", "markdown")


def _rss_date(value):
    import email.utils
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = value / 1000 if value > 1e11 else value
        return email.utils.formatdate(ts, usegmt=True)
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        d = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(v)
        except (TypeError, ValueError, IndexError):
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return email.utils.format_datetime(d.astimezone(datetime.timezone.utc), usegmt=True)


def _first(it, keys):
    for k in keys:
        v = it.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _rss(items, title: str) -> bytes:
    """RSS 2.0 from items with a title-, url- or date-like field. Only http(s) values
    become links; every value is escaped; XML-invalid characters are dropped."""
    def text(v, n=None):
        s = _XML_INVALID.sub("", _text(v))
        return _xml_escape(s[:n] if n else s)

    def http(v):
        return isinstance(v, str) and v.lower().startswith(("http://", "https://"))

    entries = []
    for it in items:
        t, link = _first(it, _RSS_TITLE), _first(it, _RSS_LINK)
        link = link if http(link) else None
        if t is None and link is None:
            continue
        parts = [f"<title>{text(t if t is not None else link, 500)}</title>"]
        if link:
            parts.append(f"<link>{text(link)}</link>")
        guid = link or it.get("id")
        if guid:
            parts.append(f'<guid isPermaLink="{"true" if link else "false"}">{text(guid)}</guid>')
        when = _rss_date(_first(it, _RSS_DATE))
        if when:
            parts.append(f"<pubDate>{when}</pubDate>")
        desc = _first(it, _RSS_TEXT)
        if desc is not None:
            parts.append(f"<description>{text(desc, 2000)}</description>")
        entries.append("<item>" + "".join(parts) + "</item>")
    if items and not entries:
        raise StorageError("rss needs items with a title, name or url field")
    channel_link = next((_first(i, _RSS_LINK) for i in items if http(_first(i, _RSS_LINK))), None)
    origin = ""
    if channel_link:
        sp = urlsplit(channel_link)
        origin = f"{sp.scheme}://{sp.netloc}/"
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
            f"<title>{text(title)}</title><link>{text(origin or 'http://127.0.0.1/')}</link>"
            f"<description>{text(title)} from ww-scraper</description>"
            + "".join(entries) + "</channel></rss>\n")
    return body.encode("utf-8")


class Dataset:
    """Append-only list of JSON objects (one per line)."""

    def __init__(self, root: Path, dataset_id: str):
        self.id = _check_id(dataset_id)
        self.path = Path(root) / "datasets" / self.id
        self.file = self.path / "items.jsonl"
        self._lock = threading.Lock()

    def push(self, items) -> int:
        if isinstance(items, dict):
            items = [items]
        lines = []
        for it in items:
            if not isinstance(it, dict):
                raise StorageError("dataset items must be JSON objects")
            lines.append(json.dumps(it, ensure_ascii=False, default=str))
        if not lines:
            return 0
        with self._lock:
            self.path.mkdir(parents=True, exist_ok=True)
            with open(self.file, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return len(lines)

    def iter_items(self, offset: int = 0, limit=None):
        if not self.file.exists():
            return
        n = 0
        idx = 0
        with open(self.file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # torn trailing line after a crash
                if idx < offset:
                    idx += 1
                    continue
                idx += 1
                yield obj
                n += 1
                if limit is not None and n >= limit:
                    return

    def items(self, offset: int = 0, limit=None) -> list:
        return list(self.iter_items(offset, limit))

    def count(self) -> int:
        return sum(1 for _ in self.iter_items())

    # -- schema and views ---------------------------------------------------------------
    @property
    def schema_file(self) -> Path:
        return self.path / "schema.json"

    def set_schema(self, schema: dict) -> None:
        atomic_write(self.schema_file, json.dumps(schema, ensure_ascii=False, indent=2).encode("utf-8"))

    def schema(self):
        try:
            return json.loads(self.schema_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def view(self, name: str) -> dict:
        """Export options of a named view from the dataset's schema."""
        views = (self.schema() or {}).get("views") or {}
        if name not in views:
            raise StorageError(f"unknown view: {name!r}" + (f" (views: {', '.join(views)})" if views else
                                                           " (this dataset has no views)"))
        return views[name]

    def export(self, fmt: str = "json", offset: int = 0, limit=None, fields=None, *,
               omit=None, flatten=False, unwind=None, desc=False, clean=False, view=None,
               chunk_size=None, chunk_overlap=None):
        """Return (bytes, content_type) in json | jsonl | csv | xlsx | xml | html | rss | chunks.
        chunks: JSONL, one record per Markdown chunk with url, title and headingPath
        (chunk_size and chunk_overlap in characters, see ww_scraper/loaders.py);
        fields: keep only these keys (dotted paths reach into objects), in this order;
        omit: drop these keys or dotted paths; flatten: nested objects to dotted keys;
        unwind: one row per element of this list field; desc: newest first;
        clean: drop '#hidden' keys and items left empty."""
        if fmt not in EXPORT_FORMATS:
            raise StorageError(f"unsupported format: {fmt!r} ({', '.join(EXPORT_FORMATS)})")
        if view:                     # a named view supplies options not given explicitly
            v = self.view(view)
            fields = fields or v.get("fields")
            omit = omit or v.get("omit")
            flatten = flatten or bool(v.get("flatten"))
            unwind = unwind or v.get("unwind")
            desc = desc or bool(v.get("desc"))
            clean = clean or bool(v.get("clean"))
        if desc:
            items = self.items()[::-1]
            items = items[offset:offset + limit if limit is not None else None]
        else:
            items = self.items(offset, limit)
        fields = [f for f in (fields or []) if f]
        omit = [f for f in (omit or []) if f]
        if fields or omit or flatten or unwind or clean:
            items = apply_view(items, fields=fields, omit=omit, flatten=flatten,
                               unwind=unwind or None, clean=clean)
        if fmt == "rss":
            return _rss(items, f"Dataset {self.id}"), "application/rss+xml; charset=utf-8"
        if fmt == "chunks":
            from .loaders import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, chunk_items
            try:
                rows = chunk_items(items, chunk_size or DEFAULT_CHUNK_SIZE,
                                   DEFAULT_OVERLAP if chunk_overlap is None else chunk_overlap)
                body = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in rows)
            except ValueError as e:
                raise StorageError(str(e)) from None
            return body.encode("utf-8"), "application/jsonl; charset=utf-8"
        if fmt == "json":
            return (json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8"),
                    "application/json; charset=utf-8")
        if fmt == "jsonl":
            body = "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items)
            return body.encode("utf-8"), "application/jsonl; charset=utf-8"
        cols = list(fields) if fields else _columns(items)
        if fmt == "xlsx":
            return _xlsx(items, cols), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if fmt == "xml":
            return _xml(items), "application/xml; charset=utf-8"
        if fmt == "html":
            return _html(items, cols, f"Dataset {self.id}"), "text/html; charset=utf-8"
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            for it in items:
                w.writerow([_csv_cell(it.get(c)) for c in cols])
            return buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8"
        raise StorageError(f"unsupported format: {fmt!r}")


# --- KeyValueStore ---------------------------------------------------------------------

class KeyValueStore:
    """Named records stored as files, each with its content-type."""

    def __init__(self, root: Path, store_id: str):
        self.id = _check_id(store_id)
        self.path = Path(root) / "key_value_stores" / self.id

    def _paths(self, key):
        if not valid_key(key):
            raise StorageError(f"invalid key: {key!r}")
        return self.path / key, self.path / ".meta" / f"{key}.json"

    def set(self, key: str, value, content_type: str = None) -> None:
        data_path, meta_path = self._paths(key)
        if isinstance(value, bytes):
            data, ctype = value, content_type or "application/octet-stream"
        elif isinstance(value, str):
            data, ctype = value.encode("utf-8"), content_type or "text/plain; charset=utf-8"
        else:
            data = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            ctype = content_type or "application/json; charset=utf-8"
        atomic_write(data_path, data)
        atomic_write(meta_path, json.dumps({"contentType": ctype, "size": len(data)}).encode())

    def get_record(self, key: str):
        """(bytes, content_type) or None."""
        data_path, meta_path = self._paths(key)
        if not data_path.is_file():
            return None
        ctype = "application/octet-stream"
        if meta_path.is_file():
            try:
                ctype = json.loads(meta_path.read_text()).get("contentType") or ctype
            except ValueError:
                pass
        return data_path.read_bytes(), ctype

    def get(self, key: str, default=None):
        rec = self.get_record(key)
        if rec is None:
            return default
        data, ctype = rec
        if ctype.startswith("application/json"):
            return json.loads(data.decode("utf-8"))
        if ctype.startswith("text/"):
            return data.decode("utf-8", "replace")
        return data

    def set_schema(self, schema: dict) -> None:
        atomic_write(self.path / ".schema.json", json.dumps(schema, ensure_ascii=False, indent=2).encode("utf-8"))

    def schema(self):
        try:
            return json.loads((self.path / ".schema.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def keys(self, collection: str = None) -> list:
        """Record keys; with collection, only those the store's schema puts in it."""
        if collection is not None:
            from .schema import collection_for
            schema = self.schema() or {}
            if collection not in (schema.get("collections") or {}):
                raise StorageError(f"unknown collection: {collection!r}")
            return [k for k in self.keys() if collection_for(schema, k)[0] == collection]
        if not self.path.is_dir():
            return []
        return sorted(p.name for p in self.path.iterdir() if p.is_file() and valid_key(p.name))

    def delete(self, key: str) -> None:
        for p in self._paths(key):
            if p.exists():
                p.unlink()


# --- RequestQueue ----------------------------------------------------------------------

# Parameters that only say where a click came from: the page is the same without them.
TRACKING_PARAMS = frozenset({
    "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "fbclid", "msclkid", "yclid", "ttclid",
    "twclid", "li_fat_id", "igshid", "srsltid", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
    "mkt_tok", "_ga", "_gl", "pk_campaign", "pk_kwd", "pk_source", "pk_medium",
})
TRACKING_PREFIXES = ("utm_", "mtm_")


def _param_name(part: str) -> str:
    return unquote_plus(part.split("=", 1)[0]).lower()


def is_tracking_param(name: str) -> bool:
    name = name.lower()
    return name in TRACKING_PARAMS or name.startswith(TRACKING_PREFIXES)


def normalize_query(query: str) -> str:
    """Tracking parameters dropped, the rest sorted by name. Values keep their original
    encoding, and repeated names keep their relative order (a=1&a=2 is not a=2&a=1)."""
    parts = [p for p in query.split("&") if p and not is_tracking_param(_param_name(p))]
    return "&".join(sorted(parts, key=_param_name))


def normalize_url(url: str) -> str:
    """Default unique_key: lowercase scheme/host, no fragment, no default port, '/' path,
    query parameters sorted and tracking parameters (utm_*, gclid, fbclid, ...) removed.
    The trailing slash is kept: /a and /a/ can be different pages (the crawler learns
    per site from rel=canonical when they are not, see ww_scraper/dedupe.py)."""
    p = urlsplit(url.strip())
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    port = p.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    return urlunsplit((scheme, netloc, p.path or "/", normalize_query(p.query), ""))


class _QueueBase:
    """Shared queue logic; subclasses decide whether events are persisted."""

    def __init__(self):
        self._requests: dict = {}
        self._handled: set = set()
        self._pending: collections.deque = collections.deque()
        self._in_progress: set = set()
        self._lock = threading.Lock()

    def _log(self, event: dict) -> None:  # pragma: no cover - overridden
        pass

    def _apply(self, ev: dict) -> None:
        op, key = ev.get("op"), ev.get("unique_key")
        if not key:
            return
        if op == "add" and key not in self._requests:
            self._requests[key] = {k: ev.get(k) for k in ("unique_key", "url", "depth", "user_data")}
            if ev.get("forefront"):
                self._pending.appendleft(key)
            else:
                self._pending.append(key)
        elif op in ("handled", "failed") and key in self._requests:
            self._handled.add(key)
            if ev.get("error"):
                self._requests[key]["error"] = ev["error"]

    def _add_event(self, url, unique_key, depth, user_data, forefront):
        ev = {"op": "add", "unique_key": unique_key or normalize_url(url), "url": url,
              "depth": int(depth), "user_data": user_data or {}}
        if forefront:
            ev["forefront"] = True
        return ev

    def add(self, url: str, unique_key: str = None, depth: int = 0, user_data=None,
            forefront: bool = False) -> bool:
        """Add a request unless its unique_key is known. forefront=True puts it at the head
        of the queue (handled next) instead of the tail."""
        ev = self._add_event(url, unique_key, depth, user_data, forefront)
        with self._lock:
            if ev["unique_key"] in self._requests:
                return False
            self._log(ev)
            self._apply(ev)
            return True

    def add_many(self, requests, forefront: bool = False) -> list:
        """Add several requests (URLs or dicts with url, unique_key, depth, user_data) in one
        journal write. Returns one bool per request: True when it was new. With forefront,
        the batch goes to the head of the queue in the order given."""
        evs = []
        for r in requests:
            r = {"url": r} if isinstance(r, str) else dict(r)
            evs.append(self._add_event(r["url"], r.get("unique_key"), r.get("depth", 0),
                                       r.get("user_data"), forefront))
        out, new, keys = [], [], set()
        with self._lock:
            for ev in evs:
                key = ev["unique_key"]
                if key in self._requests or key in keys:
                    out.append(False)
                    continue
                keys.add(key)
                new.append(ev)
                out.append(True)
            if forefront:
                new.reverse()            # appendleft one by one keeps the given order
            if new:
                self._log_many(new)
                for ev in new:
                    self._apply(ev)
        return out

    def _log_many(self, events) -> None:
        for ev in events:
            self._log(ev)

    def fetch_next(self):
        with self._lock:
            while self._pending:
                key = self._pending.popleft()
                if key in self._handled or key in self._in_progress:
                    continue
                self._in_progress.add(key)
                return dict(self._requests[key])
            return None

    def mark_handled(self, request: dict, error: str = "") -> None:
        key = request["unique_key"]
        with self._lock:
            ev = {"op": "failed" if error else "handled", "unique_key": key}
            if error:
                ev["error"] = str(error)[:500]
            self._log(ev)
            self._apply(ev)
            self._in_progress.discard(key)

    def reclaim(self, request: dict) -> None:
        key = request["unique_key"]
        with self._lock:
            self._in_progress.discard(key)
            if key not in self._handled:
                self._pending.appendleft(key)

    @property
    def handled_count(self) -> int:
        return len(self._handled)

    @property
    def pending_count(self) -> int:
        return len(self._requests) - len(self._handled)

    @property
    def total_count(self) -> int:
        return len(self._requests)

    def is_finished(self) -> bool:
        return self.pending_count == 0

    def list_requests(self, offset: int = 0, limit: int = 100) -> list:
        """Requests in the order they were added, each with handled and error."""
        with self._lock:
            keys = list(self._requests)[offset:offset + limit]
            return [dict(self._requests[k], handled=k in self._handled) for k in keys]

    def head(self, limit: int = 10) -> list:
        """The next pending requests, without taking them."""
        with self._lock:
            out = []
            for k in self._pending:
                if k in self._handled or k in self._in_progress or any(r["unique_key"] == k for r in out):
                    continue
                out.append(dict(self._requests[k]))
                if len(out) >= limit:
                    break
            return out


class RequestQueue(_QueueBase):
    """Durable queue: every add/handled event is appended to a JSONL log and replayed on
    open, so an interrupted run resumes where it stopped. Dedup is by unique_key.
    Requests that were in progress when a run died are simply pending again."""

    def __init__(self, root: Path, queue_id: str):
        super().__init__()
        self.id = _check_id(queue_id)
        self.path = Path(root) / "request_queues" / self.id
        self.file = self.path / "requests.jsonl"
        if self.file.exists():
            with open(self.file, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        self._apply(json.loads(line))
                    except ValueError:
                        continue

    def _log(self, event: dict) -> None:
        self._log_many([event])

    def _log_many(self, events) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.file, "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))


class MemoryRequestQueue(_QueueBase):
    """Same API, in memory only (standalone crawler use)."""

    def __init__(self):
        super().__init__()
        self.id = "memory"


# --- run statistics --------------------------------------------------------------------

def run_stats(runs, days: int = 30, last: int = 200, now=None) -> dict:
    """Runs started per day and status over the last `days` days (oldest first, every day
    present), and metrics of the `last` most recent runs."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    first = (now - datetime.timedelta(days=days - 1)).date()
    per_day = {(first + datetime.timedelta(days=i)).isoformat(): {} for i in range(days)}
    for r in runs:
        when = _parse_time(r.get("startedAt")) or _parse_time(r.get("createdAt"))
        if when is None:
            continue
        day = when.date().isoformat()
        if day in per_day:
            st = r.get("status") or "UNKNOWN"
            per_day[day][st] = per_day[day].get(st, 0) + 1
    recent = sorted(runs, key=lambda r: r.get("createdAt") or "", reverse=True)[:last]
    metrics = [{"id": r.get("id"), "actId": r.get("actId"), "status": r.get("status"),
                "startedAt": r.get("startedAt"), "itemCount": r.get("itemCount", 0),
                "durationMillis": (r.get("stats") or {}).get("durationMillis"),
                "requests": (r.get("stats") or {}).get("requests"),
                "failed": (r.get("stats") or {}).get("failed")} for r in recent]
    totals: dict = {}
    for counts in per_day.values():
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
    return {"days": [dict(date=d, **c) for d, c in per_day.items()], "totals": totals, "recent": metrics}


# --- Storage root ----------------------------------------------------------------------

class Storage:
    def __init__(self, root=None):
        self.root = Path(root) if root else default_root()

    def dataset(self, dataset_id: str) -> Dataset:
        return Dataset(self.root, dataset_id)

    def kv(self, store_id: str) -> KeyValueStore:
        return KeyValueStore(self.root, store_id)

    def queue(self, queue_id: str) -> RequestQueue:
        return RequestQueue(self.root, queue_id)

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{_check_id(run_id)}.json"

    def log_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{_check_id(run_id)}.log"

    def save_run(self, record: dict) -> None:
        atomic_write(self.run_path(record["id"]),
                     json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))

    def load_run(self, run_id: str):
        if not valid_id(run_id):
            return None
        p = self.run_path(run_id)
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def list_runs(self) -> list:
        if not self.runs_dir.is_dir():
            return []
        out = []
        for p in sorted(self.runs_dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return out

    # -- named storages ---------------------------------------------------------------
    def named_dataset(self, name: str) -> Dataset:
        return Dataset(self.root, check_name(name))

    def named_kv(self, name: str) -> KeyValueStore:
        return KeyValueStore(self.root, check_name(name))

    def named_queue(self, name: str) -> RequestQueue:
        return RequestQueue(self.root, check_name(name))

    # -- listing and deleting ------------------------------------------------------------
    KINDS = {"datasets": "datasets", "key-value-stores": "key_value_stores",
             "request-queues": "request_queues"}

    def _kind_dir(self, kind: str) -> Path:
        if kind not in self.KINDS:
            raise StorageError(f"unknown storage kind: {kind!r}")
        return self.root / self.KINDS[kind]

    def storage_info(self, kind: str, storage_id: str):
        """{id, name, runId, kind, modifiedAt, bytes, ...counts} or None when it does not exist."""
        path = self._kind_dir(kind) / _check_id(storage_id)
        if not path.is_dir():
            return None
        files = [p for p in path.rglob("*") if p.is_file()]
        mtime = max((p.stat().st_mtime for p in files), default=path.stat().st_mtime)
        info = {"id": storage_id, "kind": kind, "name": None if is_run_id(storage_id) else storage_id,
                "runId": storage_id if is_run_id(storage_id) else None,
                "modifiedAt": datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z",
                "bytes": sum(p.stat().st_size for p in files)}
        if kind == "datasets":
            ds = self.dataset(storage_id)
            info["itemCount"] = ds.count()
            info["schema"] = ds.schema()
        elif kind == "key-value-stores":
            kv = self.kv(storage_id)
            info["keyCount"] = len(kv.keys())
            info["schema"] = kv.schema()
        else:
            q = self.queue(storage_id)
            info.update(totalRequestCount=q.total_count, handledRequestCount=q.handled_count,
                        pendingRequestCount=q.pending_count)
        return info

    def list_storages(self, kind: str, named=None) -> list:
        """Info of every storage of a kind, newest first; named=True/False filters."""
        folder = self._kind_dir(kind)
        if not folder.is_dir():
            return []
        out = []
        for p in folder.iterdir():
            if not p.is_dir() or not valid_id(p.name):
                continue
            if named is not None and named == is_run_id(p.name):
                continue
            info = self.storage_info(kind, p.name)
            if info:
                out.append(info)
        return sorted(out, key=lambda i: i["modifiedAt"], reverse=True)

    def run_is_live(self, run_id: str) -> bool:
        from .actor import TERMINAL
        rec = self.load_run(run_id) if is_run_id(run_id) else None
        return rec is not None and rec.get("status") not in TERMINAL

    def delete_storage(self, kind: str, storage_id: str) -> bool:
        """Delete one storage. A storage of a run that is still going is refused."""
        import shutil
        path = self._kind_dir(kind) / _check_id(storage_id)
        if self.run_is_live(storage_id):
            raise StorageError(f"run {storage_id} is still going; abort it first")
        if not path.is_dir():
            return False
        shutil.rmtree(path)
        return True

    def delete_run(self, run_id: str) -> bool:
        """Delete a finished run with its record, log, report and unnamed storages."""
        import shutil
        if self.load_run(run_id) is None:
            return False
        if self.run_is_live(run_id):
            raise StorageError(f"run {run_id} is still going; abort it first")
        for p in self.run_paths(run_id):
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        return True

    # -- retention --------------------------------------------------------------------
    def run_paths(self, run_id: str) -> list:
        """Everything that belongs to one run: record, log, report and its unnamed storages."""
        _check_id(run_id)
        # storages first, the record last: a purge cut short leaves a record to retry with
        return [self.root / "datasets" / run_id, self.root / "key_value_stores" / run_id,
                self.root / "request_queues" / run_id, self.root / "reports" / f"{run_id}.html",
                self.log_path(run_id), self.run_path(run_id)]

    def purge(self, older_than_secs: float, dry_run: bool = False, now=None) -> list:
        """Delete finished runs older than older_than_secs together with their unnamed
        storages. Named storages and runs still going are never touched. Returns one
        entry per run: {runId, actId, status, finishedAt, bytes, paths}."""
        from .actor import TERMINAL
        now = now or datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(seconds=float(older_than_secs))
        out = []
        for rec in self.list_runs():
            rid = rec.get("id")
            if not is_run_id(rid) or rec.get("status") not in TERMINAL:
                continue
            when = _parse_time(rec.get("finishedAt")) or _parse_time(rec.get("createdAt"))
            if when is None or when > cutoff:
                continue
            paths = [p for p in self.run_paths(rid) if p.exists()]
            entry = {"runId": rid, "actId": rec.get("actId"), "status": rec.get("status"),
                     "finishedAt": rec.get("finishedAt"), "bytes": sum(_size(p) for p in paths),
                     "paths": [str(p) for p in paths]}
            if not dry_run:
                import shutil
                for p in paths:
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
            out.append(entry)
        return out
