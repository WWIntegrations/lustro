"""Actor, input-schema validation, RunContext and the run lifecycle.

A run moves READY -> RUNNING -> SUCCEEDED | FAILED | ABORTED | TIMED-OUT, passing through
ABORTING or TIMING-OUT while an actor winds down after a stop was asked. Its record lives in
storage/runs/<id>.json; the run's default dataset, key-value store and request queue
share the run id. The validated input is stored under the key INPUT.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys
import threading
import time
import traceback

from .compliance import PoliteSession, MIN_INTERVAL_FLOOR, redact_obj, redact_pii
from .storage import Dataset, KeyValueStore, RequestQueue, Storage, check_name, new_id, valid_id

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
        "enum": "{f}: must be one of {v}",
        "item_type": "{f}[{i}]: expected {t}",
        "pattern": "{f}: does not match the pattern {v}",
        "date": "{f}: expected a date as YYYY-MM-DD",
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
        "enum": "{f}: dozwolone wartości: {v}",
        "item_type": "{f}[{i}]: oczekiwano typu {t}",
        "pattern": "{f}: wartość nie pasuje do wzorca {v}",
        "date": "{f}: oczekiwano daty w formacie RRRR-MM-DD",
    },
}

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    # NaN and infinity are not numbers here: nan < 0 is False, so they pass every bound
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _no_constant(name):
    raise ValueError(f"{name} is not valid JSON here")


def _finite_float(text):
    v = float(text)
    if not math.isfinite(v):          # 1e999 parses to inf
        raise ValueError(f"{text} is out of range")
    return v


def loads_strict(raw):
    """json.loads that refuses NaN, Infinity and numbers too large for a float. Python's
    parser takes them by default, and a NaN slips through every minimum/maximum check."""
    return json.loads(raw, parse_constant=_no_constant, parse_float=_finite_float)


class InputError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def default_lang() -> str:
    lang = os.environ.get("WW_SCRAPER_LANG", "en").lower()[:2]
    return lang if lang in _MSG else "en"


def _pattern_ok(pattern, value) -> bool:
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


def _field_errors(key, spec, v, m):
    """[(keyword, message)] for one present value; v is already type-checked."""
    out = []
    t = spec.get("type")
    if "enum" in spec and v not in spec["enum"]:
        out.append(("enum", m["enum"].format(f=key, v=", ".join(map(str, spec["enum"])))))
    if t in ("integer", "number"):
        if "minimum" in spec and v < spec["minimum"]:
            out.append(("minimum", m["min"].format(f=key, v=spec["minimum"])))
        if "maximum" in spec and v > spec["maximum"]:
            out.append(("maximum", m["max"].format(f=key, v=spec["maximum"])))
    if t == "string":
        if "minLength" in spec and len(v) < spec["minLength"]:
            out.append(("minLength", m["min_len"].format(f=key, v=spec["minLength"])))
        if "maxLength" in spec and len(v) > spec["maxLength"]:
            out.append(("maxLength", m["max_len"].format(f=key, v=spec["maxLength"])))
        if spec.get("pattern") and v and not _pattern_ok(spec["pattern"], v):
            out.append(("pattern", m["pattern"].format(f=key, v=spec["pattern"])))
        if spec.get("editor") == "datepicker" and v and not _DATE_RE.fullmatch(v):
            out.append(("format", m["date"].format(f=key)))
    if t == "array":
        if "minItems" in spec and len(v) < spec["minItems"]:
            out.append(("minItems", m["min_items"].format(f=key, v=spec["minItems"])))
        if "maxItems" in spec and len(v) > spec["maxItems"]:
            out.append(("maxItems", m["max_items"].format(f=key, v=spec["maxItems"])))
        items = spec.get("items") or {}
        it = items.get("type")
        for i, el in enumerate(v):
            if it and not _TYPES[it](el):
                out.append(("items", m["item_type"].format(f=key, i=i, t=it)))
            elif it == "string" and items.get("pattern") and not _pattern_ok(items["pattern"], el):
                out.append(("pattern", m["pattern"].format(f=f"{key}[{i}]", v=items["pattern"])))
    return out


def _custom(key, spec, keyword, message):
    """errorMessage: one text for the field, or {keyword: text}."""
    custom = spec.get("errorMessage")
    if isinstance(custom, str):
        return f"{key}: {custom}"
    if isinstance(custom, dict) and isinstance(custom.get(keyword), str):
        return f"{key}: {custom[keyword]}"
    return message


def validate_input(schema: dict, data, lang: str = None):
    """Validate data against a small JSON-Schema subset (type, required, default, enum,
    minimum/maximum, minLength/maxLength, pattern, minItems/maxItems, items.type and
    items.pattern, a YYYY-MM-DD check for editor "datepicker", and errorMessage).
    prefill, editor, sectionCaption and secret are hints for forms; see secret_fields().
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
                errors.append(_custom(key, spec, "required", m["required"].format(f=key)))
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
            errors.append(_custom(key, spec, "type", m["type"].format(f=key, t=t)))
            continue
        errors += [_custom(key, spec, kw, msg) for kw, msg in _field_errors(key, spec, v, m)]
        clean[key] = v
    return clean, errors


