"""Actor, input-schema validation, RunContext and the run lifecycle.

A run moves READY -> RUNNING -> SUCCEEDED | FAILED | ABORTED | TIMED-OUT, passing through
ABORTING or TIMING-OUT while an actor winds down after a stop was asked. Its record lives in
storage/runs/<id>.json; the run's default dataset, key-value store and request queue
share the run id. The validated input is stored under the key INPUT.
"""
from __future__ import annotations

import datetime
import os
import re
import sys
import threading
import time
import traceback

from .compliance import PoliteSession, MIN_INTERVAL_FLOOR, redact_obj, redact_pii
from .storage import Storage, new_id, valid_id

STATUSES = ("READY", "RUNNING", "ABORTING", "TIMING-OUT", "SUCCEEDED", "FAILED", "ABORTED",
            "TIMED-OUT")
TERMINAL = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")

# --- input schema ----------------------------------------------------------------------

_MSG = {
    "en": {
        "not_object": "input must be a JSON object",
        "unknown": "{f}: unknown field",
        "required": "{f}: field is required",
        "type": "{f}: expected {t}",
        "min": "{f}: must be >= {v}",
        "max": "{f}: must be <= {v}",
        "min_items": "{f}: needs at least {v} item(s)",
        "max_items": "{f}: at most {v} item(s) allowed",
        "min_len": "{f}: must be at least {v} character(s)",
        "max_len": "{f}: must be at most {v} character(s)",
        "pattern": "{f}: contains characters that are not allowed",
        "enum": "{f}: must be one of {v}",
        "item_type": "{f}[{i}]: expected {t}",
    },
    "pl": {
        "not_object": "wejście musi być obiektem JSON",
        "unknown": "{f}: nieznane pole",
        "required": "{f}: pole jest wymagane",
        "type": "{f}: oczekiwano typu {t}",
        "min": "{f}: wartość musi być >= {v}",
        "max": "{f}: wartość musi być <= {v}",
        "min_items": "{f}: potrzeba co najmniej {v} element(ów)",
        "max_items": "{f}: dozwolone najwyżej {v} element(ów)",
        "min_len": "{f}: co najmniej {v} znak(ów)",
        "max_len": "{f}: najwyżej {v} znak(ów)",
        "pattern": "{f}: zawiera niedozwolone znaki",
        "enum": "{f}: dozwolone wartości: {v}",
        "item_type": "{f}[{i}]: oczekiwano typu {t}",
    },
}

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


class InputError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def default_lang() -> str:
    lang = os.environ.get("WW_SCRAPER_LANG", "en").lower()[:2]
    return lang if lang in _MSG else "en"


