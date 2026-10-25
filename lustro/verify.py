"""Live verification: confirm a Resource's asset is a real, fetchable image.

Fetches with a byte cap and a decompression-bomb guard, following redirects manually so
every hop is checked against the caller-supplied host allowlist (SSRF-safe).
"""
from __future__ import annotations

import datetime
import io
from urllib.parse import urlparse

from .compliance import MAX_BYTES, TIMEOUT, host_allowed, is_public_address

MAX_PIXELS = 40_000_000  # ~8000x5000; larger decoded area is treated as a bomb

_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _fetch_capped(session, url, allowlist, max_hops=5):
    """Follow up to max_hops redirects, enforcing allowlist + public-address on each,
    and return (content_type, bytes) for a 200 image response, else None."""
    current = url
    for _ in range(max_hops):
        host = (urlparse(current).hostname or "")
        if allowlist is not None and not host_allowed(host, allowlist):
            return None
        if not is_public_address(host):
            return None
        resp = session.get(current, stream=True, allow_redirects=False, timeout=TIMEOUT)
        try:
            if resp.status_code in _REDIRECT_CODES:
                current = resp.headers.get("Location", "")
                if not current:
                    return None
                continue
            if resp.status_code != 200:
                return None
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                return None
            buf = io.BytesIO()
            total = 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    return None
                buf.write(chunk)
            return ctype, buf.getvalue()
        finally:
            resp.close()
    return None


def verify_resource(session, resource, allowlist=None) -> bool:
    """Confirm the asset is a live, decodable image within size limits; on success fill
    width/height/content_type/verified_at and return True."""
    from PIL import Image

    url = resource.url or resource.thumbnail
    if not url:
        return False
    got = _fetch_capped(session, url, allowlist)
    if not got:
        return False
    ctype, data = got
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:
        return False
    if w <= 0 or h <= 0 or (w * h) > MAX_PIXELS:
        return False
    resource.width, resource.height = w, h
    resource.content_type = ctype
    resource.verified_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return True