def fill_secrets(actor, data):
    """A copy of data with missing secret fields taken from their environment variables."""
    if not isinstance(data, dict):
        return data
    data = dict(data)
    for key, env in secret_fields(actor.input_schema).items():
        if data.get(key) in (None, "") and env and os.environ.get(env):
            data[key] = os.environ[env]
    return data


def secret_fields(schema: dict) -> dict:
    """{field: env var name or None} for fields marked "secret": true. A secret is never
    written to the stored INPUT, a task, a log line or the console; when it is not given,
    it is read from the environment variable named by the field's "env"."""
    return {k: (spec.get("env") or None) for k, spec in (schema.get("properties") or {}).items()
            if spec.get("secret")}


# Common schema fragments reused by the actors.
START_URLS = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100,
              "title": "Start URLs", "description": "http(s) URLs to start from"}
MIN_INTERVAL = {"type": "number", "default": 1.0, "minimum": 0,
                "description": "Minimum seconds between requests to one host "
                               f"(never below {MIN_INTERVAL_FLOOR} outside the test switch)"}
CONCURRENCY = {"type": "integer", "default": 1, "minimum": 1, "maximum": 16,
               "description": "Pages fetched at once; requests to one host stay spaced "
                              "by minIntervalSecs"}
RENDER = {"enum": [False, True, "auto"], "default": False,
          "description": ("Run each page's scripts in a local headless Chrome before reading it: "
                          "true for every page, \"auto\" only for pages that look built by scripts")}
SCREENSHOT = {"type": "string", "enum": ["", "viewport", "fullPage"], "default": "",
              "description": "Save a screenshot of each rendered page in the run's key-value store"}
SCREENSHOT_FORMAT = {"type": "string", "enum": ["png", "jpeg"], "default": "png"}
PDF = {"type": "boolean", "default": False, "description": "Save each rendered page as a PDF in the key-value store"}
# browser interaction for rendered pages (any of them turns rendering on)
WAIT_FOR = {"type": "string", "default": "", "maxLength": 300,
            "description": "Rendered pages: wait until this CSS selector exists before reading the page"}
BROWSER_ACTIONS = {"type": "array", "items": {"type": "object"}, "default": [], "maxItems": 20, "editor": "json",
                   "description": ('Rendered pages: steps run before reading, e.g. [{"click": "#more"}, '
                                   '{"waitForSelector": ".item"}, {"scroll": "bottom"}, {"wait": 500}]')}
ACCEPT_LANGUAGE = {"type": "string", "default": "", "maxLength": 200,
                   "pattern": "^[A-Za-z0-9,;=.* -]*$",
                   "description": "Accept-Language sent with every request and to the browser, "
                                  "e.g. 'en-US,en;q=0.9' to get one language version of the site "
                                  "regardless of where the run happens"}