def validate_input(schema: dict, data, lang: str = None):
    """Validate data against a small JSON-Schema subset (type, required, default, enum,
    minimum/maximum, minLength/maxLength, pattern, minItems/maxItems, items.type).
    An array of strings also takes {"url": "..."} objects, the shape other scraping
    platforms use for start URLs; they are reduced to the URL string.
    Returns (clean_input, errors); errors are in English or Polish (lang='pl')."""
    m = _MSG.get((lang or default_lang())[:2], _MSG["en"])
    errors: list = []
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {}, [m["not_object"]]
    props = schema.get("properties", {})
    clean = {}
    for key in data:
        if key not in props:
            errors.append(m["unknown"].format(f=key))
    for key, spec in props.items():
        if key not in data or data[key] is None:
            if key in schema.get("required", []):
                errors.append(m["required"].format(f=key))
            elif "default" in spec:
                d = spec["default"]
                clean[key] = list(d) if isinstance(d, list) else d
            continue
        v = data[key]
        t = spec.get("type")
        if (t == "array" and isinstance(v, list)
                and (spec.get("items") or {}).get("type") == "string"):
            v = [el["url"] if isinstance(el, dict) and isinstance(el.get("url"), str) else el
                 for el in v]
        if t == "number" and isinstance(v, int) and not isinstance(v, bool):
            v = float(v)
        if t and not _TYPES[t](v):
            errors.append(m["type"].format(f=key, t=t))
            continue
        if "enum" in spec and v not in spec["enum"]:
            errors.append(m["enum"].format(f=key, v=", ".join(map(str, spec["enum"]))))
        if t in ("integer", "number"):
            if "minimum" in spec and v < spec["minimum"]:
                errors.append(m["min"].format(f=key, v=spec["minimum"]))
            if "maximum" in spec and v > spec["maximum"]:
                errors.append(m["max"].format(f=key, v=spec["maximum"]))
        if t == "string":
            if "minLength" in spec and len(v) < spec["minLength"]:
                errors.append(m["min_len"].format(f=key, v=spec["minLength"]))
            if "maxLength" in spec and len(v) > spec["maxLength"]:
                errors.append(m["max_len"].format(f=key, v=spec["maxLength"]))
            if "pattern" in spec and not re.fullmatch(spec["pattern"], v):
                errors.append(m["pattern"].format(f=key))
        if t == "array":
            if "minItems" in spec and len(v) < spec["minItems"]:
                errors.append(m["min_items"].format(f=key, v=spec["minItems"]))
            if "maxItems" in spec and len(v) > spec["maxItems"]:
                errors.append(m["max_items"].format(f=key, v=spec["maxItems"]))
            it = (spec.get("items") or {}).get("type")
            if it:
                for i, el in enumerate(v):
                    if not _TYPES[it](el):
                        errors.append(m["item_type"].format(f=key, i=i, t=it))
        clean[key] = v
    return clean, errors


# Common schema fragments reused by the actors.
START_URLS = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100,
              "title": "Start URLs", "description": "http(s) URLs to start from"}
MIN_INTERVAL = {"type": "number", "default": 1.0, "minimum": 0,
                "description": "Minimum seconds between requests to one host "
                               f"(never below {MIN_INTERVAL_FLOOR} outside the test switch)"}
CONCURRENCY = {"type": "integer", "default": 1, "minimum": 1, "maximum": 16,
               "description": "Pages fetched at once; requests to one host stay spaced "
                              "by minIntervalSecs"}
RENDER = {"type": "boolean", "default": False,
          "description": "Run each page's scripts in a local headless Chrome before reading it"}
ACCEPT_LANGUAGE = {"type": "string", "default": "", "maxLength": 200,
                   "pattern": "^[A-Za-z0-9,;=.* -]*$",
                   "description": "Accept-Language sent with every request and to the browser, "
                                  "e.g. 'en-US,en;q=0.9' to get one language version of the site "
                                  "regardless of where the run happens"}
REDACT = {"type": "boolean", "default": True,
          "description": "Mask e-mail addresses and phone numbers in output (GDPR default)"}


class Actor:
    """Subclass, set name/title/description/input_schema and implement run(ctx)."""

    name = ""
    title = ""
    description = ""
    input_schema: dict = {"type": "object", "properties": {}}

    def run(self, ctx: "RunContext") -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> dict:
        return {"id": self.name, "name": self.name, "title": self.title,
                "description": self.description, "inputSchema": self.input_schema}


# --- run context -----------------------------------------------------------------------

class RunAborted(Exception):
    pass


class RunLog:
    """Tiny line logger: storage/runs/<id>.log plus an optional stream."""

    def __init__(self, path, stream=None):
        self.path = path
        self.stream = stream
        self._lock = threading.Lock()

    def _write(self, level, msg):
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
        line = f"{ts} {level:5s} {msg}"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if self.stream is not None:
                print(line, file=self.stream, flush=True)

    def info(self, msg):
        self._write("INFO", msg)

    def warning(self, msg):
        self._write("WARN", msg)

    def error(self, msg):
        self._write("ERROR", msg)


