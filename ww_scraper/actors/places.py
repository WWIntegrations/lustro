"""places: businesses and points of interest from OpenStreetMap, through a public Overpass
API instance. Open data (ODbL): every record carries its source URL and the attribution the
licence requires, so the result can be reused, shared and published.

Pick a category (or raw OSM tags) and an area: a city name, a circle around a point, or a
bounding box. The default endpoint is a public instance whose robots.txt allows API use;
the main overpass-api.de instance disallows /api/ to crawlers, so ww-scraper never calls it.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..actor import MIN_INTERVAL, REDACT, Actor
from ._api import get_json

ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
# public instances whose robots.txt allows API use (checked 2026-11-12); the main instance
# disallows /api/. Tried in order when one does not answer.
ENDPOINTS = ("https://overpass.kumi.systems/api/interpreter",
             "https://overpass.private.coffee/api/interpreter")
SERVER_TIMEOUT = 60          # seconds the query lets the Overpass server work
CATEGORIES = {
    "restaurant": ["amenity=restaurant"], "cafe": ["amenity=cafe"], "bar": ["amenity=bar", "amenity=pub"],
    "fast_food": ["amenity=fast_food"], "bakery": ["shop=bakery"], "supermarket": ["shop=supermarket"],
    "pharmacy": ["amenity=pharmacy"], "dentist": ["amenity=dentist", "healthcare=dentist"],
    "doctor": ["amenity=doctors", "healthcare=doctor"], "clinic": ["amenity=clinic"],
    "veterinary": ["amenity=veterinary"], "hairdresser": ["shop=hairdresser"], "beauty": ["shop=beauty"],
    "gym": ["leisure=fitness_centre"], "hotel": ["tourism=hotel", "tourism=guest_house"],
    "car_repair": ["shop=car_repair"], "car_dealer": ["shop=car"], "fuel": ["amenity=fuel"],
    "lawyer": ["office=lawyer"], "accountant": ["office=accountant", "office=tax_advisor"],
    "estate_agent": ["office=estate_agent"], "it_company": ["office=it", "office=company"],
    "coworking": ["amenity=coworking_space", "office=coworking"], "school": ["amenity=school"],
    "kindergarten": ["amenity=kindergarten"], "florist": ["shop=florist"], "clothes": ["shop=clothes"],
    "electronics": ["shop=electronics"], "hardware": ["shop=hardware", "shop=doityourself"],
    "bicycle": ["shop=bicycle"], "optician": ["shop=optician"], "bank": ["amenity=bank"],
}
_TAG = re.compile(r"[A-Za-z0-9_:.-]{1,60}")
_VALUE = re.compile(r"[^\"\\\n\r]{1,100}")


def _q(value: str) -> str:
    """Overpass QL string literal (values are validated first; this only escapes)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(tags: list, inp: dict, limit: int) -> str:
    filters = []
    for spec in tags:
        k, _, v = spec.partition("=")
        if not _TAG.fullmatch(k) or (v and not _VALUE.fullmatch(v)):
            raise ValueError(f"tag {spec!r}: use key=value with plain characters")
        filters.append(f"[{_q(k)}={_q(v)}]" if v else f"[{_q(k)}]")
    if inp.get("bbox"):
        b = inp["bbox"]
        if len(b) != 4 or not (-90 <= b[0] < b[2] <= 90 and -180 <= b[1] < b[3] <= 180):
            raise ValueError("bbox: [south, west, north, east] in degrees")
        if (b[2] - b[0]) * (b[3] - b[1]) > 4:
            raise ValueError("bbox: at most 4 square degrees per run (be kind to a shared server)")
        scope, head = f"({b[0]},{b[1]},{b[2]},{b[3]})", ""
    elif inp.get("lat") is not None and inp.get("lon") is not None:
        lat, lon, r = inp["lat"], inp["lon"], inp["radiusMeters"]
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("lat/lon out of range")
        scope, head = f"(around:{int(r)},{lat},{lon})", ""
    elif inp.get("city"):
        if not _VALUE.fullmatch(inp["city"]):
            raise ValueError("city: plain name only")
        head = f'area[{_q("name")}={_q(inp["city"])}][{_q("boundary")}={_q("administrative")}]->.a;\n'
        scope = "(area.a)"
    else:
        raise ValueError("give an area: city, lat+lon (with radiusMeters) or bbox")
    body = "".join(f"  nwr{f}{scope};\n" for f in filters)
    return f"[out:json][timeout:{SERVER_TIMEOUT}];\n{head}(\n{body});\nout tags center qt {int(limit)};"


