"""Registered actors. Each one runs only through RunContext + PoliteCrawler, so every
fetch is robots.txt-checked, rate-limited per host and SSRF-guarded."""
from __future__ import annotations

from .change_monitor import ChangeMonitor
from .feed_reader import FeedReader
from .link_checker import LinkChecker
from .llms_txt import LlmsTxt
from .media_collector import MediaCollector
from .robots_audit import RobotsAudit
from .security_headers import SecurityHeaders
from .seo_audit import SeoAudit
from .sitemap import Sitemap
from .structured_data import StructuredData
from .website_content_crawler import WebsiteContentCrawler

ACTORS = {a.name: a for a in (
    WebsiteContentCrawler(), Sitemap(), SeoAudit(), SecurityHeaders(), LlmsTxt(),
    LinkChecker(), MediaCollector(), RobotsAudit(), StructuredData(), ChangeMonitor(),
    FeedReader(),
)}


def get_actor(name):
    """Look up by name; an platform-style 'user~name' id resolves to 'name'."""
    if not isinstance(name, str):
        return None
    return ACTORS.get(name.split("~")[-1].split("/")[-1])
