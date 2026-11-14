"""The Metropolitan Museum of Art - Open Access API. CC0 public-domain artworks, no key.

Two-step official API: a search returns object IDs; each object carries its image and
attribution. Only public-domain (CC0) objects that actually have an image are returned -
museum-grade reference material you can use freely.
"""
from __future__ import annotations

import requests

from ..models import Resource

# /v1/search was retired on 2026-10-01 (HTTP 410); v1.1 is paginated with limit and offset
SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/"


class Met:
    name = "met"
    allow_hosts = frozenset({"images.metmuseum.org", "collectionapi.metmuseum.org"})

    def search(self, session: requests.Session, query: str, limit: int) -> list[Resource]:
        want = max(1, min(int(limit) * 3, 60))     # not every hit is public domain
        r = session.get(SEARCH, params={"q": query, "hasImages": "true", "limit": want, "offset": 0},
                        timeout=30)
        r.raise_for_status()
        ids = (r.json().get("objectIDs") or [])[:want]
        out: list[Resource] = []
        for oid in ids:
            if len(out) >= limit:
                break
            try:
                o = session.get(OBJECT + str(oid), timeout=30).json()
            except Exception:
                continue
            if not o.get("isPublicDomain") or not o.get("primaryImage"):
                continue
            out.append(Resource(
                source=self.name,
                title=o.get("title", ""),
                page=o.get("objectURL", ""),
                url=o.get("primaryImageSmall") or o.get("primaryImage", ""),
                thumbnail=o.get("primaryImageSmall", ""),
                author=o.get("artistDisplayName", ""),
                license="CC0 (Public Domain)",
                license_url="https://creativecommons.org/publicdomain/zero/1.0/",
                description=o.get("objectDate", ""),
                tags=[t for t in (o.get("department"), o.get("classification")) if t],
            ))
        return out
