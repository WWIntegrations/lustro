"""Registered actors. Each one runs only through RunContext + PoliteCrawler, so every
fetch is robots.txt-checked, rate-limited per host and SSRF-guarded."""
from __future__ import annotations

from .articles import Articles
from .change_monitor import ChangeMonitor
from .contact_details import ContactDetails
from .events import Events
from .feed_reader import FeedReader
from .job_postings import JobPostings
from .lead_list import LeadList
from .link_checker import LinkChecker
from .llms_txt import LlmsTxt
from .media_collector import MediaCollector
from .package_info import PackageInfo
from .pipeline import Pipeline
from .places import Places
from .product_catalog import ProductCatalog
from .robots_audit import RobotsAudit
from .security_headers import SecurityHeaders
from .seo_audit import SeoAudit
from .sitemap import Sitemap
from .structured_data import StructuredData
from .website_content_crawler import WebsiteContentCrawler

ACTORS = {a.name: a for a in (
    WebsiteContentCrawler(), Sitemap(), SeoAudit(), SecurityHeaders(), LlmsTxt(),
    LinkChecker(), MediaCollector(), RobotsAudit(), StructuredData(), ChangeMonitor(),
    FeedReader(), JobPostings(), Places(), ProductCatalog(), ContactDetails(), LeadList(),
    Pipeline(), Articles(), Events(), PackageInfo(),
)}


BUILTIN = frozenset(ACTORS)


def get_actor(name):
    """Look up by name; a 'user~name' id resolves to 'name'."""
    if not isinstance(name, str):
        return None
    return ACTORS.get(name.split("~")[-1].split("/")[-1])
