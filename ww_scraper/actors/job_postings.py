"""job-postings: open positions from the public job-board APIs that companies publish for
exactly this purpose (Greenhouse, Lever) and the list an Ashby job board publishes in its own
page, plus schema.org JobPosting data on any careers page. One normalised record per posting, filters for keywords, location and remote,
and an optional watch list that reports only postings new since the previous run.
"""
from __future__ import annotations

import datetime
import hashlib

from ..actor import MIN_INTERVAL, REDACT, Actor
from ..schema import STR_OR_NULL
from ._api import embedded_json, fetch_body, first, get_json, html_to_text, valid_slug
from .structured_data import flatten_jsonld

SOURCES = ("greenhouse", "lever", "ashby")
WATCH_KEY = "SEEN"


def _iso(value):
    """ISO 8601 date-time from an ISO string or epoch milliseconds."""
    if isinstance(value, (int, float)) and value > 0:
        ts = value / 1000 if value > 1e11 else value
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value if isinstance(value, str) and value else None


def _name(v):
    if isinstance(v, dict):
        return v.get("name")
    if isinstance(v, list):
        return ", ".join(x for x in (_name(i) for i in v) if x) or None
    return v if isinstance(v, str) else None


def _remote(*hints):
    text = " ".join(str(h) for h in hints if h).lower()
    if not text:
        return None
    return any(w in text for w in ("remote", "zdaln", "telecommute", "anywhere", "home office"))


def greenhouse(item, company):
    loc = (item.get("location") or {}).get("name")
    return {"source": "greenhouse", "company": company, "id": str(item.get("id")),
            "title": item.get("title"), "url": item.get("absolute_url"), "applyUrl": item.get("absolute_url"),
            "location": loc, "remote": _remote(loc),
            "department": _name(item.get("departments")), "employmentType": None,
            "salary": None, "postedAt": _iso(first(item.get("first_published"), item.get("updated_at"))),
            "description": html_to_text(item.get("content") or "")}


def lever(item, company):
    cat = item.get("categories") or {}
    loc = first(cat.get("location"), ", ".join(cat.get("allLocations") or []))
    salary = item.get("salaryRange") or {}
    pay = None
    if salary.get("min") or salary.get("max"):
        pay = f"{salary.get('min', '')}-{salary.get('max', '')} {salary.get('currency', '')} {salary.get('interval', '')}".strip()
    return {"source": "lever", "company": company, "id": str(item.get("id")),
            "title": item.get("text"), "url": item.get("hostedUrl"), "applyUrl": item.get("applyUrl"),
            "location": loc, "remote": _remote(loc, item.get("workplaceType")),
            "department": first(cat.get("department"), cat.get("team")), "employmentType": cat.get("commitment"),
            "salary": pay, "postedAt": _iso(item.get("createdAt")),
            "description": first(item.get("descriptionPlain"), html_to_text(item.get("description") or "")) or ""}


def ashby(item, company, teams=None, base="https://jobs.ashbyhq.com"):
    """One posting from the list an Ashby board page embeds (window.__appData.jobBoard)."""
    pid = str(item.get("id") or "")
    url = f"{base}/{company}/{pid}" if pid else None
    places = [item.get("locationName")] + [
        (s.get("locationName") if isinstance(s, dict) else s) for s in item.get("secondaryLocations") or []]
    loc = "; ".join(x for x in places if isinstance(x, str) and x) or None
    workplace = item.get("workplaceType")
    remote = True if isinstance(workplace, str) and workplace.lower() == "remote" else _remote(loc)
    team = (teams or {}).get(item.get("teamId"))
    return {"source": "ashby", "company": company, "id": pid,
            "title": item.get("title"), "url": url, "applyUrl": f"{url}/application" if url else None,
            "location": loc, "remote": remote, "workplaceType": workplace,
            "department": team, "employmentType": item.get("employmentType"),
            "salary": item.get("compensationTierSummary"),
            "postedAt": _iso(item.get("publishedDate") or item.get("publishedAt")),
            "description": ""}


def ashby_board(html: str, company: str, base="https://jobs.ashbyhq.com"):
    """Postings from an Ashby board page, or None when the page carries no board data."""
    data = embedded_json(html, "window.__appData")
    board = data.get("jobBoard") if isinstance(data, dict) else None
    posts = board.get("jobPostings") if isinstance(board, dict) else None
    if not isinstance(posts, list):
        return None
    teams = {tm.get("id"): (tm.get("externalName") or tm.get("name"))
             for tm in board.get("teams") or [] if isinstance(tm, dict)}
    return [ashby(i, company, teams, base) for i in posts if isinstance(i, dict) and i.get("isListed", True)]