# Run options: how a run is executed, not what the actor does. They may be given on the
# command line, as API query parameters, in a task, or as top-level keys of the input
# (where they are taken out before the input is validated against the actor's schema).
RUN_OPTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "timeoutSecs": {"type": "number", "minimum": 0,
                        "description": "Stop the run after this many seconds (0 = no limit); "
                                       "it ends TIMED-OUT and keeps what it stored"},
    },
}
STATUS_MESSAGE_MAX = 500

# run id -> RunContext of every run executing in this process (for abort requests)
_LIVE: dict = {}
_LIVE_LOCK = threading.Lock()


def split_options(actor, data, options=None, lang=None):
    """(input without run options, validated options). Keys the actor's own schema
    declares stay in the input."""
    opts = dict(options or {})
    if isinstance(data, dict):
        own = actor.input_schema.get("properties", {})
        data = dict(data)
        for key in RUN_OPTIONS_SCHEMA["properties"]:
            if key in data and key not in own:
                opts.setdefault(key, data.pop(key))
    clean = {k: v for k, v in opts.items() if v is not None}
    clean, errors = validate_input({"properties": RUN_OPTIONS_SCHEMA["properties"]}, clean, lang)
    clean = {k: v for k, v in clean.items() if k in opts}
    return data, clean, errors


class RunContext:
    """Everything an actor needs: input, storages, log, polite session, stats."""

    def __init__(self, run_id, actor, input, storage: Storage, *, allow_loopback=False,
                 log_stream=None, abort_event=None, record=None, parent=None):
        self.run_id = run_id
        self.actor = actor
        self.input = input
        self.storage = storage
        self.dataset = storage.dataset(run_id)
        self.kv = storage.kv(run_id)
        self.queue = storage.queue(run_id)
        self.log = RunLog(storage.log_path(run_id), log_stream)
        self.log_stream = log_stream
        self.allow_loopback = bool(allow_loopback)
        self.redact = bool(input.get("redactPii", True))
        self.session = PoliteSession(MIN_INTERVAL_FLOOR)
        self.abort_event = abort_event or threading.Event()
        self.parent = parent
        self.record = record if record is not None else {"id": run_id}
        self.timed_out = False
        self.deadline = None             # time.monotonic() value, or None
        self._rec_lock = threading.Lock()
        self.failures: list = []
        self.stats = {"requests": 0, "retries": 0, "robotsBlocked": 0, "ssrfBlocked": 0,
                      "failed": 0, "pages": 0, "bytes": 0, "items": 0, "rendered": 0,
                      "renderFailed": 0, "throttled": 0}

    @property
    def aborted(self) -> bool:
        return self.abort_event.is_set() or (self.parent is not None and self.parent.aborted)

    def check_abort(self):
        if self.aborted:
            raise RunAborted("run timed out" if self.timed_out else "run aborted")

    @property
    def remaining_secs(self):
        """Seconds left before this run (or the run that called it) times out, or None."""
        own = None if self.deadline is None else max(0.0, self.deadline - time.monotonic())
        up = self.parent.remaining_secs if self.parent is not None else None
        return min((x for x in (own, up) if x is not None), default=None)

    # -- the run record -----------------------------------------------------------------
    def _save_record(self) -> None:
        """Write the live record: RUNNING, or ABORTING / TIMING-OUT once a stop was asked."""
        with self._rec_lock:
            rec = self.record
            if rec.get("status") in ("RUNNING", "ABORTING", "TIMING-OUT"):
                if self.abort_event.is_set():
                    rec["status"] = "TIMING-OUT" if self.timed_out else "ABORTING"
                rec["itemCount"] = self.dataset.count()
                rec["stats"] = dict(self.stats)
                self.storage.save_run(rec)

    def set_status_message(self, text) -> None:
        """Tell whoever watches the run what it is doing ("12/40 boards read"). Stored on
        the run record at once, shown by `ww-scraper runs`, the API and the console."""
        text = self.redact_text(str(text if text is not None else ""))[:STATUS_MESSAGE_MAX]
        self.record["statusMessage"] = text
        self._save_record()

    def request_abort(self, timed_out: bool = False) -> None:
        if timed_out and not self.abort_event.is_set():
            self.timed_out = True
        self.abort_event.set()
        self._save_record()

    def push_data(self, items) -> int:
        if isinstance(items, dict):
            items = [items]
        if self.redact:
            items = [redact_obj(i) for i in items]
        n = self.dataset.push(items)
        self.stats["items"] += n
        return n

    def redact_text(self, text):
        return redact_pii(text) if self.redact else text

    def set_value(self, key, value, content_type=None):
        self.kv.set(key, value, content_type)

    def record_failure(self, url, reason):
        self.stats["failed"] += 1
        if len(self.failures) < 1000:
            self.failures.append({"url": url, "reason": reason})
        self.log.warning(f"{url}: {reason}")

    def crawler(self, **kwargs):
        from .crawler import PoliteCrawler
        min_interval = kwargs.pop("min_interval", self.input.get("minIntervalSecs", 1.0))
        crawler = PoliteCrawler(self, min_interval=min_interval, **kwargs)
        crawler.default_concurrency = int(self.input.get("maxConcurrency", 1) or 1)
        crawler.render = bool(self.input.get("render", False))
        lang = (self.input.get("acceptLanguage") or "").strip()
        if lang:
            crawler.accept_language = lang
            crawler.session.headers["Accept-Language"] = lang
        return crawler


