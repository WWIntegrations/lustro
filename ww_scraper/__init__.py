"""ww-scraper - compliant, open-source collector and local scraping platform primitives.

Public API:
  search(query, sources=..., limit=..., verify=...) -> list[Resource]   (v0.1 media)
  run_actor(name, input, storage=None, ...) -> run record                (v0.2 actors)

Every fetch is robots.txt-checked, rate-limited per host and SSRF-guarded.
"""
from __future__ import annotations

from .models import Resource
from .sources import SOURCES, get_source

from ._version import __version__

__all__ = ["Actor", "Resource", "search", "run_actor", "SOURCES", "get_source", "__version__"]


def __getattr__(name):
    if name == "Actor":                      # late import: actor.py imports this package
        from .actor import Actor
        return Actor
    raise AttributeError(name)


def run_actor(*args, **kwargs):
    """Run an actor synchronously; see ww_scraper.actor.run_actor."""
    from .actor import run_actor as _run
    return _run(*args, **kwargs)


def search(query, sources=None, limit=12, verify=True, session=None, report=None):
    """Search one or more compliant sources and return verified Resource items.

    query   : free-text query
    sources : list of source names (default: every registered source)
    limit   : max results per source
    verify  : fetch each asset and confirm it is a live image (recommended)
    session : optional requests.Session (a polite one is created if omitted)
    report  : optional dict, filled per source with {"found", "verified"} and, when the
              source failed or could not run, "error" or "skipped". One source failing never
              sinks the others, and with a report it never fails silently either.
    """
    from . import verify as _verify
    from .compliance import make_session

    sess = session or make_session()
    names = sources or list(SOURCES)
    out = []
    report = {} if report is None else report
    for name in names:
        src = get_source(name)
        if src is None:
            report[name] = {"found": 0, "verified": 0, "skipped": "unknown source"}
            continue
        why = getattr(src, "unavailable", lambda: None)()
        if why:
            report[name] = {"found": 0, "verified": 0, "skipped": why}
            continue
        try:
            items = src.search(sess, query, limit)
        except Exception as e:  # noqa: BLE001 - recorded, and the other sources still run
            report[name] = {"found": 0, "verified": 0, "error": f"{type(e).__name__}: {e}"[:300]}
            continue
        allow = getattr(src, "allow_hosts", None)
        kept = [r for r in items if not verify or _verify.verify_resource(sess, r, allowlist=allow)]
        report[name] = {"found": len(items), "verified": len(kept)}
        out += kept
    return out
