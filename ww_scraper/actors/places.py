"""places: businesses and points of interest from OpenStreetMap, through a public Overpass
API instance. Open data (ODbL): every record carries its source URL and the attribution the
licence requires, so the result can be reused, shared and published.

Pick a category (or raw OSM tags) and an area: a city name, a circle around a point, or a
bounding box. The default endpoint is a public instance whose robots.txt allows API use;
the main overpass-api.de instance disallows /api/ to crawlers, so ww-scraper never calls it.

Without any server: download a regional extract (.osm.pbf, e.g. from download.geofabrik.de,
by hand or with your own script), put it in the directory named by WW_SCRAPER_OSM_DIR and
pass its file name as osmFile. The extract is read locally (optional `osmium` package); no
request leaves the machine, the result is the same shape, and the run is repeatable.
"""
from __future__ import annotations

import math
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from ..actor import MIN_INTERVAL, REDACT, Actor
from ..schema import STR_OR_NULL
from ._api import get_json

ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
# public instances whose robots.txt allows API use (checked 2026-11-12); the main instance
# disallows /api/. Tried in order when one does not answer.
# One public instance. overpass.private.coffee used to be listed as a fallback, but it is the
# same machine (overpass.kumi.systems is a CNAME to it), so it never helped when the first was
# down. overpass-api.de disallows the API in robots.txt. Another instance: the endpoint input.
ENDPOINTS = ("https://overpass.kumi.systems/api/interpreter",)
# Instances of your own (or another operator you trust), tried first: comma-separated https URLs
ENV_ENDPOINTS = "WW_SCRAPER_OVERPASS"
# Directory with OSM extracts the operator downloaded. osmFile is a file name inside it, never
# a path: a run started over the API or MCP must not be able to read other files.
ENV_OSM_DIR = "WW_SCRAPER_OSM_DIR"
PROBE_TIMEOUT = 10           # /api/status answers at once on a working server
BUSY_PAUSE = 30              # seconds to wait once when a server says it is busy (429/504)
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


_WEBSITE = '[~"^(website|contact:website)$"~"."]'


def status_url_of(endpoint: str) -> str:
    """The Overpass status page next to an interpreter URL, by URL parts, not string cuts:
    https://host/api/interpreter -> https://host/api/status; a bare host gets /api/status."""
    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/")
    base = path.rsplit("/", 1)[0] if path.endswith("interpreter") else "/api"
    return urlunsplit((parts.scheme, parts.netloc, (base or "/api") + "/status", "", ""))


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
    # the website filter runs on the server: filtering after "out ... qt N" kept only the
    # places with a website among the first N of one map tile, not N places with a website
    web = _WEBSITE if inp.get("onlyWithWebsite") else ""
    body = "".join(f"  nwr{f}{web}{scope};\n" for f in filters)
    return f"[out:json][timeout:{SERVER_TIMEOUT}];\n{head}(\n{body});\nout tags center qt {int(limit)};"


def osm_file_path(name: str) -> Path:
    """The extract named `name` inside $WW_SCRAPER_OSM_DIR, or ValueError. Only a plain file
    name ending in .osm.pbf; the resolved file must sit directly in that directory."""
    root = os.environ.get(ENV_OSM_DIR, "").strip()
    if not root:
        raise ValueError(f"osmFile needs {ENV_OSM_DIR}: the directory that holds the .osm.pbf extracts")
    if (not name.endswith(".osm.pbf") or name.startswith(".") or "/" in name or "\\" in name
            or "\x00" in name):
        raise ValueError(f"osmFile: a file name ending in .osm.pbf inside {ENV_OSM_DIR}, not a path")
    base = Path(root).resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise ValueError(f"osmFile: no extract named {name!r} in {ENV_OSM_DIR}")
    return path