def jobposting(obj, page_url):
    """A schema.org JobPosting object (Google for Jobs format) from any careers page."""
    org = obj.get("hiringOrganization")
    places = obj.get("jobLocation")
    places = places if isinstance(places, list) else [places] if places else []
    locs = []
    for p in places:
        addr = (p or {}).get("address") if isinstance(p, dict) else None
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), _name(addr.get("addressCountry"))]
            locs.append(", ".join(x for x in parts if isinstance(x, str) and x))
        elif isinstance(addr, str):
            locs.append(addr)
    salary = None
    bs = obj.get("baseSalary")
    if isinstance(bs, dict):
        v = bs.get("value") if isinstance(bs.get("value"), dict) else {"value": bs.get("value")}
        lo, hi = v.get("minValue"), v.get("maxValue")
        amount = f"{lo}-{hi}" if lo is not None and hi is not None else first(v.get("value"), lo, hi)
        if amount is not None:
            salary = f"{amount} {bs.get('currency', '')} {v.get('unitText', '')}".strip()
    remote = obj.get("jobLocationType")
    loc = "; ".join(x for x in locs if x) or None
    ident = obj.get("identifier")
    ident = ident.get("value") if isinstance(ident, dict) else ident
    url = obj.get("url") if isinstance(obj.get("url"), str) else page_url
    return {"source": "schema.org", "company": _name(org), "id": str(ident or hashlib.sha1(
                f"{url}|{obj.get('title')}".encode()).hexdigest()[:16]),
            "title": obj.get("title"), "url": url, "applyUrl": url, "location": loc,
            "remote": True if isinstance(remote, str) and "TELECOMMUTE" in remote.upper() else _remote(loc),
            "department": obj.get("occupationalCategory") if isinstance(obj.get("occupationalCategory"), str) else None,
            "employmentType": _name(obj.get("employmentType")) if not isinstance(obj.get("employmentType"), list)
            else ", ".join(str(x) for x in obj["employmentType"]),
            "salary": salary, "postedAt": _iso(obj.get("datePosted")),
            "validThrough": _iso(obj.get("validThrough")),
            "description": html_to_text(obj.get("description") or "")}