def normalise(el: dict) -> dict:
    t = el.get("tags") or {}
    lat = el.get("lat", (el.get("center") or {}).get("lat"))
    lon = el.get("lon", (el.get("center") or {}).get("lon"))
    kind = el.get("type")
    category = next((f"{k}={t[k]}" for k in ("amenity", "shop", "office", "tourism", "leisure", "healthcare",
                                             "craft") if k in t), None)
    return {
        "name": t.get("name"), "category": category, "brand": t.get("brand"),
        "street": t.get("addr:street") or t.get("addr:place"), "houseNumber": t.get("addr:housenumber"),
        "postcode": t.get("addr:postcode"), "city": t.get("addr:city"), "country": t.get("addr:country"),
        "lat": lat, "lon": lon,
        "phone": t.get("contact:phone") or t.get("phone"), "email": t.get("contact:email") or t.get("email"),
        "website": t.get("contact:website") or t.get("website"), "openingHours": t.get("opening_hours"),
        "cuisine": t.get("cuisine"), "wheelchair": t.get("wheelchair"),
        "osmUrl": f"https://www.openstreetmap.org/{kind}/{el.get('id')}", "osmId": f"{kind}/{el.get('id')}",
        "tags": t, "license": ATTRIBUTION,
    }


class Places(Actor):
    ENDPOINTS = ENDPOINTS
    name = "places"
    title = "Places (OpenStreetMap)"
    description = ("Businesses and points of interest by category and area from OpenStreetMap: name, "
                   "address, coordinates, website, opening hours. Open data you may reuse (ODbL).")
    input_schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": [""] + sorted(CATEGORIES), "default": "",
                         "description": "A ready category, or leave empty and give tags"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": [], "maxItems": 10,
                     "description": "Raw OSM tags such as shop=bicycle or cuisine=sushi (any of them)"},
            "city": {"type": "string", "default": "", "maxLength": 100,
                     "description": "Administrative area name as in OSM, e.g. Gdańsk"},
            "lat": {"type": "number", "minimum": -90, "maximum": 90},
            "lon": {"type": "number", "minimum": -180, "maximum": 180},
            "radiusMeters": {"type": "integer", "default": 2000, "minimum": 50, "maximum": 50000},
            "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                     "description": "[south, west, north, east]"},
            "onlyWithWebsite": {"type": "boolean", "default": False},
            "maxResults": {"type": "integer", "default": 1000, "minimum": 1, "maximum": 20000},
            "endpoint": {"type": "string", "default": "",
                         "description": "An Overpass API instance that allows automated use"},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        tags = list(CATEGORIES.get(inp["category"], [])) + list(inp["tags"])
        if not tags:
            raise ValueError("choose a category or give at least one tag")
        query = build_query(tags, inp, inp["maxResults"])
        own = inp["endpoint"].strip()
        if own and not own.startswith("https://") and not ctx.allow_loopback:
            raise ValueError("endpoint must be https")
        ctx.log.info(f"places: Overpass query\n{query}")
        # Overpass sends nothing until the result is ready: the read timeout must outlast the
        # time the query allows the server, or the client gives up on every busy minute.
        # One retry per instance, then the next instance: a stalled server costs minutes, not an hour.
        crawler = ctx.crawler(timeout=SERVER_TIMEOUT + 15, max_retries=1)
        data, errors = None, []
        for endpoint in ([own] if own else list(self.ENDPOINTS)):
            ctx.check_abort()
            data, err = get_json(crawler, f"{endpoint}?data={quote(query)}")
            if not err:
                break
            ctx.record_failure(endpoint, err)
            errors.append(f"{endpoint.split('/')[2]}: {err}")
            data = None
        if data is None:
            raise RuntimeError("Overpass API: " + "; ".join(errors))
        remark = (data or {}).get("remark")
        if remark and "error" in remark.lower():
            raise RuntimeError(f"Overpass API: {remark[:300]}")
        items, seen = [], set()
        for el in (data or {}).get("elements", []):
            if not isinstance(el, dict) or (el.get("type"), el.get("id")) in seen:
                continue
            seen.add((el.get("type"), el.get("id")))
            rec = normalise(el)
            if inp["onlyWithWebsite"] and not rec["website"]:
                continue
            items.append(rec)
        ctx.push_data(items)
        summary = {"results": len(items), "withWebsite": sum(1 for i in items if i["website"]),
                   "withPhone": sum(1 for i in items if i["phone"]),
                   "withOpeningHours": sum(1 for i in items if i["openingHours"]),
                   "endpoint": endpoint, "attribution": ATTRIBUTION, "query": query}
        if remark:
            summary["remark"] = remark[:300]
        ctx.set_value("OUTPUT", summary)
