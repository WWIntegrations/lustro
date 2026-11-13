"""Ready-made actors: job-postings, places, product-catalog, contact-details - offline,
against one local server that imitates the public APIs and three kinds of websites."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from ww_scraper.actor import InputError, run_actor
from ww_scraper.actors.contact_details import is_role_address, social_profile
from ww_scraper.actors.job_postings import JobPostings
from ww_scraper.actors.places import Places, build_query

GH = {"jobs": [
    {"id": 101, "title": "Senior Python Engineer", "absolute_url": "https://boards.example/acme/101",
     "location": {"name": "Remote, Poland"}, "updated_at": "2026-11-10T09:00:00Z",
     "departments": [{"name": "Platform"}],
     "content": "&lt;p&gt;Build integrations. Questions: jan.nowak@acme.example&lt;/p&gt;"},
    {"id": 102, "title": "Office Manager", "absolute_url": "https://boards.example/acme/102",
     "location": {"name": "Warsaw"}, "updated_at": "2026-11-01T09:00:00Z", "content": "&lt;p&gt;Office&lt;/p&gt;"},
]}
LEVER = [{"id": "lv-1", "text": "AI Integration Engineer", "hostedUrl": "https://jobs.example/lv-1",
          "applyUrl": "https://jobs.example/lv-1/apply", "createdAt": 1762500000000,
          "categories": {"location": "Gdańsk", "team": "Agents", "commitment": "Full-time"},
          "workplaceType": "hybrid", "descriptionPlain": "Agents and APIs.",
          "salaryRange": {"min": 15000, "max": 22000, "currency": "PLN", "interval": "per-month"}}]
ASHBY_DATA = {"organization": {"name": "Acme"}, "jobBoard": {
    "teams": [{"id": "t-1", "name": "Engineering", "externalName": None}],
    "jobPostings": [
        {"id": "as-1", "title": "Python Developer", "teamId": "t-1", "locationName": "Berlin",
         "workplaceType": "Remote", "employmentType": "FullTime", "secondaryLocations": [{"locationName": "Lisbon"}],
         "compensationTierSummary": "€70K - €90K"},
        {"id": "as-2", "title": "Hidden Role", "isListed": False, "locationName": "Berlin"}]}}
ASHBY_PAGE = ("<html><head><title>Jobs</title></head><body><div id=root></div><script>"
              "window.__appData = " + json.dumps(ASHBY_DATA) + ";</script></body></html>")
CAREERS = """<html><head><title>Careers</title><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Data Engineer","datePosted":"2026-11-09",
 "hiringOrganization":{"@type":"Organization","name":"Firm SA"},"jobLocationType":"TELECOMMUTE",
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":"Kraków","addressCountry":"PL"}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"PLN","value":{"@type":"QuantitativeValue","minValue":12000,"maxValue":18000,"unitText":"MONTH"}},
 "employmentType":["FULL_TIME"],"identifier":{"@type":"PropertyValue","value":"DE-7"}}
