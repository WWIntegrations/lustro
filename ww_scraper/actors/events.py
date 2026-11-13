"""events: concerts, conferences, meetups and other events a site publishes as
schema.org Event data (the markup event listings and search use). One item per event with
dates, status, attendance mode, place and address, organizer, performers and the cheapest
offer; past events can be left out. Records missing what event rich results need are
flagged in `issues`.
"""
from __future__ import annotations

import datetime

from ..actor import CONCURRENCY, MIN_INTERVAL, REDACT, RENDER, START_URLS, Actor
from ..schema import STR_OR_NULL
from ._api import first, html_to_text
from .product_catalog import _enum, _name, _offer, _types, _url
from .structured_data import flatten_jsonld


def is_event(obj) -> bool:
    return any(t == "Event" or (t.endswith("Event") and t[0].isupper()) for t in _types(obj))


def _address(a):
    """(text, locality, country) from a PostalAddress or a plain string."""
    if isinstance(a, str):
        return a.strip() or None, None, None
    if not isinstance(a, dict):
        return None, None, None
    country = a.get("addressCountry")
    country = _name(country) if isinstance(country, dict) else country if isinstance(country, str) else None
    parts = [a.get(k) for k in ("streetAddress", "postalCode", "addressLocality", "addressRegion")]
    text = ", ".join(p.strip() for p in parts + [country] if isinstance(p, str) and p.strip())
    loc = a.get("addressLocality") if isinstance(a.get("addressLocality"), str) else None
    return text or None, loc, country


def _location(v) -> dict:
    places = v if isinstance(v, list) else [v] if v else []
    physical = next((p for p in places if isinstance(p, dict) and "VirtualLocation" not in _types(p)), None)
    virtual = next((p for p in places if isinstance(p, dict) and "VirtualLocation" in _types(p)), None)
    if physical is None and places and isinstance(places[0], str):
        physical = {"name": places[0]}
    out = {"name": None, "address": None, "locality": None, "country": None, "latitude": None,
           "longitude": None, "onlineUrl": _url(virtual.get("url")) if virtual else None}
    if physical:
        out["name"] = _name(physical.get("name"))
        out["address"], out["locality"], out["country"] = _address(physical.get("address"))
        geo = physical.get("geo") if isinstance(physical.get("geo"), dict) else {}
        for key in ("latitude", "longitude"):
            try:
                out[key] = float(geo[key]) if geo.get(key) not in (None, "") else None
            except (TypeError, ValueError):
                out[key] = None
    return out


def _names(v) -> list:
    items = v if isinstance(v, list) else [v] if v else []
    return [n for n in (_name(x) for x in items) if isinstance(n, str) and n.strip()]


def _date(v):
    return v if isinstance(v, str) and v.strip() else None


def event(obj: dict, page_url: str) -> dict:
    price, lo, hi, cur, avail, _cond, _valid, count = _offer(obj.get("offers"))
    offers = obj.get("offers")
    first_offer = offers[0] if isinstance(offers, list) and offers else offers if isinstance(offers, dict) else {}
    rec = {
        "name": _name(obj.get("name")),
        "type": next((t for t in _types(obj) if t.endswith("Event")), "Event"),
        "url": first(_url(obj.get("url")), page_url), "pageUrl": page_url,
        "startDate": _date(obj.get("startDate")), "endDate": _date(obj.get("endDate")),
        "status": _enum(obj.get("eventStatus")),
        "attendanceMode": _enum(obj.get("eventAttendanceMode")),
        "location": _location(obj.get("location")),
        "organizer": _name(obj.get("organizer")),
        "performers": _names(obj.get("performer")),
        "price": price, "lowPrice": lo, "highPrice": hi, "currency": cur, "availability": avail,
        "offerCount": count,
        "ticketUrl": _url(first_offer.get("url")) if isinstance(first_offer, dict) else None,
        "image": _url(obj.get("image")),
        "description": html_to_text(obj.get("description") or "", limit=1500) or None,
    }
    issues = []
    if not rec["name"]:
        issues.append("no name")
    if not rec["startDate"]:
        issues.append("no startDate")
    if not (rec["location"]["name"] or rec["location"]["address"] or rec["location"]["onlineUrl"]):
        issues.append("no location")
    if rec["startDate"] and rec["endDate"] and rec["endDate"][:10] < rec["startDate"][:10]:
        issues.append("endDate before startDate")
    if not rec["image"]:
        issues.append("no image")
    rec["issues"] = issues
    return rec


class Events(Actor):
    name = "events"
    version = "1.0.0"
    title = "Events"
    description = ("Concerts, conferences and meetups from the schema.org Event data a site publishes: "
                   "dates, place, organizer, tickets; past events can be skipped.")
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxPages": {"type": "integer", "default": 100, "minimum": 1, "maximum": 20000},
            "maxDepth": {"type": "integer", "default": 2, "minimum": 0, "maximum": 20},
            "upcomingOnly": {"type": "boolean", "default": True,
                             "description": "Leave out events that ended before today"},
            "includeGlobs": {"type": "array", "items": {"type": "string"}, "default": []},
            "excludeGlobs": {"type": "array", "items": {"type": "string"},
                             "default": ["*/login*", "*/account*", "*/cart*", "*/koszyk*"]},
            "minIntervalSecs": MIN_INTERVAL,
            "maxConcurrency": CONCURRENCY,
            "render": RENDER,
            "redactPii": REDACT,
        },
    }
    dataset_schema = {
        "fields": {"type": "object", "required": ["name", "startDate", "location", "issues"],
                   "properties": {"name": STR_OR_NULL, "startDate": STR_OR_NULL, "location": {"type": "object"},
                                  "issues": {"type": "array", "items": {"type": "string"}}}},
        "views": {"overview": {"title": "Overview", "fields": ["startDate", "name", "location.name",
                                                                "location.locality", "price", "currency", "url"]}},
    }

    def run(self, ctx):
        inp = ctx.input
        today = datetime.date.today().isoformat()
        seen = set()
        summary = {"pages": 0, "events": 0, "skippedPast": 0, "duplicates": 0, "withIssues": 0}

        def on_page(res, req, doc):
            if doc is None:
                if res.error and not res.duplicate:
                    ctx.record_failure(req["url"], res.error)
                return
            summary["pages"] += 1
            out = []
            for obj in flatten_jsonld(doc.jsonld):
                if not is_event(obj):
                    continue
                rec = event(obj, res.final_url or req["url"])
                key = (rec["name"], rec["startDate"], rec["location"]["name"] or rec["location"]["address"])
                if key in seen:
                    summary["duplicates"] += 1
                    continue
                seen.add(key)
                last = (rec["endDate"] or rec["startDate"] or "")[:10]
                if inp["upcomingOnly"] and last and last < today:
                    summary["skippedPast"] += 1
                    continue
                if rec["description"]:
                    rec["description"] = ctx.redact_text(rec["description"])
                summary["withIssues"] += bool(rec["issues"])
                out.append(rec)
            if out:
                summary["events"] += len(out)
                ctx.push_data(out)

        ctx.crawler().crawl(inp["startUrls"], on_page, max_pages=inp["maxPages"], max_depth=inp["maxDepth"],
                            include=inp["includeGlobs"] or None, exclude=inp["excludeGlobs"] or None)
        ctx.set_value("OUTPUT", summary)
