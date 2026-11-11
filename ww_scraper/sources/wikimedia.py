"""Wikimedia Commons source - commons.wikimedia.org MediaWiki API.

Official API (generator=search in the File namespace); author and license come from
each file's extmetadata. Assets are served from upload.wikimedia.org.
"""
from __future__ import annotations

import re

import requests

from ..models import Resource

API = "https://commons.wikimedia.org/w/api.php"
_TAG = re.compile(r"<[^>]+>")


def _ext(ext: dict, key: str) -> str:
    v = (ext.get(key) or {}).get("value", "")
    return _TAG.sub("", str(v)).strip()


class Wikimedia:
    name = "wikimedia"
    allow_hosts = frozenset({"commons.wikimedia.org", "upload.wikimedia.org"})

    def search(self, session: requests.Session, query: str, limit: int) -> list[Resource]:
        params = {
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": query, "gsrnamespace": "6",
            "gsrlimit": max(1, min(int(limit), 50)),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata|mediatype",
            "iiurlwidth": "1024",
        }
        r = session.get(API, params=params, timeout=30)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
        out = []
        for p in pages.values():
            ii = (p.get("imageinfo") or [None])[0]
            if not ii or ii.get("mediatype") not in (None, "BITMAP", "DRAWING"):
                continue
            ext = ii.get("extmetadata") or {}
            out.append(Resource(
                source=self.name,
                title=p.get("title", ""),
                page=ii.get("descriptionurl") or ii.get("descriptionshorturl") or "",
                url=ii.get("thumburl") or ii.get("url") or "",
                thumbnail=ii.get("thumburl") or "",
                author=_ext(ext, "Artist"),
                license=_ext(ext, "LicenseShortName"),
                license_url=_ext(ext, "LicenseUrl"),
                description=_ext(ext, "ImageDescription"),
            ))
        return out[:limit]
