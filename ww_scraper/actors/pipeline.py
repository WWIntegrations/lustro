"""pipeline: run actors one after another, each step's URLs feeding the next step.

    {"steps": [
      {"actor": "sitemap", "input": {"startUrls": ["https://example.com/"]}},
      {"actor": "seo-audit", "input": {"maxPages": 50}, "urlsFrom": "url", "into": "startUrls"}
    ]}

Each step is an ordinary run started with ctx.call_actor(), so it keeps every rule of its
actor. The items of the last step become the pipeline's items; the record of every step
is in OUTPUT. Save a pipeline as a task to run it by name.
"""
from __future__ import annotations

from ..actor import REDACT, Actor, get_actor, split_options, validate_input

MAX_URLS = 100


def _urls(items, field, limit):
    out, seen = [], set()
    for it in items:
        v = it.get(field)
        for u in (v if isinstance(v, list) else [v]):
            if isinstance(u, str) and u.lower().startswith(("http://", "https://")) and u not in seen:
                seen.add(u)
                out.append(u)
                if len(out) >= limit:
                    return out
    return out


class Pipeline(Actor):
    name = "pipeline"
    title = "Pipeline"
    description = ("Runs actors one after another; URLs found by one step become the start URLs "
                   "of the next. For example sitemap then seo-audit.")
    input_schema = {
        "type": "object",
        "required": ["steps"],
        "properties": {
            "steps": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 10,
                      "description": "[{actor, input, urlsFrom?, into?, maxUrls?}]: urlsFrom names the "
                                     "field of the previous step's items whose URLs go into the input "
                                     "field `into` (default startUrls)"},
            "stopOnFailure": {"type": "boolean", "default": True,
                              "description": "Stop when a step does not succeed"},
            "redactPii": dict(REDACT, description="Passed to every step that does not set its own"),
        },
    }

    @staticmethod
    def _input(step, redact):
        data = dict(step.get("input") or {})
        if "redactPii" in get_actor(step["actor"]).input_schema.get("properties", {}):
            data.setdefault("redactPii", redact)
        return data

    def run(self, ctx):
        redact = ctx.input["redactPii"]
        steps = ctx.input["steps"]
        for i, step in enumerate(steps):
            if not isinstance(step.get("actor"), str) or get_actor(step["actor"]) is None:
                raise ValueError(f"steps[{i}].actor: unknown actor {step.get('actor')!r}")
            if not isinstance(step.get("input", {}), dict):
                raise ValueError(f"steps[{i}].input must be an object")
            if step.get("urlsFrom") and i == 0:
                raise ValueError("steps[0] has no previous step to take urlsFrom")
            probe = self._input(step, redact)
            if step.get("urlsFrom"):
                probe[step.get("into") or "startUrls"] = ["https://example.com/"]
            actor = get_actor(step["actor"])
            probe, _, errors = split_options(actor, probe)
            errors += validate_input(actor.input_schema, probe)[1]
            if errors:                 # refuse the whole pipeline before any step runs
                raise ValueError(f"steps[{i}].input: " + "; ".join(errors))
        done, prev = [], None
        for i, step in enumerate(steps):
            data = self._input(step, redact)
            if step.get("urlsFrom"):
                items = ctx.storage.dataset(prev["defaultDatasetId"]).items()
                limit = int(step.get("maxUrls") or MAX_URLS)
                urls = _urls(items, step["urlsFrom"], max(1, min(limit, MAX_URLS)))
                if not urls:
                    ctx.log.warning(f"step {i + 1}: no URLs in field {step['urlsFrom']!r} of the previous step")
                    done.append({"step": i + 1, "actor": step["actor"], "status": "SKIPPED", "runId": None})
                    break
                data[step.get("into") or "startUrls"] = urls
            ctx.set_status_message(f"step {i + 1}/{len(steps)}: {step['actor']}")
            prev = ctx.call_actor(step["actor"], data)
            done.append({"step": i + 1, "actor": step["actor"], "status": prev["status"],
                         "runId": prev["id"], "items": prev["itemCount"],
                         "statusMessage": prev.get("statusMessage") or ""})
            if prev["status"] != "SUCCEEDED" and ctx.input["stopOnFailure"]:
                ctx.set_value("OUTPUT", {"steps": done})
                raise RuntimeError(f"step {i + 1} ({step['actor']}) ended {prev['status']}: "
                                   f"{prev.get('statusMessage')}")
        if prev is not None and done[-1].get("runId") == prev["id"]:
            strict = ctx.redact
            ctx.redact = False             # the step already applied its own redaction
            try:
                for chunk in _chunks(ctx.storage.dataset(prev["defaultDatasetId"]).iter_items()):
                    ctx.push_data(chunk)
            finally:
                ctx.redact = strict
        ctx.set_status_message(f"{len(done)} step(s) done")
        ctx.set_value("OUTPUT", {"steps": done})


def _chunks(it, n=500):
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
