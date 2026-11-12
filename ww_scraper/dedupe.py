"""Duplicate pages: skip URLs that lead to a page the crawl already has.

Sites reach one page through many URLs: a print view (?view=print), the same parameters in
another order, a trailing slash, an old path that redirects. The request queue already
drops fragments and tracking parameters (storage.normalize_url). This guard goes further,
and only on evidence the site itself gives:

- Every fetched page registers its request URL, its final URL after redirects and its
  rel=canonical. A later URL whose key is already registered is skipped before it is
  fetched, and a redirect to a registered page stops before the second hop.
- rel=canonical is trusted only when it keeps the page's path (up to a trailing slash) and
  its host. A site that points every page at its home page (a common mistake) therefore
  never makes pages look like duplicates of each other.
- From such canonicals the guard learns, per host, which query parameters do not change
  the page (?view=print -> canonical without view) and whether the trailing slash matters.
  A rule is used only after LEARN_AFTER agreeing pages and never after one disagreeing
  page (a canonical that keeps the parameter), so one odd page cannot hide real content.
"""
from __future__ import annotations

import threading
from urllib.parse import urlsplit, urlunsplit

from .storage import _param_name, normalize_url

LEARN_AFTER = 3


def _split(url: str):
    p = urlsplit(normalize_url(url))
    params = [part for part in p.query.split("&") if part]
    return p, params


def _strip_slash(path: str) -> str:
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


class DuplicateGuard:
    def __init__(self, learn_after: int = LEARN_AFTER):
        self.learn_after = learn_after
        self._seen: set = set()
        self._lock = threading.Lock()
        # host -> {name: [agree, disagree]}; "/" is the trailing-slash rule
        self._evidence: dict = {}

    # -- keys -------------------------------------------------------------------------
    def _learned(self, host: str, name: str) -> bool:
        agree, disagree = self._evidence.get(host, {}).get(name, (0, 0))
        return disagree == 0 and agree >= self.learn_after

    def key(self, url: str) -> str:
        p, params = _split(url)
        host = p.netloc
        with self._lock:
            kept = [x for x in params if not self._learned(host, _param_name(x))]
            path = _strip_slash(p.path) if self._learned(host, "/") else p.path
        return urlunsplit((p.scheme, host, path, "&".join(kept), ""))

    def seen(self, url: str) -> bool:
        k = self.key(url)
        with self._lock:
            return k in self._seen

    # -- after a fetch ----------------------------------------------------------------
    def register(self, request_url: str, final_url: str = "", canonical: str = "") -> bool:
        """Record a fetched page. Returns True when it is a duplicate of a page registered
        earlier (same final URL, or a trusted canonical already seen)."""
        final_url = final_url or request_url
        trusted = canonical if canonical and self._trust(final_url, canonical) else ""
        if trusted:
            self._learn(final_url, trusted)
        keys = {self.key(u) for u in (request_url, final_url, trusted) if u}
        page_keys = {self.key(final_url)} | ({self.key(trusted)} if trusted else set())
        with self._lock:
            duplicate = bool(page_keys & self._seen)
            self._seen |= keys
        return duplicate

    @staticmethod
    def _trust(url: str, canonical: str) -> bool:
        u, c = urlsplit(normalize_url(url)), urlsplit(normalize_url(canonical))
        return (u.scheme, u.netloc) == (c.scheme, c.netloc) and _strip_slash(u.path) == _strip_slash(c.path)

    def _learn(self, url: str, canonical: str) -> None:
        u, uparams = _split(url)
        c, cparams = _split(canonical)
        host = u.netloc
        cnames = {_param_name(x) for x in cparams}
        with self._lock:
            ev = self._evidence.setdefault(host, {})
            for x in uparams:
                name = _param_name(x)
                agree, disagree = ev.get(name, (0, 0))
                if x in cparams:
                    ev[name] = (agree, disagree + 1)      # the canonical keeps it: it matters
                elif name not in cnames:
                    ev[name] = (agree + 1, disagree)      # dropped by the canonical
            if u.path != c.path:
                # the two differ only by the trailing slash (_trust checked the rest), so the
                # site says both forms are one page; a self-canonical says nothing either way
                agree, disagree = ev.get("/", (0, 0))
                ev["/"] = (agree + 1, disagree)

    def stats(self) -> dict:
        with self._lock:
            learned = {h: sorted(n for n in ev if self._learned(h, n)) for h, ev in self._evidence.items()}
        return {h: names for h, names in learned.items() if names}
