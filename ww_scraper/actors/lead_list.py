"""lead-list: businesses from OpenStreetMap (`places`) joined with the contact channels each
one publishes on its own website (`contact-details`), as one record per business.

It runs the two actors through ctx.call_actor(), so every rule of both applies unchanged:
Overpass instances that allow API use, robots.txt and rate limits on every website, and
the field-aware GDPR policy of contact-details (role addresses kept, personal addresses
and phones from running text masked unless redactPii is off).
"""
from __future__ import annotations

from ..actor import MIN_INTERVAL, REDACT, Actor
from ..schema import STR_OR_NULL
from .places import ATTRIBUTION, CATEGORIES

CHUNK = 100                      # contact-details takes up to 100 start URLs per run


def _address(p: dict):
    street = " ".join(x for x in (p.get("street"), p.get("houseNumber")) if x)
    town = " ".join(x for x in (p.get("postcode"), p.get("city")) if x)
    return ", ".join(x for x in (street, town) if x) or None


def _site_key(url: str) -> str:
    return url.strip().rstrip("/").lower()


class LeadList(Actor):
    name = "lead-list"
    version = "1.0.0"
    dataset_schema = {
        "fields": {"type": "object", "required": ["name", "category", "address", "website", "emails", "phones",
                                                  "socialProfiles", "source"],
                   "properties": {
                       "name": STR_OR_NULL, "category": STR_OR_NULL, "address": STR_OR_NULL, "website": STR_OR_NULL,
                       "emails": {"type": "array", "items": {"type": "string"}},
                       "phones": {"type": "array", "items": {"type": "string"}},
                       "socialProfiles": {"type": "object"},
                       "source": {"type": "object", "required": ["osmUrl", "license"]}}},
        "views": {"spreadsheet": {"title": "Spreadsheet", "flatten": True,
                                  "fields": ["name", "category", "address", "website", "emails", "phones",
                                             "socialProfiles.linkedin", "socialProfiles.facebook",
                                             "socialProfiles.instagram", "source.osmUrl"]}},
    }
    kv_schema = {"collections": {"summary": {"key": "OUTPUT", "contentTypes": ["application/json"],
                                             "jsonSchema": {"type": "object", "required": ["leads", "attribution"]}}}}
    output_schema = {
        "items": {"title": "Leads", "template": "/v2/datasets/{defaultDatasetId}/items"},
        "spreadsheet": {"title": "Leads (CSV)", "template": "/v2/datasets/{defaultDatasetId}/items?view=spreadsheet&format=csv"},
        "summary": {"title": "Summary (OUTPUT)", "template": "/v2/key-value-stores/{defaultKeyValueStoreId}/records/OUTPUT"},
    }
    title = "Lead List (places + contact details)"
    description = ("Businesses by category and area from OpenStreetMap, each joined with the e-mails, "
                   "phones and social profiles it publishes on its own website. Role contacts kept, "
                   "personal ones masked by default (GDPR).")
    input_schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": [""] + sorted(CATEGORIES), "default": "",
                         "description": "A ready category, or leave empty and give tags"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": [], "maxItems": 10},
            "city": {"type": "string", "default": "", "maxLength": 100},
            "lat": {"type": "number", "minimum": -90, "maximum": 90},
            "lon": {"type": "number", "minimum": -180, "maximum": 180},
            "radiusMeters": {"type": "integer", "default": 2000, "minimum": 50, "maximum": 50000},
            "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            "maxResults": {"type": "integer", "default": 200, "minimum": 1, "maximum": 2000,
                           "description": "Places to read from OpenStreetMap"},
            "onlyWithWebsite": {"type": "boolean", "default": True,
                                "description": "Leave out places that list no website"},
            "maxPagesPerSite": {"type": "integer", "default": 4, "minimum": 1, "maximum": 20},
            "endpoint": {"type": "string", "default": "",
                         "description": "An Overpass API instance that allows automated use"},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        inp = ctx.input
        area = {k: inp[k] for k in ("category", "tags", "city", "radiusMeters", "endpoint") if k in inp}
        area.update({k: inp[k] for k in ("lat", "lon", "bbox") if inp.get(k) is not None})
        common = {"minIntervalSecs": inp["minIntervalSecs"], "redactPii": inp["redactPii"]}
        ctx.set_status_message("reading places from OpenStreetMap")
        places_run = ctx.call_actor("places", dict(area, **common, maxResults=inp["maxResults"],
                                                   onlyWithWebsite=inp["onlyWithWebsite"]))
        if places_run["status"] != "SUCCEEDED":
            raise RuntimeError(f"places run {places_run['id']} {places_run['status']}: "
                               f"{places_run.get('statusMessage')}")
        places = ctx.storage.dataset(places_run["defaultDatasetId"]).items()
        sites = []
        for p in places:
            w = p.get("website") or ""
            if w.lower().startswith(("http://", "https://")) and _site_key(w) not in {_site_key(s) for s in sites}:
                sites.append(w.strip())
        contacts, contact_runs = {}, []
        for i in range(0, len(sites), CHUNK):
            batch = sites[i:i + CHUNK]
            ctx.set_status_message(f"{len(places)} places, reading contacts of sites "
                                   f"{i + 1}-{i + len(batch)} of {len(sites)}")
            run = ctx.call_actor("contact-details", dict(common, startUrls=batch,
                                                         maxPagesPerSite=inp["maxPagesPerSite"]))
            contact_runs.append(run["id"])
            if run["status"] not in ("SUCCEEDED",):
                ctx.log.warning(f"contact-details run {run['id']} {run['status']}: {run.get('statusMessage')}")
            for c in ctx.storage.dataset(run["defaultDatasetId"]).iter_items():
                contacts[_site_key(c.get("startUrl") or "")] = c
        leads = []
        for p in places:
            c = contacts.get(_site_key(p.get("website") or ""), {})
            phones = [x["number"] for x in c.get("phones", []) if x.get("number")]
            if p.get("phone") and p["phone"] != "[phone]" and p["phone"] not in phones:
                phones.insert(0, p["phone"])
            emails = [x["address"] for x in c.get("emails", []) if x.get("address")]
            if p.get("email") and "[email]" not in p["email"] and p["email"] not in emails:
                emails.insert(0, p["email"])
            lead = {
                "name": p.get("name") or c.get("organization"), "category": p.get("category"),
                "address": _address(p) or c.get("address"), "website": p.get("website"),
                "emails": emails, "phones": phones, "socialProfiles": c.get("socialProfiles") or {},
                "vatId": c.get("vatId"), "openingHours": p.get("openingHours"),
                "lat": p.get("lat"), "lon": p.get("lon"),
                "source": {"osmUrl": p.get("osmUrl"), "license": p.get("license") or ATTRIBUTION,
                           "pagesRead": c.get("pagesRead") or [], "placesRunId": places_run["id"]},
            }
            if "maskedPersonalEmails" in c:
                lead["maskedPersonalEmails"] = c["maskedPersonalEmails"]
                lead["maskedTextPhones"] = c.get("maskedTextPhones", 0)
            leads.append(lead)
        strict = ctx.redact
        ctx.redact = False                 # both actors already applied their own policies
        try:
            ctx.push_data(leads)
        finally:
            ctx.redact = strict
        ctx.set_status_message(f"{len(leads)} leads, {sum(1 for x in leads if x['emails'])} with an e-mail")
        ctx.set_value("OUTPUT", {
            "leads": len(leads), "withWebsite": sum(1 for x in leads if x["website"]),
            "withEmail": sum(1 for x in leads if x["emails"]), "withPhone": sum(1 for x in leads if x["phones"]),
            "withSocial": sum(1 for x in leads if x["socialProfiles"]), "placesRunId": places_run["id"],
            "contactRunIds": contact_runs, "attribution": ATTRIBUTION})
