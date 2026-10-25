"""Lustro - compliant, open-source collector and local mirror of Apify-style primitives.

Public API:
  search(query, sources=..., limit=..., verify=...) -> list[Resource]   (v0.1 media)
  run_actor(name, input, storage=None, ...) -> run record                (v0.2 actors)

Every fetch is robots.txt-checked, rate-limited per host and SSRF-guarded.
"""
from __future__ import annotations

from .models import Resource
from .sources import SOURCES, get_source

from ._version import __version__

__all__ = ["Resource", "search", "run_actor", "SOURCES", "get_source", "__version__"]


def run_actor(*args, **kwargs):
    """Run an actor synchronously; see lustro.actor.run_actor."""
    from .actor import run_actor as _run
    return _run(*args, **kwargs)


def search(query, sources=None, limit=12, verify=True, session=None):
    """Search one or more compliant sources and return verified Resource items.

    query   : free-text query
    sources : list of source names (default: every registered source)
    limit   : max results per source
    verify  : fetch each asset and confirm it is a live image (recommended)
    session : optional requests.Session (a polite one is created if omitted)
    """
    from .compliance import make_session
    from .verify import verify_resource

    sess = session or make_session()
    names = sources or list(SOURCES)
    out = []
    for name in names:
        src = get_source(name)
        if src is None:
            continue
        try:
            items = src.search(sess, query, limit)
        except Exception:
            continue  # one source failing must not sink the whole search
        allow = getattr(src, "allow_hosts", None)
        for r in items:
            if verify and not verify_resource(sess, r, allowlist=allow):
                continue
            out.append(r)
    return out
