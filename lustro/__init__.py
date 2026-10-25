"""Lustro - compliant, open-source media collector.

Public API: `search(query, sources=..., limit=..., verify=...)` -> list[Resource].
Every returned Resource is live-verified (unless verify=False) and carries license +
provenance. Verification is SSRF-gated by each source's declared allow_hosts.
"""
from __future__ import annotations

from .models import Resource
from .sources import SOURCES, get_source

__version__ = "0.1.0"
__all__ = ["Resource", "search", "SOURCES", "get_source", "__version__"]


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