# --- run lifecycle ---------------------------------------------------------------------

def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def get_actor(name):
    from .actors import get_actor as _get
    return _get(name)


def prepare_run(actor_name, input=None, storage: Storage = None, run_id=None, lang=None,
                options=None):
    """Validate input and create (or reopen, to resume) a run record. Raises InputError
    for bad input and KeyError for an unknown actor. options: run options (see
    RUN_OPTIONS_SCHEMA); a resumed run keeps its earlier options unless given new ones."""
    storage = storage or Storage()
    actor = get_actor(actor_name)
    if actor is None:
        raise KeyError(actor_name)
    prev = {}
    if run_id is not None:
        if not valid_id(run_id):
            raise InputError([f"invalid run id: {run_id!r}"])
        prev = storage.load_run(run_id) or {}
        if prev:
            if prev.get("actId") != actor.name:
                raise InputError([f"run {run_id} belongs to actor {prev.get('actId')}"])
            if prev.get("status") not in TERMINAL and run_id in _LIVE:
                raise InputError([f"run {run_id} is still running"])
            if input is None:
                input = storage.kv(run_id).get("INPUT", {})
    input, opts, opt_errors = split_options(actor, input, options, lang)
    if prev and not opts:
        opts = dict(prev.get("options") or {})
    clean, errors = validate_input(actor.input_schema, input, lang)
    errors = opt_errors + errors
    if errors:
        raise InputError(errors)
    run_id = run_id or new_id()
    rec = {
        "id": run_id,
        "actId": actor.name,
        "status": "READY",
        "createdAt": prev.get("createdAt") or _now(),
        "startedAt": None,
        "finishedAt": None,
        "statusMessage": "",
        "resumedFrom": prev.get("status") if prev else None,
        "options": opts,
        "defaultDatasetId": run_id,
        "defaultKeyValueStoreId": run_id,
        "defaultRequestQueueId": run_id,
        "stats": {},
        "itemCount": 0,
    }
    storage.kv(run_id).set("INPUT", clean)
    storage.save_run(rec)
    return rec


