"""Openverse source - api.openverse.org, anonymous access to CC / public-domain media.

Openverse exposes an official API and proxies thumbnails on api.openverse.org, so the
asset URL we return stays on the allowlisted host. License metadata is normalized to a
human-readable string (e.g. "CC BY 4.0", "CC0", "PDM").
"""
from __future__ import annotations

import requests

from ..models import Resource

API = "https://api.openverse.org/v1/images/"


class Openverse:
    name = "openverse"
    allow_hosts = frozenset({"api.openverse.org"})

    def search(self, session: requests.Session, query: str, limit: int) -> list[Resource]:
        page_size = max(1, min(int(limit), 50))
        r = session.get(API, params={"q": query, "page_size": page_size, "mature": "false"},
                        timeout=60)  # Openverse can be slow (~30s) under load
        r.raise_for_status()
        out = []
        for it in r.json().get("results", []):
            out.append(Resource(
                source=self.name,
                title=it.get("title") or "",
                page=it.get("foreign_landing_url") or it.get("url") or "",
                # thumbnail proxy sits on api.openverse.org (allowlisted); raw `url` may not
                url=it.get("thumbnail") or it.get("url") or "",
                thumbnail=it.get("thumbnail") or "",
                author=it.get("creator") or "",
                author_url=it.get("creator_url") or "",
                license=_license_label(it),
                license_url=it.get("license_url") or "",
                tags=[t.get("name") for t in (it.get("tags") or [])
                      if isinstance(t, dict) and t.get("name")],
            ))
        return out[:limit]


def _license_label(it: dict) -> str:
    lic = (it.get("license") or "").upper()
    ver = it.get("license_version") or ""
    if lic and lic not in ("CC0", "PDM") and ver:
        return f"CC {lic} {ver}"
    return lic
