"""robots-audit: read each origin's robots.txt the way ww-scraper itself obeys it (RFC 9309)
and report the groups, which group applies to a given crawler, Crawl-delay, sitemaps and
the decision (with the winning rule) for every test path."""
from __future__ import annotations

from urllib.parse import urljoin

from ..actor import MIN_INTERVAL, REDACT, START_URLS, Actor
from ..compliance import USER_AGENT, origin_of


class RobotsAudit(Actor):
    name = "robots-audit"
    title = "Robots.txt Audit"
    description = "Explain a site's robots.txt: groups, applied rules, sitemaps, per-path decisions."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "testPaths": {"type": "array", "items": {"type": "string"}, "default": ["/"],
                          "maxItems": 200, "description": "Paths to evaluate against the rules"},
            "userAgents": {"type": "array", "items": {"type": "string"}, "default": [],
                           "maxItems": 20,
                           "description": "Extra crawler tokens to evaluate (ww-scraper is always included)"},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        crawler = ctx.crawler()
        agents = [USER_AGENT] + [a for a in inp["userAgents"] if a.strip()]
        summary = {"origins": 0, "withRobots": 0, "blockedForUs": 0}
        for origin in sorted({origin_of(u) for u in inp["startUrls"]}):
            ctx.check_abort()
            entry = crawler.robots.get(origin + "/")
            summary["origins"] += 1
            item = {"origin": origin, "robotsUrl": origin + "/robots.txt", "status": entry.status,
                    "httpStatus": entry.http_status, "sitemaps": entry.sitemaps,
                    "groups": [], "ignoredLines": 0, "decisions": []}
            rules = entry.parser
            if rules is not None:
                summary["withRobots"] += 1
                item["groups"] = rules.describe()
                item["ignoredLines"] = rules.ignored_lines
            for agent in agents:
                token = agent.strip().split("/")[0]
                for path in inp["testPaths"]:
                    url = urljoin(origin + "/", path)
                    if origin_of(url) != origin:
                        continue
                    if rules is not None:
                        d = rules.decide(agent, url)
                    else:
                        allowed = entry.status == "missing"
                        d = {"allowed": allowed, "group": None,
                             "rule": f"robots.txt {entry.status}", "pattern": ""}
                    item["decisions"].append(dict(d, userAgent=token, path=path))
                    if agent is USER_AGENT and not d["allowed"]:
                        summary["blockedForUs"] += 1
            item["crawlDelay"] = entry.crawl_delay()
            ctx.push_data(item)
        ctx.set_value("OUTPUT", summary)
