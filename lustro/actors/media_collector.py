"""media-collector: the v0.1 source collectors (Openverse, Wikimedia, Pexels, Met) as an
actor. Official APIs only; each result is live-verified and carries its license."""
from __future__ import annotations

from ..actor import MIN_INTERVAL, REDACT, Actor
from ..compliance import make_session
from ..sources import SOURCES


class MediaCollector(Actor):
    name = "media-collector"
    title = "Media Collector"
    description = "Search licensed media via official APIs; every item is live-verified."
    input_schema = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
            "sources": {"type": "array", "items": {"type": "string", "enum": list(SOURCES)},
                        "default": [], "description": "default: all sources"},
            "limit": {"type": "integer", "default": 12, "minimum": 1, "maximum": 50},
            "verify": {"type": "boolean", "default": True},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        from .. import search  # late import: lustro/__init__ imports the actor layer lazily

        inp = ctx.input
        unknown = [s for s in inp["sources"] if s not in SOURCES]
        if unknown:
            raise ValueError(f"unknown sources: {', '.join(unknown)}")
        session = make_session(max(1.0, inp["minIntervalSecs"]))
        items = search(inp["query"], sources=inp["sources"] or None, limit=inp["limit"],
                       verify=inp["verify"], session=session)
        ctx.push_data([r.to_dict() for r in items])
        ctx.log.info(f"{len(items)} item(s) collected")
