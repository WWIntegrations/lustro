"""contact-details: the contact channels an organisation publishes on its own website:
e-mail addresses, phone numbers, social profiles, address, from the home page and the
pages it links as contact, about, imprint or team pages.

GDPR by design. With redactPii on (the default):
- role addresses (info@, kontakt@, biuro@, sales@, ...) are kept: they name a function,
  not a person;
- personal-looking addresses (jan.kowalski@, j.kowalski@) are masked and only counted;
- phone numbers are kept when the organisation published them as a tel: link or in its
  schema.org Organization data, and masked when they only appear in running text (a
  staff member's mobile in a bio is the typical case).
With redactPii off, everything found is returned; that is the operator's legal decision.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from ..actor import BROWSER_ACTIONS, MIN_INTERVAL, REDACT, RENDER, START_URLS, WAIT_FOR, Actor
from ..compliance import _EMAIL_RE, _PHONE_RE, _phone_sub, redact_obj
from ..schema import STR_OR_NULL
from ..schema_types import is_organization_type
from ..storage import MemoryRequestQueue
from .structured_data import flatten_jsonld

ROLE_LOCALS = {
    "info", "kontakt", "contact", "office", "biuro", "hello", "hi", "hej", "czesc", "sales", "sprzedaz",
    "support", "pomoc", "help", "helpdesk", "service", "serwis", "admin", "administrator", "rodo", "iod",
    "dpo", "privacy", "gdpr", "press", "media", "pr", "marketing", "hr", "jobs", "careers", "kariera",
    "praca", "rekrutacja", "recruitment", "billing", "faktury", "invoice", "invoices", "ksiegowosc",
    "accounting", "finance", "team", "recepcja", "reception", "sekretariat", "zamowienia", "orders",
    "order", "shop", "sklep", "enquiries", "inquiries", "mail", "poczta", "kancelaria", "partners",
    "partner", "partnerships", "business", "biz", "b2b", "export", "legal", "compliance", "security",
    "abuse", "webmaster", "noreply", "no-reply", "newsletter", "events", "rezerwacje", "booking",
    "reservations", "contact-us", "customerservice", "obsluga", "bok", "dział", "dzial", "studio",
}
SOCIAL = {
    "linkedin.com": "linkedin", "facebook.com": "facebook", "instagram.com": "instagram",
    "x.com": "x", "twitter.com": "x", "youtube.com": "youtube", "tiktok.com": "tiktok",
    "github.com": "github", "pinterest.com": "pinterest", "threads.net": "threads",
    "behance.net": "behance", "dribbble.com": "dribbble", "vimeo.com": "vimeo",
}
SKIP_SOCIAL_PATHS = ("/sharer", "/share", "/intent", "/dialog", "/plugins", "/login", "/signup")
DEFAULT_PATTERNS = ["*kontakt*", "*contact*", "*about*", "*o-nas*", "*o-firmie*", "*impressum*",
                    "*imprint*", "*team*", "*zespol*", "*zespół*", "*company*", "*firma*"]


def is_role_address(addr: str) -> bool:
    local = addr.split("@", 1)[0].lower()
    base = re.split(r"[+]", local)[0]
    if base in ROLE_LOCALS:
        return True
    return any(base.startswith(r + sep) or base.endswith(sep + r) for r in ROLE_LOCALS for sep in ".-_")


def social_profile(url: str):
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("pl.")
    net = SOCIAL.get(host)
    if not net or len(p.path.strip("/")) == 0 or p.path.lower().startswith(SKIP_SOCIAL_PATHS):
        return None
    return net, f"https://{host}{p.path.rstrip('/')}"


def _clean_phone(raw: str) -> str:
    raw = unquote(raw).strip()
    keep = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    return keep if 9 <= sum(ch.isdigit() for ch in keep) <= 15 else ""


class ContactDetails(Actor):
    name = "contact-details"
    version = "1.1.0"
    dataset_schema = {
        "fields": {"type": "object", "required": ["startUrl", "emails", "phones", "socialProfiles", "pagesRead"],
                   "properties": {
                       "startUrl": {"type": "string"}, "organization": STR_OR_NULL, "legalName": STR_OR_NULL,
                       "vatId": STR_OR_NULL, "address": STR_OR_NULL,
                       "emails": {"type": "array", "items": {"type": "object", "required": ["address", "role"],
                                                              "properties": {"address": {"type": "string"},
                                                                             "role": {"type": "boolean"}}}},
                       "phones": {"type": "array", "items": {"type": "object", "required": ["number"]}},
                       "socialProfiles": {"type": "object"}, "pagesRead": {"type": "array", "items": {"type": "string"}},
                       "maskedPersonalEmails": {"type": "integer", "minimum": 0},
                       "maskedTextPhones": {"type": "integer", "minimum": 0}}},
        "views": {"overview": {"title": "Overview", "flatten": True,
                               "fields": ["startUrl", "organization", "vatId", "address", "socialProfiles.linkedin",
                                          "socialProfiles.facebook", "socialProfiles.instagram"]}},
    }
    title = "Contact Details"
    description = ("E-mails, phone numbers, social profiles and address an organisation publishes on "
                   "its own website. Role addresses kept, personal ones masked by default (GDPR).")
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "maxPagesPerSite": {"type": "integer", "default": 8, "minimum": 1, "maximum": 100},
            "pagePatterns": {"type": "array", "items": {"type": "string"}, "default": DEFAULT_PATTERNS,
                             "description": "Besides the start page, read linked pages matching one of these globs"},
            "minIntervalSecs": MIN_INTERVAL,
            "render": RENDER,
            "waitForSelector": WAIT_FOR,
            "browserActions": BROWSER_ACTIONS,
            "redactPii": REDACT,
        },
    }

    def site(self, ctx, start: str) -> dict:
        strict = ctx.redact
        emails, masked, phones, text_phones, social, sources = {}, set(), {}, set(), {}, set()
        org = {"name": None, "address": None, "legalName": None, "vatId": None}

        def add_email(addr, where):
            addr = addr.strip().strip(".").lower()
            if not _EMAIL_RE.fullmatch(addr):
                return
            if strict and not is_role_address(addr):
                masked.add(addr)
                return
            emails.setdefault(addr, where)

        def on_page(res, req, doc):
            if doc is None:
                if res.error or (res.status and res.status >= 400):
                    ctx.record_failure(req["url"], res.error or f"HTTP {res.status}")
                return
            url = res.final_url or req["url"]
            sources.add(url)
            for href in doc.contact_links:
                scheme, _, rest = href.partition(":")
                target = unquote(rest.split("?", 1)[0])
                if scheme.lower() == "mailto":
                    for a in target.split(","):
                        add_email(a, url)
                elif scheme.lower() == "tel" and (p := _clean_phone(target)):
                    phones.setdefault(p, url)
            text = (doc.markdown or "") + "\n" + (doc.contact_text or "")
            for m in _EMAIL_RE.finditer(text):
                add_email(m.group(0), url)
            for m in _PHONE_RE.finditer(text):
                if _phone_sub(m) != m.group(0) and (p := _clean_phone(m.group(0))):
                    text_phones.add(p)
            for link in doc.links:
                sp = social_profile(link["url"])
                if sp:
                    social.setdefault(sp[0], sp[1])
            for obj in flatten_jsonld(doc.jsonld):
                types = obj.get("@type")
                types = types if isinstance(types, list) else [types]
                if not any(is_organization_type(t) for t in types):
                    continue
                org["name"] = org["name"] or (obj.get("name") if isinstance(obj.get("name"), str) else None)
                org["legalName"] = org["legalName"] or obj.get("legalName")
                org["vatId"] = org["vatId"] or obj.get("vatID") or obj.get("taxID")
                tel = obj.get("telephone")
                for t in (tel if isinstance(tel, list) else [tel]):
                    if isinstance(t, str) and (p := _clean_phone(t)):
                        phones.setdefault(p, url)
                mail = obj.get("email")
                for e in (mail if isinstance(mail, list) else [mail]):
                    if isinstance(e, str):
                        add_email(e.removeprefix("mailto:"), url)
                addr = obj.get("address")
                if isinstance(addr, dict) and not org["address"]:
                    org["address"] = ", ".join(str(addr[k]) for k in ("streetAddress", "postalCode", "addressLocality",
                                                                       "addressCountry") if isinstance(addr.get(k), str))
                same = obj.get("sameAs")
                for s in (same if isinstance(same, list) else [same]):
                    if isinstance(s, str) and (sp := social_profile(s)):
                        social.setdefault(sp[0], sp[1])

        include = [start] + [g for g in ctx.input["pagePatterns"]]
        # a queue of its own per site: the run's durable queue counts the pages of every
        # site before this one, so from the third or fourth site on nothing was read
        ctx.crawler().crawl([start], on_page, max_pages=ctx.input["maxPagesPerSite"], max_depth=1,
                            include=include, queue=MemoryRequestQueue())
        only_text = sorted(text_phones - set(phones))
        rec = {
            "startUrl": start, "organization": org["name"], "legalName": org["legalName"], "vatId": org["vatId"],
            "address": org["address"],
            "emails": [{"address": a, "foundOn": w, "role": is_role_address(a)} for a, w in emails.items()],
            "phones": [{"number": p, "foundOn": w} for p, w in phones.items()],
            "socialProfiles": social, "pagesRead": sorted(sources),
        }
        if strict:
            rec["maskedPersonalEmails"] = len(masked)
            rec["maskedTextPhones"] = len(only_text)
        else:
            rec["emails"] += [{"address": a, "foundOn": None, "role": False} for a in sorted(masked)]
            rec["phones"] += [{"number": p, "foundOn": None} for p in only_text]
        return rec

    def run(self, ctx):
        strict = ctx.redact
        ctx.redact = False                 # this actor applies its own, field-aware policy above
        summary = {"sites": 0, "withEmail": 0, "withPhone": 0, "withSocial": 0}
        try:
            for start in ctx.input["startUrls"]:
                ctx.check_abort()
                ctx.redact = strict
                rec = self.site(ctx, start)
                ctx.redact = False
                if strict:                 # free-text fields still go through the normal redaction
                    for k in ("organization", "legalName", "address"):
                        rec[k] = redact_obj(rec[k], k)
                ctx.push_data(rec)
                summary["sites"] += 1
                summary["withEmail"] += bool(rec["emails"])
                summary["withPhone"] += bool(rec["phones"])
                summary["withSocial"] += bool(rec["socialProfiles"])
        finally:
            ctx.redact = strict
        ctx.set_value("OUTPUT", summary)
