"""Data model + sanitizers. Titles/authors from any source are untrusted input."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


def sanitize(s, limit: int = 300) -> str:
    """Trim untrusted text: non-printable -> space, collapse whitespace, cap length."""
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    s = " ".join(s.split())
    return s[:limit]


def clean_url(u) -> str:
    """Return u only if it is a safe, bounded http(s) URL, else empty string."""
    if not isinstance(u, str) or len(u) > 2000:
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in u):
        return ""
    if not (u.startswith("http://") or u.startswith("https://")):
        return ""
    return u


@dataclass
class Resource:
    """One media asset with provenance and license. Dimensions/content_type are
    filled by live verification; `verified_at` is set only once the asset is confirmed."""
    source: str
    title: str
    page: str = ""            # work/landing page (verifiable)
    url: str = ""             # direct asset URL (verified live)
    thumbnail: str = ""
    author: str = ""
    author_url: str = ""
    license: str = ""
    license_url: str = ""
    tags: list = field(default_factory=list)
    description: str = ""
    width: int = 0
    height: int = 0
    content_type: str = ""
    verified_at: str = ""

    def __post_init__(self):
        self.source = sanitize(self.source, 60)
        self.title = sanitize(self.title, 200)
        self.author = sanitize(self.author, 120)
        self.license = sanitize(self.license, 120)
        self.description = sanitize(self.description, 400)
        self.tags = [sanitize(t, 40) for t in (self.tags if isinstance(self.tags, list) else [])][:25]
        for f in ("page", "url", "thumbnail", "author_url", "license_url"):
            setattr(self, f, clean_url(getattr(self, f)))

    def to_dict(self) -> dict:
        return asdict(self)