def execute_run(rec, storage: Storage = None, *, allow_loopback=False, log_stream=None,
                abort_event=None, webhooks=None, parent=None):
    """Run a prepared record to a terminal state and return the final record."""
    storage = storage or Storage()
    actor = get_actor(rec["actId"])
    clean = storage.kv(rec["id"]).get("INPUT", {})
    ctx = RunContext(rec["id"], actor, clean, storage, allow_loopback=allow_loopback,
                     log_stream=log_stream, abort_event=abort_event, record=rec, parent=parent)
    timeout = float((rec.get("options") or {}).get("timeoutSecs") or 0) or None
    if parent is not None and parent.remaining_secs is not None:
        # a called run never outlives the run that called it
        timeout = min(x for x in (timeout, parent.remaining_secs) if x is not None)
    rec["status"] = "RUNNING"
    rec["startedAt"] = _now()
    storage.save_run(rec)
    with _LIVE_LOCK:
        _LIVE[rec["id"]] = ctx
    timer = None
    t0 = time.monotonic()
    if timeout is not None:
        ctx.deadline = t0 + timeout
        timer = threading.Timer(timeout, ctx.request_abort, kwargs={"timed_out": True})
        timer.daemon = True
        timer.start()
    ctx.log.info(f"run {rec['id']} actor={actor.name} started"
                 + (f" timeout={timeout:g}s" if timeout is not None else ""))
    try:
        actor.run(ctx)
        if ctx.aborted:
            raise RunAborted("run aborted")
        status = "SUCCEEDED"
    except (RunAborted, KeyboardInterrupt):
        status = "ABORTED"
    except Exception as e:  # an actor bug must end as FAILED, never crash the host
        if ctx.timed_out:
            status = "ABORTED"   # the timeout cut a request short; report it as the timeout
        else:
            status = "FAILED"
            rec["statusMessage"] = f"{type(e).__name__}: {e}"[:STATUS_MESSAGE_MAX]
            ctx.log.error(traceback.format_exc())
    finally:
        if timer is not None:
            timer.cancel()
        with _LIVE_LOCK:
            _LIVE.pop(rec["id"], None)
    if status == "ABORTED":
        last = rec.get("statusMessage") or ""
        if ctx.timed_out:
            status = "TIMED-OUT"
            rec["statusMessage"] = (f"timed out after {timeout:g} s"
                                    + (f" (last status: {last})" if last else ""))[:STATUS_MESSAGE_MAX]
        else:
            rec["statusMessage"] = "aborted"
    if ctx.failures:
        ctx.set_value("FAILED_REQUESTS", ctx.failures)
    with ctx._rec_lock:
        rec["status"] = status
        rec["finishedAt"] = _now()
        rec["stats"] = dict(ctx.stats, durationMillis=int((time.monotonic() - t0) * 1000))
        rec["itemCount"] = ctx.dataset.count()
        storage.save_run(rec)
    ctx.log.info(f"run {rec['id']} finished: {rec['status']} items={rec['itemCount']}")
    if webhooks:
        from .webhooks import dispatch
        for line in dispatch(rec, webhooks, allow_loopback=allow_loopback):
            ctx.log.info(line)
    return rec


def request_abort(run_id) -> bool:
    """Ask a run executing in this process to stop. False when it is not running here."""
    with _LIVE_LOCK:
        ctx = _LIVE.get(run_id)
    if ctx is None:
        return False
    ctx.request_abort()
    return True


def run_actor(actor_name, input=None, storage: Storage = None, *, run_id=None, lang=None,
              allow_loopback=False, log_stream=None, abort_event=None, webhooks=None,
              options=None, timeout_secs=None):
    """Validate, create and execute a run synchronously. Returns the final record.
    timeout_secs is a shortcut for options={"timeoutSecs": ...}."""
    storage = storage or Storage()
    if timeout_secs is not None:
        options = dict(options or {}, timeoutSecs=timeout_secs)
    rec = prepare_run(actor_name, input, storage, run_id=run_id, lang=lang, options=options)
    return execute_run(rec, storage, allow_loopback=allow_loopback, log_stream=log_stream,
                       abort_event=abort_event, webhooks=webhooks)


def stderr_stream():
    return sys.stderr