def _distance_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def area_test(inp: dict):
    """A function (lat, lon) -> bool for the area of an osmFile run. A city name needs its
    administrative boundary, which an extract read point by point does not give: refused."""
    if inp.get("bbox"):
        s, w, n, e = inp["bbox"]
        if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
            raise ValueError("bbox: [south, west, north, east] in degrees")
        return lambda lat, lon: s <= lat <= n and w <= lon <= e
    if inp.get("lat") is not None and inp.get("lon") is not None:
        lat0, lon0, r = inp["lat"], inp["lon"], inp["radiusMeters"]
        if not (-90 <= lat0 <= 90 and -180 <= lon0 <= 180):
            raise ValueError("lat/lon out of range")
        return lambda lat, lon: _distance_m(lat0, lon0, lat, lon) <= r
    raise ValueError("with osmFile give lat+lon (with radiusMeters) or bbox; a city name needs a "
                     "boundary the extract is not searched for")


def read_osm_file(path: Path, tags: list, inside, only_with_website: bool, check_abort=None):
    """Elements of a local extract that carry any of `tags` (key=value or key) and lie in the
    area, as Overpass-shaped dicts (`center` for ways = centre of the bounding box, as
    Overpass computes it). Relations are not read. Returns (elements, data timestamp)."""
    try:
        import osmium
    except ImportError as e:
        raise RuntimeError("osmFile needs the osmium package: pip install osmium") from e
    pairs = []
    for spec in tags:
        k, _, v = spec.partition("=")
        if not _TAG.fullmatch(k) or (v and not _VALUE.fullmatch(v)):
            raise ValueError(f"tag {spec!r}: use key=value with plain characters")
        pairs.append((k, v))
    keys = sorted({k for k, _ in pairs})
    header = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING).header()
    stamp = header.get("osmosis_replication_timestamp") or header.get("timestamp") or None
    proc = (osmium.FileProcessor(str(path), osmium.osm.NODE | osmium.osm.WAY)
            .with_locations().with_filter(osmium.filter.KeyFilter(*keys)))
    out = []
    for n, obj in enumerate(proc):
        if check_abort and n % 1000 == 0:
            check_abort()
        t = {tag.k: tag.v for tag in obj.tags}
        if not any(k in t and (not v or t[k] == v) for k, v in pairs):
            continue
        if only_with_website and not (t.get("website") or t.get("contact:website")):
            continue
        if obj.is_node():
            if not obj.location.valid():
                continue
            lat, lon, el = obj.location.lat, obj.location.lon, {"type": "node"}
        else:
            locs = [nd.location for nd in obj.nodes if nd.location.valid()]
            if not locs:
                continue
            lats, lons = [p.lat for p in locs], [p.lon for p in locs]
            lat, lon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
            el = {"type": "way", "center": {"lat": round(lat, 7), "lon": round(lon, 7)}}
        if not inside(lat, lon):
            continue
        el.update({"id": obj.id, "tags": t})
        if el["type"] == "node":
            el.update({"lat": round(lat, 7), "lon": round(lon, 7)})
        out.append(el)
    out.sort(key=lambda e: (e["type"], e["id"]))
    return out, stamp


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
    version = "1.2.0"
    dataset_schema = {
        "fields": {"type": "object", "required": ["name", "category", "osmId", "osmUrl", "license", "lat", "lon"],
                   "properties": {
                       "name": STR_OR_NULL, "category": STR_OR_NULL, "street": STR_OR_NULL, "houseNumber": STR_OR_NULL,
                       "postcode": STR_OR_NULL, "city": STR_OR_NULL, "lat": {"type": ["number", "null"]},
                       "lon": {"type": ["number", "null"]}, "phone": STR_OR_NULL, "email": STR_OR_NULL,
                       "website": STR_OR_NULL, "openingHours": STR_OR_NULL, "osmUrl": {"type": "string"},
                       "osmId": {"type": "string"}, "tags": {"type": "object"}, "license": {"type": "string"}}},
        "views": {"overview": {"title": "Overview", "fields": ["name", "category", "street", "houseNumber", "postcode",
                                                                "city", "website", "phone", "openingHours", "osmUrl"]}},
    }
    kv_schema = {"collections": {"summary": {"key": "OUTPUT", "contentTypes": ["application/json"],
                                             "jsonSchema": {"type": "object", "required": ["results", "attribution"]}}}}
    output_schema = {
        "items": {"title": "Places", "template": "/v2/datasets/{defaultDatasetId}/items"},
        "overview": {"title": "Overview (CSV)", "template": "/v2/datasets/{defaultDatasetId}/items?view=overview&format=csv"},
        "summary": {"title": "Summary (OUTPUT)", "template": "/v2/key-value-stores/{defaultKeyValueStoreId}/records/OUTPUT"},
    }
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
            "osmFile": {"type": "string", "default": "", "maxLength": 200,
                        "description": "Read a local extract instead of Overpass: file name (*.osm.pbf) "
                                       "in the directory set by WW_SCRAPER_OSM_DIR"},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        tags = list(CATEGORIES.get(inp["category"], [])) + list(inp["tags"])
        if not tags:
            raise ValueError("choose a category or give at least one tag")
        if inp["osmFile"].strip():
            path = osm_file_path(inp["osmFile"].strip())
            inside = area_test(inp)
            ctx.set_status_message(f"reading {path.name}")
            elements, stamp = read_osm_file(path, tags, inside, inp["onlyWithWebsite"], ctx.check_abort)
            source = {"osmFile": path.name, "dataTimestamp": stamp, "matched": len(elements)}
            return self._finish(ctx, inp, {"elements": elements[:inp["maxResults"]]}, source)
        query = build_query(tags, inp, inp["maxResults"])
        own = inp["endpoint"].strip()
        env = [e.strip() for e in os.environ.get(ENV_ENDPOINTS, "").split(",") if e.strip()]
        for e in ([own] if own else []) + env:
            if not e.lower().startswith("https://") and not ctx.allow_loopback:
                raise ValueError(f"endpoint must be https: {e}")
        endpoints = [own] if own else (env or list(self.ENDPOINTS))
        ctx.log.info(f"places: Overpass query\n{query}")
        # Overpass sends nothing until the result is ready: the read timeout must outlast the
        # time the query allows the server, or the client gives up on every busy minute.
        # One retry per instance, then the next instance: a stalled server costs minutes, not an hour.
        crawler = ctx.crawler(timeout=SERVER_TIMEOUT + 15, max_retries=1)
        data, errors = None, []
        probe = ctx.crawler(timeout=PROBE_TIMEOUT, max_retries=0)
        for endpoint in endpoints:
            ctx.check_abort()
            # a server whose web front answers while the Overpass backend hangs used to cost
            # two 75 s timeouts; its status page tells in seconds
            status_url = status_url_of(endpoint)
            st = probe.fetch(status_url, accept_types=None, read_body=False)
            if not st.ok:
                err = f"not answering ({status_url}: {st.error or 'HTTP ' + str(st.status)})"
                ctx.record_failure(endpoint, err)
                errors.append(f"{endpoint.split('/')[2]}: {err}")
                continue
            data, err = get_json(crawler, f"{endpoint}?data={quote(query)}")
            if err and ("HTTP 429" in err or "HTTP 504" in err):
                ctx.log.info(f"{endpoint.split('/')[2]} is busy ({err}); trying again in {BUSY_PAUSE} s")
                for _ in range(BUSY_PAUSE):           # an abort must not wait out the pause
                    ctx.check_abort()
                    time.sleep(1)
                data, err = get_json(crawler, f"{endpoint}?data={quote(query)}")
            if not err:
                break
            ctx.record_failure(endpoint, err)
            errors.append(f"{endpoint.split('/')[2]}: {err}")
            data = None
        if data is None:
            hint = ("" if own else f"; the public instance may be down: set the endpoint input or "
                    f"{ENV_ENDPOINTS} to another Overpass server you are allowed to use")
            raise RuntimeError("Overpass API: " + "; ".join(errors) + hint)
        remark = (data or {}).get("remark")
        if remark and "error" in remark.lower():
            raise RuntimeError(f"Overpass API: {remark[:300]}")
        source = {"endpoint": endpoint, "query": query}
        if remark:
            source["remark"] = remark[:300]
        return self._finish(ctx, inp, data, source)

    def _finish(self, ctx, inp, data, source):
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
                   # phones are published by businesses, but a sole trader's number is
                   # personal data: masked unless redactPii is off (needs a legal basis)
                   "phonesMasked": bool(inp["redactPii"]),
                   "withOpeningHours": sum(1 for i in items if i["openingHours"]),
                   "attribution": ATTRIBUTION, **source}
        ctx.set_value("OUTPUT", summary)
