"""Local storage primitives shaped like the Apify platform, kept on disk under ./storage/
(override with LUSTRO_STORAGE_DIR), similar to apify_storage:

    storage/datasets/<id>/items.jsonl          Dataset (append-only JSONL)
    storage/key_value_stores/<id>/<key>        KeyValueStore record
    storage/key_value_stores/<id>/.meta/<key>.json   its content-type
    storage/request_queues/<id>/requests.jsonl RequestQueue (append-only event log)
    storage/runs/<id>.json, <id>.log           run records and logs

IDs and keys are validated, so nothing outside the storage root can be addressed.
"""
from __future__ import annotations

import collections
import csv
import datetime
import io
import json
import os
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9!_'()-][A-Za-z0-9!_.'()-]{0,255}$")


class StorageError(ValueError):
    pass


def default_root() -> Path:
    return Path(os.environ.get("LUSTRO_STORAGE_DIR", "storage"))


def new_id() -> str:
    """Sortable, unguessable id: UTC timestamp + random suffix."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{ts}-{secrets.token_hex(4)}"


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def valid_key(value) -> bool:
    return isinstance(value, str) and bool(_KEY_RE.match(value)) and ".." not in value


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

    def export(self, fmt: str = "json", offset: int = 0, limit=None):
        """Return (bytes, content_type) in json | jsonl | csv."""
        items = self.items(offset, limit)
        if fmt == "json":
            return (json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8"),
                    "application/json; charset=utf-8")
        if fmt == "jsonl":
            body = "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items)
            return body.encode("utf-8"), "application/jsonl; charset=utf-8"
        if fmt == "csv":
            cols: list = []
            for it in items:
                for k in it:
                    if k not in cols:
                        cols.append(k)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            for it in items:
                w.writerow([_csv_cell(it.get(c)) for c in cols])
            return buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8"
        raise StorageError(f"unsupported format: {fmt!r} (json, jsonl, csv)")


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

    def keys(self) -> list:
        if not self.path.is_dir():
            return []
        return sorted(p.name for p in self.path.iterdir() if p.is_file() and valid_key(p.name))

    def delete(self, key: str) -> None:
        for p in self._paths(key):
            if p.exists():
                p.unlink()


# --- RequestQueue ----------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Default unique_key: lowercase scheme/host, no fragment, no default port, '/' path."""
    p = urlsplit(url.strip())
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    port = p.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    return urlunsplit((scheme, netloc, p.path or "/", p.query, ""))


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
            self._pending.append(key)
        elif op in ("handled", "failed") and key in self._requests:
            self._handled.add(key)
            if ev.get("error"):
                self._requests[key]["error"] = ev["error"]

    def add(self, url: str, unique_key: str = None, depth: int = 0, user_data=None) -> bool:
        key = unique_key or normalize_url(url)
        with self._lock:
            if key in self._requests:
                return False
            ev = {"op": "add", "unique_key": key, "url": url, "depth": int(depth),
                  "user_data": user_data or {}}
            self._log(ev)
            self._apply(ev)
            return True

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
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


class MemoryRequestQueue(_QueueBase):
    """Same API, in memory only (standalone crawler use)."""

    def __init__(self):
        super().__init__()
        self.id = "memory"


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