</script></head><body><main><h1>Careers</h1></main></body></html>"""
OVERPASS = {"elements": [
    {"type": "node", "id": 1, "lat": 54.35, "lon": 18.65,
     "tags": {"amenity": "cafe", "name": "Kawiarnia A", "addr:street": "Długa", "addr:housenumber": "5",
              "addr:city": "Gdańsk", "website": "https://a.example", "opening_hours": "Mo-Fr 08:00-18:00",
              "phone": "+48 58 123 45 67"}},
    {"type": "way", "id": 2, "center": {"lat": 54.36, "lon": 18.64}, "tags": {"amenity": "cafe", "name": "B"}},
    {"type": "node", "id": 1, "lat": 54.35, "lon": 18.65, "tags": {"amenity": "cafe", "name": "Kawiarnia A"}},
]}


def product_page(jsonld: str, extra_head: str = "") -> str:
    return (f"<html><head><title>P</title>{extra_head}<script type=\"application/ld+json\">{jsonld}</script>"
            "</head><body><main>product</main></body></html>")


SHOP = {
    "/shop/": "<html><body><a href='/shop/p/1'>1</a><a href='/shop/p/1?utm_source=x'>1</a>"
              "<a href='/shop/p/2'>2</a><a href='/shop/group'>g</a><a href='/shop/og'>og</a>"
              "<a href='/shop/cart'>cart</a></body></html>",
    "/shop/p/1": product_page(json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "Kurtka",
                                          "sku": "K-1", "gtin13": "5901234123457", "brand": {"@type": "Brand", "name": "Ww"},
                                          "image": ["https://img.example/k1.jpg"], "aggregateRating": {"ratingValue": "4.6", "reviewCount": "31"},
                                          "offers": {"@type": "Offer", "price": "49,99", "priceCurrency": "PLN",
                                                     "availability": "https://schema.org/InStock"}})),
    "/shop/p/2": product_page(json.dumps({"@graph": [{"@type": "WebPage"}, {"@type": "Product", "name": "Czapka",
                                                     "sku": "C-2", "offers": [{"@type": "Offer", "price": 30},
                                                                               {"@type": "Offer", "price": 25}]}]})),
    "/shop/group": product_page(json.dumps({"@type": "ProductGroup", "name": "Buty", "brand": "Ww",
                                            "image": "https://img.example/b.jpg", "hasVariant": [
        {"@type": "Product", "sku": "B-40", "size": "40", "gtin13": "5901234123464",
         "offers": {"price": "199.00", "priceCurrency": "PLN", "availability": "https://schema.org/OutOfStock"}},
        {"@type": "Product", "sku": "B-41", "size": "41", "gtin13": "5901234123471",
         "offers": {"price": "199.00", "priceCurrency": "PLN", "availability": "https://schema.org/InStock"}}]})),
    "/shop/og": "<html><head><meta property='og:type' content='product'><meta property='og:title' content='Szal'>"
                "<meta property='product:price:amount' content='59'><meta property='product:price:currency' content='PLN'>"
                "</head><body>x</body></html>",
    "/shop/cart": product_page(json.dumps({"@type": "Product", "name": "SHOULD NOT BE READ"})),
}
FIRM = {
    "/firm/": """<html><head><script type="application/ld+json">{"@type":"LocalBusiness","name":"Firm SA",
      "telephone":"+48 58 000 11 22","vatID":"PL5840000000","address":{"streetAddress":"Długa 1","postalCode":"80-001",
      "addressLocality":"Gdańsk","addressCountry":"PL"},"sameAs":["https://www.linkedin.com/company/firm-sa"]}</script>
      </head><body><main><a href="/firm/kontakt">Kontakt</a><a href="/firm/zespol">Zespół</a>
      <a href="/firm/blog">Blog</a></main>
      <footer>Napisz: biuro@firm.example <a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
      <a href="https://www.instagram.com/firm.sa/">ig</a></footer></body></html>""",
    "/firm/kontakt": """<html><body><main><a href="mailto:sprzedaz@firm.example?subject=Hi">sprzedaz</a>
      <a href="tel:+48%2058%20000%2033%2044">zadzwoń</a></main></body></html>""",
    "/firm/zespol": """<html><body><main>Jan Kowalski, jan.kowalski@firm.example, tel. 601 234 567</main></body></html>""",
    "/firm/blog": "<html><body>NOT READ: blog@firm.example</body></html>",
}


class _Server:
    def __init__(self):
        self.queries = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                body, ctype, code = None, "text/html", 200
                if u.path == "/robots.txt":
                    body, ctype = "User-agent: *\nAllow: /\n", "text/plain"
                elif u.path == "/gh/v1/boards/acme/jobs":
                    body, ctype = json.dumps(GH), "application/json"
                elif u.path == "/lever/v0/postings/acme":
                    body, ctype = json.dumps(LEVER), "application/json"
                elif u.path == "/ashby/acme":
                    body = ASHBY_PAGE
                elif u.path == "/ashby/empty":
                    body = "<html><body>No board here</body></html>"
                elif u.path == "/careers":
                    body = CAREERS
                elif u.path == "/api/interpreter":
                    srv.queries.append(parse_qs(u.query)["data"][0])
                    body, ctype = json.dumps(OVERPASS), "application/json"
                elif u.path in SHOP or u.path in FIRM:
                    body = SHOP.get(u.path) or FIRM.get(u.path)
                else:
                    body, ctype, code = "nf", "text/plain", 404
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def srv():
    s = _Server()
    yield s
    s.httpd.shutdown()


@pytest.fixture
def apis(srv, monkeypatch):
    monkeypatch.setattr(JobPostings, "GREENHOUSE", srv.base + "/gh/v1/boards/{}/jobs?content={}")
    monkeypatch.setattr(JobPostings, "LEVER", srv.base + "/lever/v0/postings/{}?mode=json")
    monkeypatch.setattr(JobPostings, "ASHBY", srv.base + "/ashby/{}")
    return srv


def _run(storage, actor, inp):
    rec = run_actor(actor, dict(inp, minIntervalSecs=0), storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED", rec.get("statusMessage")
    return rec, storage.dataset(rec["defaultDatasetId"]).items(), storage.kv(rec["id"]).get("OUTPUT")


# --- job-postings ----------------------------------------------------------------------
def test_jobs_from_three_ats_apis_and_a_careers_page(storage, apis):
    _, items, out = _run(storage, "job-postings", {"boards": [
        "greenhouse:acme", "lever:acme", "ashby:acme", apis.base + "/careers"], "includeDescription": True})
    by = {i["title"]: i for i in items}
    assert set(by) == {"Senior Python Engineer", "Office Manager", "AI Integration Engineer",
                       "Python Developer", "Data Engineer"}
    gh = by["Senior Python Engineer"]
    assert gh["remote"] is True and gh["department"] == "Platform" and gh["location"] == "Remote, Poland"
    assert "Build integrations" in gh["description"] and "jan.nowak" not in gh["description"]   # PII masked
    lv = by["AI Integration Engineer"]
    assert lv["salary"] == "15000-22000 PLN per-month" and lv["postedAt"].startswith("2025-11-07")
    assert lv["employmentType"] == "Full-time" and lv["applyUrl"].endswith("/apply")
    asb = by["Python Developer"]
    assert asb["salary"] == "€70K - €90K" and asb["remote"] is True and asb["department"] == "Engineering"
    assert asb["location"] == "Berlin; Lisbon" and asb["url"] == apis.base + "/ashby/acme/as-1"
    assert asb["applyUrl"].endswith("/as-1/application") and "Hidden Role" not in by
    de = by["Data Engineer"]
    assert (de["company"], de["remote"], de["salary"], de["id"]) == ("Firm SA", True, "12000-18000 PLN MONTH", "DE-7")
    assert de["location"] == "Kraków, PL"
    assert out["fetched"] == 5 and out["failedBoards"] == 0
    dated = [i["postedAt"] or "" for i in items]
    assert dated == sorted(dated, reverse=True)


def test_job_filters(storage, apis):
    _, items, _ = _run(storage, "job-postings", {"boards": ["greenhouse:acme", "lever:acme", "ashby:acme"],
                                                 "keywords": ["python"], "remoteOnly": True,
                                                 "includeDescription": False})
    # newest first; the Ashby list carries no dates, so its posting comes last
    assert [i["title"] for i in items] == ["Senior Python Engineer", "Python Developer"]
    assert all("description" not in i for i in items)
    _, items, _ = _run(storage, "job-postings", {"boards": ["lever:acme", "greenhouse:acme"], "locations": ["gdańsk"]})
    assert [i["title"] for i in items] == ["AI Integration Engineer"]


def test_job_watch_reports_only_new_postings(storage, apis):
    _, first_run, out1 = _run(storage, "job-postings", {"boards": ["greenhouse:acme"], "watchName": "me"})
    _, second, out2 = _run(storage, "job-postings", {"boards": ["greenhouse:acme", "lever:acme"], "watchName": "me"})
    assert len(first_run) == 2 and out1["new"] == 2
    assert [i["title"] for i in second] == ["AI Integration Engineer"] and out2["new"] == 1


def test_job_board_specs_are_validated(storage, apis):
    for bad in ("workday:acme", "greenhouse:../../etc", "lever:a/b", "greenhouse:"):
        rec = run_actor("job-postings", {"boards": [bad], "minIntervalSecs": 0}, storage, allow_loopback=True)
        assert rec["status"] == "FAILED" and "use greenhouse" in rec["statusMessage"], bad


def test_ashby_page_without_board_data_is_a_recorded_failure(storage, apis):
    _, items, out = _run(storage, "job-postings", {"boards": ["ashby:empty", "ashby:acme"]})
    assert [i["title"] for i in items] == ["Python Developer"] and out["failedBoards"] == 1


def test_failed_board_is_recorded_not_fatal(storage, apis):
    _, items, out = _run(storage, "job-postings", {"boards": ["greenhouse:missing", "lever:acme"]})
    assert out["failedBoards"] == 1 and len(items) == 1


# --- places ----------------------------------------------------------------------------
def test_places_normalises_dedupes_and_attributes(storage, apis):
    _, items, out = _run(storage, "places", {"category": "cafe", "city": "Gdańsk",
                                             "endpoint": apis.base + "/api/interpreter", "redactPii": False})
    assert [i["name"] for i in items] == ["Kawiarnia A", "B"]
    a = items[0]
    assert (a["street"], a["houseNumber"], a["city"], a["website"]) == ("Długa", "5", "Gdańsk", "https://a.example")
    assert a["osmUrl"] == "https://www.openstreetmap.org/node/1" and "ODbL" in a["license"]
    assert items[1]["lat"] == 54.36                       # way centre
    assert out["withWebsite"] == 1 and "OpenStreetMap" in out["attribution"]
    q = apis.queries[-1]
    assert '["amenity"="cafe"](area.a)' in q and '["name"="Gdańsk"]' in q


def test_places_masks_phone_by_default_and_filters_website(storage, apis):
    _, items, _ = _run(storage, "places", {"category": "cafe", "lat": 54.35, "lon": 18.65, "radiusMeters": 500,
                                           "onlyWithWebsite": True, "endpoint": apis.base + "/api/interpreter"})
    assert [i["name"] for i in items] == ["Kawiarnia A"] and items[0]["phone"] == "[phone]"
    assert "(around:500,54.35,18.65)" in apis.queries[-1]


def test_places_query_is_injection_safe_and_bounded():
    q = build_query(["name=a];out;"], {"city": "X"}, 10)
    assert '["name"="a];out;"]' in q                       # stays inside the string literal
    for bad_tags, inp in ((["name=a\"];out;"], {"city": "X"}), (["name=a\\"], {"city": "X"}),
                          (["na me=x"], {"city": "X"}), (["amenity=cafe"], {}),
                          (["amenity=cafe"], {"bbox": [50, 14, 55, 24]}), (["amenity=cafe"], {"city": "a\nb"})):
        with pytest.raises(ValueError):
            build_query(bad_tags, inp, 10)


def test_places_endpoint_must_be_https(storage):
    rec = run_actor("places", {"category": "cafe", "city": "X", "endpoint": "http://example.org/api"}, storage)
    assert rec["status"] == "FAILED" and "https" in rec["statusMessage"]


# --- product-catalog -------------------------------------------------------------------
def test_products_from_jsonld_variants_and_og(storage, apis):
    _, items, out = _run(storage, "product-catalog", {"startUrls": [apis.base + "/shop/"]})
    by = {(i["name"], i["sku"]): i for i in items}
    k = by[("Kurtka", "K-1")]
    assert (k["price"], k["currency"], k["availability"], k["gtin"], k["brand"]) == (49.99, "PLN", "InStock",
                                                                                    "5901234123457", "Ww")
    assert (k["rating"], k["reviewCount"], k["issues"]) == (4.6, 31, [])
    c = by[("Czapka", "C-2")]
    assert (c["price"], c["lowPrice"], c["highPrice"]) == (25, 25, 30)
    assert "price without currency" in c["issues"] and "no availability" in c["issues"]
    sizes = {i["size"]: i for i in items if i["variantOf"] == "Buty"}
    assert set(sizes) == {"40", "41"} and sizes["40"]["availability"] == "OutOfStock"
    assert sizes["41"]["image"] == "https://img.example/b.jpg"       # inherited from the group
    assert by[("Szal", None)]["price"] == 59
    assert "SHOULD NOT BE READ" not in {i["name"] for i in items}    # cart excluded by default
    assert out["products"] == len(items) == 5 and out["currencies"]["PLN"] == 4


# --- contact-details -------------------------------------------------------------------
def test_contacts_keep_role_addresses_and_mask_personal_ones(storage, apis):
    _, items, out = _run(storage, "contact-details", {"startUrls": [apis.base + "/firm/"]})
    r = items[0]
    assert {e["address"] for e in r["emails"]} == {"biuro@firm.example", "sprzedaz@firm.example"}
    assert r["maskedPersonalEmails"] == 1 and r["maskedTextPhones"] == 1
    assert {p["number"] for p in r["phones"]} == {"+48580001122", "+48580003344"}
    assert r["socialProfiles"] == {"linkedin": "https://linkedin.com/company/firm-sa",
                                   "instagram": "https://instagram.com/firm.sa"}
    assert (r["organization"], r["vatId"]) == ("Firm SA", "PL5840000000")
    assert r["address"] == "Długa 1, 80-001, Gdańsk, PL"
    assert not any("/blog" in u for u in r["pagesRead"])
    assert out == {"sites": 1, "withEmail": 1, "withPhone": 1, "withSocial": 1}


def test_contacts_without_redaction_return_everything(storage, apis):
    _, items, _ = _run(storage, "contact-details", {"startUrls": [apis.base + "/firm/"], "redactPii": False})
    r = items[0]
    assert "jan.kowalski@firm.example" in {e["address"] for e in r["emails"]}
    assert "601234567" in {p["number"] for p in r["phones"]}
    assert "maskedPersonalEmails" not in r


def test_role_address_and_social_rules():
    for a in ("info@x.pl", "kontakt@x.pl", "biuro+web@x.pl", "sales.eu@x.com", "hr-team@x.com"):
        assert is_role_address(a), a
    for a in ("jan.kowalski@x.pl", "j.nowak@x.pl", "anna@x.pl"):
        assert not is_role_address(a), a
    assert social_profile("https://www.facebook.com/sharer/sharer.php?u=1") is None
    assert social_profile("https://x.com/intent/tweet?x") is None
    assert social_profile("https://twitter.com/firm") == ("x", "https://twitter.com/firm")
    assert social_profile("https://evil.example/linkedin.com/x") is None


def test_ready_actors_validate_input(storage):
    for actor, inp in (("job-postings", {}), ("product-catalog", {}), ("contact-details", {})):
        with pytest.raises(InputError):
            from ww_scraper.actor import prepare_run
            prepare_run(actor, inp, storage)
    assert Places.name == "places"


def test_places_falls_back_to_the_next_instance(storage, apis, monkeypatch):
    from ww_scraper.actors.places import Places
    monkeypatch.setattr(Places, "ENDPOINTS", (apis.base + "/down/api/interpreter", apis.base + "/api/interpreter"))
    rec, items, out = _run(storage, "places", {"category": "cafe", "city": "Gdańsk"})
    assert items and out["endpoint"] == apis.base + "/api/interpreter"


def test_network_errors_are_not_retried_twice_over():
    from types import SimpleNamespace
    from ww_scraper.actors._api import fetch_body
    calls = []

    class Stalled:
        def fetch(self, url, **kw):
            calls.append(url)
            return SimpleNamespace(ok=False, blocked=False, status=0, error="network: ReadTimeout", body=b"")

    body, err = fetch_body(Stalled(), "https://x.example/api", pause=0)
    assert body is None and err == "network: ReadTimeout" and len(calls) == 1

    class NoDns(Stalled):
        def fetch(self, url, **kw):
            calls.append(url)
            return SimpleNamespace(ok=False, blocked=False, status=0, error="dns: unresolvable host", body=b"")

    calls.clear()
    fetch_body(NoDns(), "https://x.example/api", retries=2, pause=0)
    assert len(calls) == 3


def test_only_with_website_filters_on_the_server():
    q = build_query(["amenity=dentist"], {"city": "X", "onlyWithWebsite": True}, 25)
    assert '[~"^(website|contact:website)$"~"."]' in q
    assert "website" not in build_query(["amenity=dentist"], {"city": "X"}, 25)


def test_organization_subtypes_are_recognised():
    from ww_scraper.schema_types import is_organization_type
    for t in ("Dentist", "BeautySalon", "HairSalon", "CafeOrCoffeeShop", "FoodEstablishment",
              "https://schema.org/MedicalClinic", "BikeStore", "Organization"):
        assert is_organization_type(t), t
    for t in ("WebSite", "WebPage", "Person", "BreadcrumbList", None):
        assert not is_organization_type(t), t


def test_llms_txt_with_bom_is_valid_and_empty_file_is_not_present():
    from ww_scraper.actors.llms_txt import validate_llms_txt
    r = validate_llms_txt("﻿# Clinic\n\n> Dental care in the city.\n\n## Pages\n\n- [Home](https://e.com/)\n")
    assert r["valid"], r["issues"]