AUTOSCALE = {"type": "boolean", "default": False,
             "description": ("Let concurrency follow this machine's load and memory, between 1 and "
                             "maxConcurrency (per-host spacing is unchanged)")}
REDACT = {"type": "boolean", "default": True,
          "description": "Mask e-mail addresses and phone numbers in output (GDPR default)"}


DEFAULT_OUTPUT = {
    "items": {"title": "Items", "template": "/v2/datasets/{defaultDatasetId}/items"},
    "summary": {"title": "Summary (OUTPUT)",
                "template": "/v2/key-value-stores/{defaultKeyValueStoreId}/records/OUTPUT"},
}


class Actor:
    """Subclass, set name/title/description/input_schema and implement run(ctx).
    Optional: version, output_schema, dataset_schema, kv_schema (see ww_scraper/schema.py)."""

    name = ""
    title = ""
    description = ""
    version = ""                 # empty: the package version
    input_schema: dict = {"type": "object", "properties": {}}
    output_schema: dict = DEFAULT_OUTPUT
    dataset_schema: dict = None
    kv_schema: dict = None

    def run(self, ctx: "RunContext") -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def get_version(self) -> str:
        from ._version import __version__
        return self.version or __version__

    def describe(self) -> dict:
        d = {"id": self.name, "name": self.name, "title": self.title, "version": self.get_version(),
             "description": self.description, "inputSchema": self.input_schema,
             "outputSchema": self.output_schema}
        if self.dataset_schema:
            d["datasetSchema"] = self.dataset_schema
        if self.kv_schema:
            d["keyValueStoreSchema"] = self.kv_schema
        return d


# --- run context -----------------------------------------------------------------------

class RunAborted(Exception):
    pass


def mask_secrets(text, secrets) -> str:
    """text with every secret value (and its URL-encoded forms) replaced by [secret]."""
    from urllib.parse import quote, quote_plus
    text = str(text if text is not None else "")
    for secret in secrets or ():
        for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
            text = text.replace(form, "[secret]")
    return text


class RunLog:
    """Tiny line logger: storage/runs/<id>.log plus an optional stream."""

    def __init__(self, path, stream=None):
        self.path = path
        self.stream = stream
        self.secrets: list = []          # values replaced by [secret] in every line
        self._lock = threading.Lock()

    def _write(self, level, msg):
        msg = mask_secrets(msg, self.secrets)
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
        "maxItems": {"type": "integer", "minimum": 1,
                     "description": "Stop cleanly once this many items are stored (the run "
                                    "SUCCEEDS with exactly that many)"},
        "maxRequests": {"type": "integer", "minimum": 1,
                        "description": "Stop cleanly after this many HTTP requests"},
        "maxBytes": {"type": "integer", "minimum": 1,
                     "description": "Stop cleanly after downloading this many bytes"},
        "checks": {"type": "object",
                   "description": "Conditions checked after the run, e.g. {\"minItems\": 1, "
                                  "\"fieldFillRate\": {\"email\": 0.5}} (see ww_scraper/checks.py)"},
        "datasetName": {"type": "string", "maxLength": 64,
                        "description": "Also append this run's items to the named dataset, "
                                       "which is kept across runs and never purged"},
    },
}
STATUS_MESSAGE_MAX = 500
BUDGETS = {"maxItems": "items", "maxRequests": "requests", "maxBytes": "bytes"}
# what runs started with ctx.call_actor() used, counted against the caller's budgets too
CALLED_USAGE = {"maxRequests": "calledRequests", "maxBytes": "calledBytes"}
MAX_CALL_DEPTH = 4
STATE_KEY = "STATE"
STATE_PERSIST_SECS = 60.0               # ctx.call_actor() nesting: a run calling itself stops here

