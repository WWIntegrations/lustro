"""RFC 9309 robots.txt rules: groups, wildcards, longest match, linear matching."""
import time

from ww_scraper.robots import RobotsRules, pattern_matches, product_token

UA = "ww-scraper/0.3.0 (+https://example.org) python-requests"


def rules(text):
    return RobotsRules(text)


def test_product_token():
    assert product_token(UA) == "ww-scraper"
    assert product_token("") == ""


def test_no_rules_and_robots_txt_itself_are_allowed():
    r = rules("")
    assert r.can_fetch(UA, "https://a.test/anything")
    assert rules("User-agent: *\nDisallow: /").can_fetch(UA, "https://a.test/robots.txt")


def test_wildcard_and_end_anchor():
    r = rules("User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp*/x\nDisallow: /exact$")
    assert not r.can_fetch(UA, "https://a.test/files/a.pdf")
    assert r.can_fetch(UA, "https://a.test/files/a.pdf?download=1")   # $ anchors the end
    assert not r.can_fetch(UA, "https://a.test/tmp123/x/y")
    assert r.can_fetch(UA, "https://a.test/tmp123/y")
    assert not r.can_fetch(UA, "https://a.test/exact")
    assert r.can_fetch(UA, "https://a.test/exact/more")


def test_longest_match_wins_and_allow_wins_a_tie():
    r = rules("User-agent: *\nDisallow: /shop/\nAllow: /shop/public/\nDisallow: /a\nAllow: /a")
    assert not r.can_fetch(UA, "https://a.test/shop/cart")
    assert r.can_fetch(UA, "https://a.test/shop/public/item")
    assert r.can_fetch(UA, "https://a.test/a")
    d = r.decide(UA, "https://a.test/shop/public/item")
    assert d == {"allowed": True, "group": "*", "rule": "allow", "pattern": "/shop/public/"}


def test_specific_group_replaces_star_group():
    r = rules("User-agent: *\nDisallow: /\n\nUser-agent: WW-Scraper\nDisallow: /private/")
    assert r.can_fetch(UA, "https://a.test/page")
    assert not r.can_fetch(UA, "https://a.test/private/x")
    assert not r.can_fetch("otherbot/1.0", "https://a.test/page")


def test_groups_with_several_agents_and_merged_groups():
    r = rules("User-agent: a\nUser-agent: b\nDisallow: /x\n\nUser-agent: a\nDisallow: /y\nCrawl-delay: 7")
    assert not r.can_fetch("a/1", "https://a.test/x") and not r.can_fetch("a/1", "https://a.test/y")
    assert not r.can_fetch("b/1", "https://a.test/x") and r.can_fetch("b/1", "https://a.test/y")
    assert r.crawl_delay("a/1") == 7.0 and r.crawl_delay("b/1") is None


def test_empty_disallow_is_no_rule_and_query_is_matched():
    r = rules("User-agent: *\nDisallow:\nDisallow: /search?q=")
    assert r.can_fetch(UA, "https://a.test/")
    assert not r.can_fetch(UA, "https://a.test/search?q=x")
    assert r.can_fetch(UA, "https://a.test/search")


def test_percent_encoding_is_normalised():
    r = rules("User-agent: *\nDisallow: /a%3cd\nDisallow: /café")
    assert not r.can_fetch(UA, "https://a.test/a%3Cd")
    assert not r.can_fetch(UA, "https://a.test/caf%C3%A9/menu")


def test_sitemaps_comments_and_junk_lines():
    r = rules("# hi\nSitemap: https://a.test/s.xml\nDisallow: /early\nFoo: bar\n"
              "User-agent: * # all\nDisallow: /x # why\nSitemap: https://a.test/s.xml")
    assert r.site_maps() == ["https://a.test/s.xml"]
    assert r.ignored_lines == 2                       # the early rule and the unknown key
    assert not r.can_fetch(UA, "https://a.test/x") and r.can_fetch(UA, "https://a.test/early")
    assert rules("").site_maps() is None


def test_bad_crawl_delay_is_ignored():
    r = rules("User-agent: *\nCrawl-delay: soon\nCrawl-delay: -1\nCrawl-delay: nan\nCrawl-delay: inf")
    assert r.crawl_delay(UA) is None


def test_matching_is_linear_on_hostile_patterns():
    pattern = "/" + "*a" * 2000
    path = "/" + "a" * 20000 + "b"
    t0 = time.perf_counter()
    assert pattern_matches(pattern, path)
    assert not pattern_matches(pattern + "c$", path)
    assert time.perf_counter() - t0 < 1.0
