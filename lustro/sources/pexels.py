"""Pexels source - api.pexels.com. Requires PEXELS_API_KEY in the environment.

Photos come under the Pexels License (free to use; attribution appreciated). The key is
read from the environment only - never hard-coded. With no key set, this source yields
nothing instead of erroring, so a key-less install still works with the other sources.
"""
from __future__ import annotations

import os

import requests

from ..models import Resource

API = "https://api.pexels.com/v1/search"


class Pexels:
    name = "pexels"
    allow_hosts = frozenset({"images.pexels.com", "api.pexels.com", "www.pexels.com"})

    def search(self, session: requests.Session, query: str, limit: int) -> list[Resource]:
        key = os.environ.get("PEXELS_API_KEY")
        if not key:
            return []
        r = session.get(API, params={"query": query, "per_page": max(1, min(int(limit), 80))},
                        headers={"Authorization": key}, timeout=30)
        r.raise_for_status()
        out = []
        for ph in r.json().get("photos", []):
            src = ph.get("src") or {}
            out.append(Resource(
                source=self.name,
                title=ph.get("alt") or f"Photo by {ph.get('photographer', '')}".strip(),
                page=ph.get("url") or "",
                url=src.get("large") or src.get("medium") or src.get("original") or "",
                thumbnail=src.get("medium") or src.get("small") or "",
                author=ph.get("photographer") or "",
                author_url=ph.get("photographer_url") or "",
                license="Pexels License",
                license_url="https://www.pexels.com/license/",
            ))
        return out[:limit]