class JobPostings(Actor):
    name = "job-postings"
    version = "1.1.0"
    dataset_schema = {
        "fields": {"type": "object", "required": ["source", "title", "url", "company", "location", "remote", "postedAt"],
                   "properties": {
                       "source": {"type": "string", "enum": ["greenhouse", "lever", "ashby", "schema.org"]},
                       "company": STR_OR_NULL, "id": STR_OR_NULL, "title": STR_OR_NULL, "url": STR_OR_NULL,
                       "applyUrl": STR_OR_NULL, "location": STR_OR_NULL, "remote": {"type": ["boolean", "null"]},
                       "department": STR_OR_NULL, "employmentType": STR_OR_NULL, "salary": STR_OR_NULL,
                       "postedAt": STR_OR_NULL, "description": STR_OR_NULL}},
        "views": {"overview": {"title": "Overview", "fields": ["title", "company", "location", "remote", "salary",
                                                                "postedAt", "applyUrl"]}},
    }
    output_schema = {
        "items": {"title": "Postings", "template": "/v2/datasets/{defaultDatasetId}/items"},
        "overview": {"title": "Overview (CSV)", "template": "/v2/datasets/{defaultDatasetId}/items?view=overview&format=csv"},
        "feed": {"title": "Postings as RSS", "template": "/v2/datasets/{defaultDatasetId}/items?format=rss"},
        "summary": {"title": "Summary (OUTPUT)", "template": "/v2/key-value-stores/{defaultKeyValueStoreId}/records/OUTPUT"},
    }
    title = "Job Postings"
    description = ("Open positions from public job boards (Greenhouse, Lever, Ashby) and "
                   "schema.org JobPosting data on careers pages, normalised, filtered, new-only on request.")
    GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content={}"
    LEVER = "https://api.lever.co/v0/postings/{}?mode=json"
    # api.ashbyhq.com answers robots.txt with 401, which this crawler reads as a refusal; the
    # board page on jobs.ashbyhq.com is allowed and embeds the full list of postings
    ASHBY = "https://jobs.ashbyhq.com/{}"
    input_schema = {
        "type": "object",
        "required": ["boards"],
        "properties": {
            "boards": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 200,
                       "title": "Job boards",
                       "description": "greenhouse:<board>, lever:<company>, ashby:<organization>, "
                                      "or a careers page URL (reads its JobPosting data)"},
            "keywords": {"type": "array", "items": {"type": "string"}, "default": [],
                         "description": "Keep postings whose title contains any of these (case-insensitive)"},
            "locations": {"type": "array", "items": {"type": "string"}, "default": [],
                          "description": "Keep postings whose location contains any of these"},
            "remoteOnly": {"type": "boolean", "default": False},
            "includeDescription": {"type": "boolean", "default": False,
                                   "description": "Full description text from Greenhouse, Lever and careers "
                                                  "pages (large boards answer slower; Ashby lists have none)"},
            "maxJobsPerBoard": {"type": "integer", "default": 1000, "minimum": 1, "maximum": 10000},
            "maxPagesPerSite": {"type": "integer", "default": 30, "minimum": 1, "maximum": 1000,
                                "description": "For careers page URLs: pages read on that site"},
            "watchName": {"type": "string", "default": "", "maxLength": 40,
                          "description": "When set, output only postings not seen in earlier runs "
                                         "with the same watch name"},
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def _board(self, crawler, spec, descriptions: bool):
        source, _, slug = spec.partition(":")
        source = source.lower()
        if source not in SOURCES or not valid_slug(slug):
            raise ValueError(f"{spec!r}: use greenhouse:<board>, lever:<company> or ashby:<organization>")
        if source == "ashby":
            url = self.ASHBY.format(slug)
            body, err = fetch_body(crawler, url, ("text/html", "application/xhtml+xml"))
            if err:
                return url, None, err
            jobs = ashby_board(body.decode("utf-8", "replace"), slug, self.ASHBY.rsplit("/", 1)[0])
            if jobs is None:
                return url, None, "no job board data on the page (wrong organization name?)"
            return url, jobs, None
        if source == "greenhouse":
            # with content=true a large board is several MB; ask for it only when it is wanted
            url = self.GREENHOUSE.format(slug, "true" if descriptions else "false")
        else:
            url = self.LEVER.format(slug)
        data, err = get_json(crawler, url)
        if err:
            return url, None, err
        if source == "lever":
            items = data if isinstance(data, list) else []
        else:
            items = data.get("jobs") if isinstance(data, dict) else None
            items = items if isinstance(items, list) else []
        norm = greenhouse if source == "greenhouse" else lever
        return url, [norm(i, slug) for i in items if isinstance(i, dict)], None

    def _site(self, ctx, crawler, url, max_pages):
        found = []

        def on_page(res, req, doc):
            if doc is None:
                if res.error or (res.status and res.status >= 400):
                    ctx.record_failure(req["url"], res.error or f"HTTP {res.status}")
                return
            for obj in flatten_jsonld(doc.jsonld):
                t = obj.get("@type")
                types = t if isinstance(t, list) else [t]
                if "JobPosting" in types:
                    found.append(jobposting(obj, res.final_url or req["url"]))

        crawler.crawl([url], on_page, max_pages=max_pages, max_depth=2)
        return found

    def run(self, ctx):
        inp = ctx.input
        crawler = ctx.crawler(timeout=90)      # large boards answer slowly
        kw = [k.lower() for k in inp["keywords"] if k.strip()]
        locs = [x.lower() for x in inp["locations"] if x.strip()]
        watch = inp["watchName"].strip()
        if watch and not all(c.isascii() and (c.isalnum() or c in "-_") for c in watch):
            raise ValueError("watchName: use letters, digits, - and _ only")
        store = ctx.storage.kv(f"watch-{watch}") if watch else None
        seen = set(store.get(WATCH_KEY, []) or []) if store else set()
        summary = {"boards": len(inp["boards"]), "fetched": 0, "kept": 0, "new": 0, "failedBoards": 0,
                   "bySource": {}}
        out, keys = [], set()
        for spec in inp["boards"]:
            ctx.check_abort()
            if spec.startswith(("http://", "https://")):
                where, jobs, err = spec, self._site(ctx, crawler, spec, inp["maxPagesPerSite"]), None
            else:
                where, jobs, err = self._board(crawler, spec, inp["includeDescription"])
            if err:
                summary["failedBoards"] += 1
                ctx.record_failure(where, err)
                continue
            jobs = jobs[: inp["maxJobsPerBoard"]]
            summary["fetched"] += len(jobs)
            for j in jobs:
                title = (j.get("title") or "").lower()
                where_text = (j.get("location") or "").lower()
                if kw and not any(k in title for k in kw):
                    continue
                if locs and not any(x in where_text for x in locs):
                    continue
                if inp["remoteOnly"] and j.get("remote") is not True:
                    continue
                key = f"{j['source']}:{j.get('company')}:{j['id']}"
                if key in keys:
                    continue
                keys.add(key)
                j["key"] = key
                j["isNew"] = key not in seen
                if watch and not j["isNew"]:
                    continue
                if not inp["includeDescription"]:
                    j.pop("description", None)
                out.append(j)
                summary["bySource"][j["source"]] = summary["bySource"].get(j["source"], 0) + 1
        summary["kept"] = len(out)
        summary["new"] = sum(1 for j in out if j["isNew"])
        out.sort(key=lambda j: j.get("postedAt") or "", reverse=True)
        ctx.push_data(out)
        if store:
            store.set(WATCH_KEY, sorted(seen | keys))
        ctx.set_value("OUTPUT", summary)
        ctx.log.info(f"job-postings: {summary['fetched']} fetched, {summary['kept']} kept, {summary['new']} new")
