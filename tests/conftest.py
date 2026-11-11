"""Shared fixtures. Everything runs offline against the bundled 127.0.0.1 test site;
loopback access is enabled only through the explicit allow_loopback test switch."""
import pytest

from ww_scraper.storage import Storage
from ww_scraper.testsite import TestSite


@pytest.fixture(scope="session")
def _site():
    site = TestSite().start()
    yield site
    site.stop()


@pytest.fixture
def site(_site):
    _site.reset()
    return _site


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "storage")


@pytest.fixture
def crawler(storage):
    from ww_scraper.crawler import PoliteCrawler
    return PoliteCrawler(min_interval=0, allow_loopback=True, backoff_base=0.05, max_backoff=5)
