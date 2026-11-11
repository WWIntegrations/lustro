"""robots.txt rules following RFC 9309.

- Groups: one or more `User-agent` lines followed by rules. Groups naming the same
  token are merged. A crawler obeys the group whose token equals its own product token
  (case-insensitive); if there is none, the `*` group; if there is none, no rules.
- Rules: `Allow` / `Disallow` path patterns with `*` (any run of characters) and a
  trailing `$` (end of the URL path). The longest matching pattern wins; on a tie
  `Allow` wins. An empty pattern is not a rule. `/robots.txt` is always fetchable.
- Also read: `Sitemap` (anywhere in the file) and the non-standard `Crawl-delay`.

Matching is a single left-to-right scan with `str.find` per pattern, so it is linear in
the URL length for each rule and has no regular-expression backtracking.
"""
from __future__ import annotations

from urllib.parse import quote, unquote_to_bytes, urlsplit

_SAFE = "/?=&*$:@!,;+~'()-._"
MAX_LINES = 50_000          # generous: RFC 9309 asks parsers to accept at least 500 KiB
MAX_RULES_PER_GROUP = 5_000


def _norm(path: str) -> str:
    """Canonical percent-encoding so `/a%3Cd` and `/a%3cd` and `/a<d` compare equal."""
    return quote(unquote_to_bytes(path), safe=_SAFE)


def product_token(user_agent: str) -> str:
    """`ww-scraper/0.3 (+https://...)` -> `ww-scraper`."""
    return (user_agent or "").strip().split("/")[0].split(" ")[0].lower()


def pattern_matches(pattern: str, path: str) -> bool:
    """True when the robots pattern matches the start of path (whole path if it ends in $)."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    parts = pattern.split("*")
    if not path.startswith(parts[0]):
        return False
    pos = len(parts[0])
    if len(parts) == 1:
        return pos == len(path) if anchored else True
    for part in parts[1:-1]:
        idx = path.find(part, pos)
        if idx < 0:
            return False
        pos = idx + len(part)
    last = parts[-1]
    if anchored:
        return len(path) - len(last) >= pos and path.endswith(last)
    return path.find(last, pos) >= 0


class Group:
    def __init__(self):
        self.agents: list = []
        self.rules: list = []       # (allow: bool, pattern: str)
        self.crawl_delay = None

    def describe(self) -> dict:
        return {"userAgents": list(self.agents),
                "allow": [p for a, p in self.rules if a],
                "disallow": [p for a, p in self.rules if not a],
                "crawlDelay": self.crawl_delay}


class RobotsRules:
    """Parsed robots.txt. The method names match urllib.robotparser where they overlap."""

    def __init__(self, text: str = ""):
        self.groups: dict = {}      # token -> Group (merged)
        self.sitemaps: list = []
        self.ignored_lines = 0
        self.parse(text.splitlines() if isinstance(text, str) else text)

    def parse(self, lines) -> None:
        current: list = []          # groups the following rules belong to
        seen_rule = False
        for n, raw in enumerate(lines):
            if n >= MAX_LINES:
                break
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if seen_rule:
                    current, seen_rule = [], False
                token = value.lower()
                if token:
                    current.append(self.groups.setdefault(token, Group()))
                    if token not in current[-1].agents:
                        current[-1].agents.append(token)
            elif key in ("allow", "disallow"):
                seen_rule = True
                if not current:
                    self.ignored_lines += 1     # a rule before any User-agent line
                    continue
                if value:
                    pattern = _norm(value if value.startswith(("/", "*")) else "/" + value)
                    for g in current:
                        if len(g.rules) < MAX_RULES_PER_GROUP:
                            g.rules.append((key == "allow", pattern))
            elif key == "crawl-delay":
                seen_rule = True
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0 and delay == delay and delay != float("inf"):
                    for g in current:
                        g.crawl_delay = delay
            elif key == "sitemap":
                if value and value not in self.sitemaps and len(self.sitemaps) < 1000:
                    self.sitemaps.append(value)
            else:
                self.ignored_lines += 1

    # -- selection ---------------------------------------------------------------------
    def group_for(self, user_agent: str):
        """(token, Group) that applies to user_agent, or (None, None)."""
        token = product_token(user_agent)
        if token and token in self.groups:
            return token, self.groups[token]
        if "*" in self.groups:
            return "*", self.groups["*"]
        return None, None

    def decide(self, user_agent: str, url: str) -> dict:
        """Explain the decision: {"allowed", "group", "rule", "pattern"}."""
        parts = urlsplit(url)
        path = _norm((parts.path or "/") + (("?" + parts.query) if parts.query else ""))
        if path == "/robots.txt":
            return {"allowed": True, "group": None, "rule": "always", "pattern": "/robots.txt"}
        token, group = self.group_for(user_agent)
        if group is None:
            return {"allowed": True, "group": None, "rule": "no-group", "pattern": ""}
        best = None                 # (length, allow, pattern)
        for allow, pattern in group.rules:
            if pattern_matches(pattern, path):
                cand = (len(pattern), allow, pattern)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        if best is None:
            return {"allowed": True, "group": token, "rule": "no-match", "pattern": ""}
        return {"allowed": best[1], "group": token,
                "rule": "allow" if best[1] else "disallow", "pattern": best[2]}

    # -- urllib.robotparser-compatible surface ----------------------------------------------
    def can_fetch(self, user_agent: str, url: str) -> bool:
        return self.decide(user_agent, url)["allowed"]

    def crawl_delay(self, user_agent: str):
        _, group = self.group_for(user_agent)
        return group.crawl_delay if group is not None else None

    def site_maps(self):
        return list(self.sitemaps) or None

    def describe(self) -> list:
        return [g.describe() for g in self.groups.values()]