# run id -> secret input values of a prepared run, kept in memory only. A run that is
# prepared but never executed (aborted while waiting for a slot) drops its entry through
# discard_secrets(); the cap bounds what any other path could leave behind.
_SECRETS: dict = {}
_SECRETS_MAX = 1024
_SECRETS_LOCK = threading.Lock()


def _hold_secrets(run_id, held) -> None:
    with _SECRETS_LOCK:
        _SECRETS.pop(run_id, None)
        _SECRETS[run_id] = held
        while len(_SECRETS) > _SECRETS_MAX:      # oldest first (dicts keep insertion order)
            _SECRETS.pop(next(iter(_SECRETS)))


def discard_secrets(run_id) -> None:
    """Forget the secret input values of a prepared run that will not be executed."""
    with _SECRETS_LOCK:
        _SECRETS.pop(run_id, None)
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
    if "checks" in clean:
        from .checks import validate as validate_checks
        errors += validate_checks(clean["checks"])
    if clean.get("datasetName"):
        try:
            check_name(clean["datasetName"])
        except ValueError as e:
            errors.append(f"datasetName: {e}")
    return data, clean, errors


class RunDataset(Dataset):
    """The run's own dataset. Called with a name, returns that named dataset instead:
    ctx.dataset.push(...) writes to this run, ctx.dataset("leads").push(...) to the named
    one, with the same PII redaction as ctx.push_data."""

    def __init__(self, ctx, root, dataset_id):
        super().__init__(root, dataset_id)
        self._ctx = ctx

    def __call__(self, name=None):
        if name is None:
            return self
        return _NamedDataset(self._ctx, self._ctx.storage.root, check_name(name))


class _NamedDataset(Dataset):
    def __init__(self, ctx, root, dataset_id):
        super().__init__(root, dataset_id)
        self._ctx = ctx

    def push(self, items) -> int:
        if isinstance(items, dict):
            items = [items]
        if self._ctx.redact:
            items = [redact_obj(i) for i in items]
        return super().push(items)


class RunKeyValueStore(KeyValueStore):
    """The run's own key-value store; ctx.kv("name") opens a named one."""

    def __init__(self, ctx, root, store_id):
        super().__init__(root, store_id)
        self._ctx = ctx

    def __call__(self, name=None):
        return self if name is None else self._ctx.storage.named_kv(name)


class RunRequestQueue(RequestQueue):
    """The run's own request queue; ctx.queue("name") opens a named one."""

    def __init__(self, ctx, root, queue_id):
        super().__init__(root, queue_id)
        self._ctx = ctx

    def __call__(self, name=None):
        return self if name is None else self._ctx.storage.named_queue(name)


