"""package-info: metadata of open-source packages from the registries' own JSON APIs:
npm (registry.npmjs.org), RubyGems (rubygems.org/api/v1) and Packagist (packagist.org).
Latest version, its release date, licence, description, homepage, repository, runtime
dependencies, downloads where the registry publishes them, and deprecation.

PyPI is not included on purpose: pypi.org's robots.txt disallows /pypi/*/json, and
ww-scraper never works around robots.txt. Personal fields (maintainer names and e-mail
addresses) are not read.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..actor import REDACT, Actor
from ..schema import STR_OR_NULL
from ._api import first, get_json

SPEC = re.compile(r"(npm|rubygems|packagist):(\S+)")
NPM_NAME = re.compile(r"(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]{0,213}")
GEM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
PACKAGIST_NAME = re.compile(r"[a-z0-9]([_.-]?[a-z0-9]+)*/[a-z0-9](([_.]|-{1,2})?[a-z0-9]+)*")
UNSTABLE = re.compile(r"(^dev-|-dev$|alpha|beta|rc|\.x-dev)", re.I)


def _repo(v):
    if isinstance(v, dict):
        v = v.get("url")
    if not isinstance(v, str) or not v:
        return None
    v = re.sub(r"^git\+", "", v)
    v = re.sub(r"^git://", "https://", v)
    return re.sub(r"\.git$", "", v)


def _license(v):
    if isinstance(v, list):
        names = [x.get("type") if isinstance(x, dict) else x for x in v]
        names = [n for n in names if isinstance(n, str) and n]
        return " OR ".join(names) or None
    if isinstance(v, dict):
        v = v.get("type")
    return v if isinstance(v, str) and v else None


def _record(ecosystem, name, **kw):
    rec = {"ecosystem": ecosystem, "name": name, "version": None, "publishedAt": None, "description": None,
           "license": None, "homepage": None, "repository": None, "dependencies": [], "downloads": None,
           "deprecated": None, "registryUrl": None}
    rec.update(kw)
    rec["dependencyCount"] = len(rec["dependencies"])
    issues = []
    if not rec["license"]:
        issues.append("no licence")
    if not rec["repository"]:
        issues.append("no source repository")
    if rec["deprecated"]:
        issues.append("deprecated")
    rec["issues"] = issues
    return rec


def npm(doc, name, url):
    deps = doc.get("dependencies") if isinstance(doc.get("dependencies"), dict) else {}
    dep = doc.get("deprecated")
    return _record("npm", doc.get("name") or name, version=doc.get("version"),
                   description=doc.get("description") if isinstance(doc.get("description"), str) else None,
                   license=_license(doc.get("license") or doc.get("licenses")),
                   homepage=doc.get("homepage") if isinstance(doc.get("homepage"), str) else None,
                   repository=_repo(doc.get("repository")),
                   dependencies=[f"{k} {v}" for k, v in sorted(deps.items())],
                   deprecated=dep if isinstance(dep, str) and dep else None, registryUrl=url,
                   engines=doc.get("engines") if isinstance(doc.get("engines"), dict) else None)


def rubygems(doc, name, url):
    deps = ((doc.get("dependencies") or {}).get("runtime") or []) if isinstance(doc.get("dependencies"), dict) else []
    return _record("rubygems", doc.get("name") or name, version=doc.get("version"),
                   publishedAt=doc.get("version_created_at"),
                   description=doc.get("info") if isinstance(doc.get("info"), str) else None,
                   license=_license(doc.get("licenses")),
                   homepage=first(doc.get("homepage_uri"), doc.get("project_uri")),
                   repository=_repo(doc.get("source_code_uri")),
                   dependencies=[f"{d.get('name')} {d.get('requirements')}".strip() for d in deps
                                 if isinstance(d, dict) and d.get("name")],
                   downloads=doc.get("downloads") if isinstance(doc.get("downloads"), int) else None,
                   registryUrl=url)


def packagist(doc, name, url):
    pkg = doc.get("package") if isinstance(doc.get("package"), dict) else {}
    versions = pkg.get("versions") if isinstance(pkg.get("versions"), dict) else {}
    stable = [v for k, v in versions.items() if isinstance(v, dict) and not UNSTABLE.search(k)]
    latest = max(stable, key=lambda v: v.get("time") or "", default={})
    req = latest.get("require") if isinstance(latest.get("require"), dict) else {}
    downloads = pkg.get("downloads") if isinstance(pkg.get("downloads"), dict) else {}
    abandoned = pkg.get("abandoned")
    return _record("packagist", pkg.get("name") or name, version=latest.get("version"),
                   publishedAt=latest.get("time"),
                   description=pkg.get("description") if isinstance(pkg.get("description"), str) else None,
                   license=_license(latest.get("license")),
                   homepage=latest.get("homepage") if isinstance(latest.get("homepage"), str) else None,
                   repository=_repo(pkg.get("repository")),
                   dependencies=[f"{k} {v}" for k, v in sorted(req.items())],
                   downloads=downloads.get("total") if isinstance(downloads.get("total"), int) else None,
                   deprecated=(f"abandoned, use {abandoned}" if isinstance(abandoned, str)
                               else "abandoned" if abandoned is True else None),
                   registryUrl=url)


class PackageInfo(Actor):
    name = "package-info"
    version = "1.0.0"
    title = "Package Info"
    description = ("Latest version, licence, repository, dependencies and downloads of npm, RubyGems and "
                   "Packagist packages, from the registries' own JSON APIs.")
    NPM = "https://registry.npmjs.org/{}/latest"
    RUBYGEMS = "https://rubygems.org/api/v1/gems/{}.json"
    PACKAGIST = "https://packagist.org/packages/{}.json"
    input_schema = {
        "type": "object",
        "required": ["packages"],
        "properties": {
            "packages": {"type": "array", "minItems": 1, "maxItems": 500,
                         "items": {"type": "string", "pattern": r"^(npm|rubygems|packagist):\S+$"},
                         "description": "npm:<name> (or npm:@scope/name), rubygems:<gem>, packagist:<vendor>/<name>",
                         "prefill": ["npm:react", "rubygems:rails", "packagist:symfony/console"],
                         "errorMessage": {"pattern": "use npm:<name>, rubygems:<gem> or packagist:<vendor>/<name>"}},
            "redactPii": REDACT,
        },
    }
    dataset_schema = {
        "fields": {"type": "object", "required": ["ecosystem", "name", "version", "issues"],
                   "properties": {"ecosystem": {"type": "string"}, "name": {"type": "string"}, "version": STR_OR_NULL,
                                  "license": STR_OR_NULL, "issues": {"type": "array", "items": {"type": "string"}}}},
        "views": {"overview": {"title": "Overview", "fields": ["ecosystem", "name", "version", "publishedAt",
                                                                "license", "dependencyCount", "repository"]}},
    }

    def _url(self, ecosystem, name):
        if ecosystem == "npm" and NPM_NAME.fullmatch(name):
            return self.NPM.format(quote(name, safe="@"))          # @scope%2Fname, as the registry expects
        if ecosystem == "rubygems" and GEM_NAME.fullmatch(name) and ".." not in name:
            return self.RUBYGEMS.format(name)
        if ecosystem == "packagist" and PACKAGIST_NAME.fullmatch(name):
            return self.PACKAGIST.format(name)
        return None

    def run(self, ctx):
        crawler = ctx.crawler()
        summary = {"requested": 0, "found": 0, "failed": 0, "ecosystems": {}}
        norm = {"npm": npm, "rubygems": rubygems, "packagist": packagist}
        for i, spec in enumerate(dict.fromkeys(ctx.input["packages"])):
            if ctx.aborted:
                break
            summary["requested"] += 1
            m = SPEC.fullmatch(spec)
            url = self._url(m.group(1), m.group(2)) if m else None
            if url is None:
                ctx.record_failure(spec, "not a valid package name for this registry")
                summary["failed"] += 1
                continue
            data, err = get_json(crawler, url)
            if err or not isinstance(data, dict):
                ctx.record_failure(url, err or "unexpected response")
                summary["failed"] += 1
                continue
            rec = norm[m.group(1)](data, m.group(2), url)
            if rec["description"]:
                rec["description"] = ctx.redact_text(rec["description"])
            ctx.push_data(rec)
            summary["found"] += 1
            summary["ecosystems"][m.group(1)] = summary["ecosystems"].get(m.group(1), 0) + 1
            ctx.set_status_message(f"{i + 1}/{len(ctx.input['packages'])} packages read")
        ctx.set_value("OUTPUT", summary)
