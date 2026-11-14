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
        from .. import search  # late import: ww_scraper/__init__ imports the actor layer lazily

        inp = ctx.input
        unknown = [s for s in inp["sources"] if s not in SOURCES]
        if unknown:
            raise ValueError(f"unknown sources: {', '.join(unknown)}")
        session = make_session(max(1.0, inp["minIntervalSecs"]))
        report = {}
        items = search(inp["query"], sources=inp["sources"] or None, limit=inp["limit"],
                       verify=inp["verify"], session=session, report=report)
        for name, r in report.items():
            if r.get("error"):
                ctx.log.warning(f"{name}: failed: {r['error']}")
            elif r.get("skipped"):
                ctx.log.warning(f"{name}: skipped: {r['skipped']}")
            elif r["found"] and not r["verified"]:
                ctx.log.warning(f"{name}: {r['found']} found, none passed the live check")
        ctx.set_value("OUTPUT", {"query": inp["query"], "items": len(items), "sources": report})
        ran = {n: r for n, r in report.items() if not r.get("skipped")}
        if not items and ran and all(r.get("error") for r in ran.values()):
            raise RuntimeError("every source failed: " + "; ".join(f"{n}: {r['error']}" for n, r in ran.items()))
        ctx.push_data([r.to_dict() for r in items])
        ctx.log.info(f"{len(items)} item(s) collected")