class RunContext:
    """Everything an actor needs: input, storages, log, polite session, stats."""

    def __init__(self, run_id, actor, input, storage: Storage, *, allow_loopback=False,
                 log_stream=None, abort_event=None, record=None, parent=None, allow_hosts=None):
        self.run_id = run_id
        self.actor = actor
        self.input = input
        self.storage = storage
        self.dataset = RunDataset(self, storage.root, run_id)
        self.kv = RunKeyValueStore(self, storage.root, run_id)
        self.queue = RunRequestQueue(self, storage.root, run_id)
        self.log = RunLog(storage.log_path(run_id), log_stream)
        self.log_stream = log_stream
        self.allow_loopback = bool(allow_loopback)
        self.redact = bool(input.get("redactPii", True))
        self.session = PoliteSession(MIN_INTERVAL_FLOOR)
        self.abort_event = abort_event or threading.Event()
        self.parent = parent
        self.depth = parent.depth + 1 if parent is not None else 0
        # hosts the crawler may fetch from (subdomains included); None = any public host.
        # A called run keeps the allowlist of the run that called it.
        if allow_hosts is None and parent is not None:
            allow_hosts = parent.allow_hosts
        self.allow_hosts = frozenset(h.lower().strip(".") for h in allow_hosts) if allow_hosts else None
        self.record = record if record is not None else {"id": run_id}
        self.timed_out = False
        self.deadline = None             # time.monotonic() value, or None
        opts = self.record.get("options") or {}
        self.budgets = {k: opts[k] for k in BUDGETS if opts.get(k)}
        self.budget_hit = None           # "maxItems=100" once a budget stopped the run
        self._state = None
        self._state_key = STATE_KEY
        self._state_timer = None
        self._rec_lock = threading.Lock()
        self.failures: list = []
        self.stats = {"requests": 0, "retries": 0, "robotsBlocked": 0, "ssrfBlocked": 0,
                      "failed": 0, "pages": 0, "bytes": 0, "items": 0, "rendered": 0,
                      "renderFailed": 0, "throttled": 0}

    @property
    def aborted(self) -> bool:
        if self.abort_event.is_set() or (self.parent is not None and self.parent.aborted):
            return True
        for key, limit in self.budgets.items():
            if self._budget_used(key) >= limit:
                self.budget_hit = f"{key}={limit}"
                self.abort_event.set()
                return True
        return False

    def _budget_used(self, key) -> int:
        return self.stats.get(BUDGETS[key], 0) + self.stats.get(CALLED_USAGE.get(key, ""), 0)

    def _called_budgets(self, name, input, options) -> dict:
        """options for a called run, with this run's budgets as ceilings: requests and bytes
        get what is left of this run's budget, items this run's item cap. Without it a
        pipeline step would run unbounded under a capped run. A lower budget asked for in
        options or as a run option in input is kept."""
        options = dict(options or {})
        actor = get_actor(name)
        own_props = actor.input_schema.get("properties", {}) if actor is not None else {}
        for key, limit in self.budgets.items():
            cap = limit if key == "maxItems" else max(1, limit - self._budget_used(key))
            own = options.get(key)
            if own is None and isinstance(input, dict) and key not in own_props:
                own = input.get(key)
            if own is None:
                options[key] = cap
            elif isinstance(own, int) and not isinstance(own, bool) and own > 0:
                options[key] = min(own, cap)
            # anything else is left as given, for the called run's validation to refuse
        return options

    def check_abort(self):
        if self.aborted:
            raise RunAborted("run timed out" if self.timed_out else "run aborted")

    @property
    def timed_out_chain(self) -> bool:
        """This run, or a run that called it, ran out of time."""
        return self.timed_out or (self.parent is not None and self.parent.timed_out_chain)

    def call_actor(self, name, input=None, *, options=None, timeout_secs=None) -> dict:
        """Run another actor now, in this process, and return its final run record.

        The called run gets its own run id and unnamed storages (read its items with
        ctx.storage.dataset(rec["defaultDatasetId"])), the same loopback switch and log
        stream, and never outlives this run's timeout. Aborting this run stops it too.
        A called run that fails is returned, not raised: check rec["status"]."""
        self.check_abort()
        if self.depth + 1 > MAX_CALL_DEPTH:
            raise RuntimeError(f"call_actor: nesting deeper than {MAX_CALL_DEPTH} runs")
        if timeout_secs is not None:
            options = dict(options or {}, timeoutSecs=timeout_secs)
        rec = prepare_run(name, input, self.storage, options=self._called_budgets(name, input, options))
        rec["parentRunId"] = self.run_id
        self.storage.save_run(rec)
        with self._rec_lock:
            self.record.setdefault("childRunIds", []).append(rec["id"])
        self.log.info(f"call {rec['actId']}: run {rec['id']} started")
        rec = execute_run(rec, self.storage, allow_loopback=self.allow_loopback,
                          log_stream=self.log_stream, parent=self)
        self.stats["calledRuns"] = self.stats.get("calledRuns", 0) + 1
        child = rec.get("stats") or {}
        for key, stat in CALLED_USAGE.items():
            used = child.get(BUDGETS[key], 0) + child.get(stat, 0)
            if used:
                self.stats[stat] = self.stats.get(stat, 0) + used
        self.log.info(f"call {rec['actId']}: run {rec['id']} {rec['status']} items={rec['itemCount']}"
                      + (f" ({rec['statusMessage']})" if rec["status"] != "SUCCEEDED" else ""))
        self._save_record()
        self.check_abort()           # this run's own timeout or abort wins over carrying on
        return rec

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
                if self.abort_event.is_set() and not self.budget_hit:
                    rec["status"] = "TIMING-OUT" if self.timed_out else "ABORTING"
                rec["itemCount"] = self.dataset.count()
                rec["stats"] = dict(self.stats)
                self.storage.save_run(rec)

    def mask(self, text) -> str:
        """text with this run's secret input values replaced by [secret], as in the log.
        Everything stored on the run record (status message, failures) goes through it,
        since the record is served by the API, MCP and webhooks."""
        return mask_secrets(text, self.log.secrets)

    def set_status_message(self, text) -> None:
        """Tell whoever watches the run what it is doing ("12/40 boards read"). Stored on
        the run record at once, shown by `ww-scraper runs`, the API and the console."""
        text = self.redact_text(self.mask(text))[:STATUS_MESSAGE_MAX]
        self.record["statusMessage"] = text
        self._save_record()

    def request_abort(self, timed_out: bool = False) -> None:
        if timed_out and not self.abort_event.is_set():
            self.timed_out = True
        self.abort_event.set()
        self._save_record()

    # -- actor state ------------------------------------------------------------------
    def use_state(self, default=None, key: str = STATE_KEY, persist_secs: float = STATE_PERSIST_SECS) -> dict:
        """A dict the actor may change freely. It is saved to the run's key-value store
        under `key` every persist_secs seconds and when the run ends in any status
        (aborted, timed out or failed too), and loaded back when the run is resumed."""
        if self._state is None:
            self._state_key = key
            saved = self.kv.get(key)
            self._state = saved if isinstance(saved, dict) else dict(default or {})
            if persist_secs and persist_secs > 0:
                self._schedule_persist(float(persist_secs))
        return self._state

    def _schedule_persist(self, every: float) -> None:
        def tick():
            self.persist_state()
            self._schedule_persist(every)
        self._state_timer = threading.Timer(every, tick)
        self._state_timer.daemon = True
        self._state_timer.start()

    def persist_state(self) -> None:
        if self._state is None:
            return
        for _ in range(5):
            try:
                data = json.loads(json.dumps(self._state, default=str))
                break
            except RuntimeError:             # changed by the actor while copying: try again
                time.sleep(0.01)
        else:
            return
        self.kv.set(self._state_key, data)

    def _stop_state(self) -> None:
        if self._state_timer is not None:
            self._state_timer.cancel()
        self.persist_state()

    def push_data(self, items) -> int:
        """Store items. With a dataset_schema on the actor, every item is checked first and
        one invalid item rejects the whole push (SchemaValidationError)."""
        if isinstance(items, dict):
            items = [items]
        fields = (self.actor.dataset_schema or {}).get("fields") if self.actor is not None else None
        if fields:
            from .schema import SchemaValidationError, check
            errors = [e for i, it in enumerate(items) for e in check(fields, it, f"item[{i}]")]
            if errors:
                self.stats["invalidItems"] = self.stats.get("invalidItems", 0) + len(items)
                raise SchemaValidationError(errors)
        if "maxItems" in self.budgets:
            items = list(items)[:max(0, self.budgets["maxItems"] - self.stats["items"])]
        if self.redact:
            items = [redact_obj(i) for i in items]
        n = self.dataset.push(items)
        self.stats["items"] += n
        return n

    def redact_text(self, text):
        return redact_pii(text) if self.redact else text

    def set_value(self, key, value, content_type=None):
        schema = self.actor.kv_schema if self.actor is not None else None
        if schema:
            from .schema import SchemaValidationError, check_record
            ctype = content_type or ("application/octet-stream" if isinstance(value, bytes)
                                     else "text/plain" if isinstance(value, str) else "application/json")
            errors = check_record(schema, key, value, ctype)
            if errors:
                raise SchemaValidationError(errors)
        self.kv.set(key, value, content_type)

    def record_failure(self, url, reason):
        self.stats["failed"] += 1
        if len(self.failures) < 1000:
            self.failures.append({"url": self.mask(url), "reason": self.mask(reason)})
        self.log.warning(f"{url}: {reason}")

    def crawler(self, **kwargs):
        from .crawler import PoliteCrawler
        min_interval = kwargs.pop("min_interval", self.input.get("minIntervalSecs", 1.0))
        crawler = PoliteCrawler(self, min_interval=min_interval, **kwargs)
        crawler.default_concurrency = int(self.input.get("maxConcurrency", 1) or 1)
        crawler.autoscale = bool(self.input.get("autoscaleConcurrency", False))
        render = self.input.get("render", False)
        crawler.render = "auto" if render == "auto" else bool(render)
        inp = self.input
        crawler.browser = {"waitForSelector": inp.get("waitForSelector") or None,
                           "actions": inp.get("browserActions") or None,
                           "screenshot": inp.get("screenshot") or None,
                           "format": inp.get("screenshotFormat") or "png",
                           "pdf": bool(inp.get("pdf"))}
        if any(crawler.browser[k] for k in ("waitForSelector", "actions", "screenshot", "pdf")) and not render:
            crawler.render = True
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
                options=None, task_name=None):
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
    secrets = secret_fields(actor.input_schema)
    input = fill_secrets(actor, input)
    if prev:
        opts = dict(prev.get("options") or {}, **opts)
    clean, errors = validate_input(actor.input_schema, input, lang)
    if clean.get("browserActions"):
        from .render import validate_actions
        errors += validate_actions(clean["browserActions"])
    if "render" in clean and not (isinstance(clean["render"], bool) or clean["render"] == "auto"):
        errors.append('render: expected true, false or "auto"')      # 0 and 1 are not booleans here
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
        "taskName": task_name or prev.get("taskName"),
        "actVersion": actor.get_version(),
        "defaultDatasetId": run_id,
        "defaultKeyValueStoreId": run_id,
        "defaultRequestQueueId": run_id,
        "stats": {},
        "itemCount": 0,
    }
    held = {k: clean.pop(k) for k in secrets if k in clean}
    from .schema import resolve_output
    rec["output"] = resolve_output(actor.output_schema, rec)
    storage.kv(run_id).set("INPUT", clean)
    if actor.dataset_schema:
        storage.dataset(run_id).set_schema(actor.dataset_schema)
    if actor.kv_schema:
        storage.kv(run_id).set_schema(actor.kv_schema)
    storage.save_run(rec)
    if held:
        _hold_secrets(run_id, held)  # in this process only, handed to the run when it starts
    return rec


