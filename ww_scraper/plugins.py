"""Your own actors: one Python file with a subclass of ww_scraper.Actor.

    ww-scraper init price-watch          # writes price_watch.py, a working starting point
    ww-scraper run ./price_watch.py --input '{"startUrls": ["https://example.com/"]}'

A file actor gets the same RunContext as the built-in ones, so every fetch it makes through
ctx.crawler() is robots.txt-checked, rate-limited and SSRF-guarded. Files are loaded only
from the command line: the local HTTP API runs registered actors by name and never loads
a path it was sent.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from .actor import Actor

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")


class PluginError(ValueError):
    pass


def is_actor_path(value) -> bool:
    return isinstance(value, str) and value.endswith(".py")


def load_actor_file(path) -> Actor:
    """Import the file and return an instance of the one Actor subclass it defines."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise PluginError(f"actor file not found: {path}")
    spec = importlib.util.spec_from_file_location(f"ww_scraper_user_{p.stem}", p)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise PluginError(f"{p.name}: {type(e).__name__}: {e}") from e
    found = [v for v in vars(module).values()
             if isinstance(v, type) and issubclass(v, Actor) and v is not Actor
             and v.__module__ == module.__name__]
    if len(found) != 1:
        raise PluginError(f"{p.name}: expected exactly one Actor subclass, found {len(found)}")
    actor = found[0]()
    if not _NAME_RE.match(actor.name or ""):
        raise PluginError(f"{p.name}: actor name must be lowercase letters, digits and -, 2 to 40 characters")
    schema = actor.input_schema
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        raise PluginError(f"{p.name}: input_schema needs a 'properties' object")
    return actor


def register(actor: Actor) -> Actor:
    """Add a file actor to the registry of this process. Built-in names cannot be replaced."""
    from .actors import ACTORS, BUILTIN
    if actor.name in BUILTIN:
        raise PluginError(f"'{actor.name}' is a built-in actor; choose another name")
    ACTORS[actor.name] = actor
    return actor


TEMPLATE = '''"""{name}: describe in one sentence what this actor collects."""
from ww_scraper import Actor
from ww_scraper.actor import CONCURRENCY, MIN_INTERVAL, REDACT, RENDER, START_URLS


class {cls}(Actor):
    name = "{name}"
    title = "{title}"
    description = "Collect the title and the first heading of each page."
    input_schema = {{
        "type": "object",
        "required": ["startUrls"],
        "properties": {{
            "startUrls": START_URLS,
            "maxPages": {{"type": "integer", "default": 20, "minimum": 1, "maximum": 10000}},
            "maxDepth": {{"type": "integer", "default": 1, "minimum": 0, "maximum": 20}},
            "minIntervalSecs": MIN_INTERVAL,
            "maxConcurrency": CONCURRENCY,
            "render": RENDER,
            "redactPii": REDACT,
        }},
    }}

    def run(self, ctx):
        def on_page(res, req, doc):
            # res: the fetch (status, headers, error); doc: the parsed page, or None
            if doc is None:
                if res.error:
                    ctx.record_failure(req["url"], res.error)
                return
            ctx.push_data({{
                "url": res.final_url,
                "title": doc.title,
                "h1": (doc.headings.get("h1") or [""])[0],
                "words": doc.word_count,
            }})

        # ctx.crawler() is the only way to fetch: robots.txt, rate limit and SSRF guard included
        ctx.crawler().crawl(ctx.input["startUrls"], on_page,
                            max_pages=ctx.input["maxPages"], max_depth=ctx.input["maxDepth"])
        ctx.set_value("OUTPUT", {{"pages": ctx.stats["pages"]}})
'''


def scaffold(name: str, directory=".") -> Path:
    if not _NAME_RE.match(name):
        raise PluginError("name: lowercase letters, digits and -, 2 to 40 characters, starting with a letter")
    target = Path(directory) / (name.replace("-", "_") + ".py")
    if target.exists():
        raise PluginError(f"{target} already exists")
    cls = "".join(part.capitalize() for part in name.split("-"))
    target.write_text(TEMPLATE.format(name=name, cls=cls, title=name.replace("-", " ").title()),
                      encoding="utf-8")
    return target
