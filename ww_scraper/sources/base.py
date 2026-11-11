"""Source contract. Every adapter is an official API or a robots-allowed public page,
and declares the hosts its assets may legitimately live on (used as the SSRF allowlist
during live verification)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import requests

from ..models import Resource


@runtime_checkable
class Source(Protocol):
    name: str
    allow_hosts: frozenset  # hosts verify.py will permit for this source's assets

    def search(self, session: requests.Session, query: str, limit: int) -> list[Resource]:
        """Return up to `limit` candidate Resources (unverified)."""
        ...