def execute_run(rec, storage: Storage = None, *, allow_loopback=False, log_stream=None,
                abort_event=None, webhooks=None, parent=None, allow_hosts=None):
    """Run a prepared record to a terminal state and return the final record.
    allow_hosts: only these hosts (and their subdomains) are fetched by the run's crawler."""
    storage = storage or Storage()
    actor = get_actor(rec["actId"])
    clean = storage.kv(rec["id"]).get("INPUT", {})
    with _SECRETS_LOCK:
        held = _SECRETS.pop(rec["id"], {})
    for key, env in secret_fields(actor.input_schema).items():
        if key not in held and env and os.environ.get(env):
            held[key] = os.environ[env]
    clean.update(held)
    ctx = RunContext(rec["id"], actor, clean, storage, allow_loopback=allow_loopback,
                     log_stream=log_stream, abort_event=abort_event, record=rec, parent=parent,
                     allow_hosts=allow_hosts)
    ctx.log.secrets = [v for v in held.values() if isinstance(v, str) and len(v) >= 4]
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
        if webhooks:
            from .webhooks import dispatch
            start = "ACTOR.RUN.RESURRECTED" if rec.get("resumedFrom") else "ACTOR.RUN.CREATED"
            for line in dispatch(rec, webhooks, allow_loopback=allow_loopback, event=start):
                ctx.log.info(line)
        actor.run(ctx)
        if ctx.aborted:
            raise RunAborted("run aborted")
        status = "SUCCEEDED"
    except (RunAborted, KeyboardInterrupt):
        status = "ABORTED"
    except Exception as e:  # an actor bug must end as FAILED, never crash the host
        if ctx.timed_out_chain:
            status = "ABORTED"   # the timeout cut a request short; report it as the timeout
        else:
            status = "FAILED"
            rec["statusMessage"] = ctx.mask(f"{type(e).__name__}: {e}")[:STATUS_MESSAGE_MAX]
            ctx.log.error(traceback.format_exc())
    finally:
        if timer is not None:
            timer.cancel()
        ctx._stop_state()
        with _LIVE_LOCK:
            _LIVE.pop(rec["id"], None)
    if status == "ABORTED" and ctx.budget_hit and not ctx.timed_out_chain \
            and not (parent is not None and parent.aborted):
        status = "SUCCEEDED"         # a budget is a planned, clean stop
        rec["statusMessage"] = f"stopped at {ctx.budget_hit}"
    if status == "ABORTED":
        last = rec.get("statusMessage") or ""
        if ctx.timed_out_chain:
            status = "TIMED-OUT"
            rec["statusMessage"] = (f"timed out after {timeout or 0:g} s"
                                    + (f" (last status: {last})" if last else ""))[:STATUS_MESSAGE_MAX]
        else:
            rec["statusMessage"] = "aborted"
    if ctx.failures:
        ctx.set_value("FAILED_REQUESTS", ctx.failures)
    named = (rec.get("options") or {}).get("datasetName")
    if named:
        # items were already redacted on the way into the run's dataset
        n = storage.named_dataset(named).push(list(ctx.dataset.iter_items()))
        ctx.log.info(f"appended {n} item(s) to the named dataset {named}")
    with ctx._rec_lock:
        rec["status"] = status
        rec["finishedAt"] = _now()
        rec["stats"] = dict(ctx.stats, durationMillis=int((time.monotonic() - t0) * 1000))
        rec["itemCount"] = ctx.dataset.count()
        checks = (rec.get("options") or {}).get("checks")
        if checks:
            from .checks import evaluate
            rec["checks"] = evaluate(checks, rec, ctx.dataset.iter_items())
        storage.save_run(rec)
    ctx.log.info(f"run {rec['id']} finished: {rec['status']} items={rec['itemCount']}")
    for r in (rec.get("checks") or {}).get("results", []) if checks else []:
        if not r["passed"]:
            ctx.log.warning(f"check {r['check']} failed: expected {r['expected']}, got {r['actual']}")
    if webhooks:
        from .webhooks import dispatch
        for line in dispatch(rec, webhooks, allow_loopback=allow_loopback):
            ctx.log.info(line)
        if checks and not rec["checks"]["passed"]:
            for line in dispatch(rec, webhooks, allow_loopback=allow_loopback,
                                 event="ACTOR.RUN.CHECKS_FAILED"):
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
              options=None, timeout_secs=None, task_name=None, allow_hosts=None):
    """Validate, create and execute a run synchronously. Returns the final record.
    timeout_secs is a shortcut for options={"timeoutSecs": ...}."""
    storage = storage or Storage()
    if timeout_secs is not None:
        options = dict(options or {}, timeoutSecs=timeout_secs)
    rec = prepare_run(actor_name, input, storage, run_id=run_id, lang=lang, options=options,
                      task_name=task_name)
    return execute_run(rec, storage, allow_loopback=allow_loopback, log_stream=log_stream,
                       abort_event=abort_event, webhooks=webhooks, allow_hosts=allow_hosts)


def stderr_stream():
    return sys.stderr
