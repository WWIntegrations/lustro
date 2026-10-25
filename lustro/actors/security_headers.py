"""security-headers: one request per origin, grade the HTTP security headers A-F with
reasons. Cookie values are never stored, only their names and flags."""
from __future__ import annotations

import re

from ..actor import MIN_INTERVAL, REDACT, START_URLS, Actor
from ..compliance import origin_of

HSTS_MIN = 15552000  # 180 days
GRADES = ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (50, "E"), (0, "F"))
SAFE_REFERRER = {"no-referrer", "same-origin", "strict-origin",
                 "strict-origin-when-cross-origin", "origin", "origin-when-cross-origin"}


def _csp_directives(csp: str) -> dict:
    out = {}
    for part in csp.split(";"):
        bits = part.strip().split()
        if bits:
            out.setdefault(bits[0].lower(), [b.lower() for b in bits[1:]])
    return out


def grade_headers(headers: dict, final_url: str, cookies=None) -> dict:
    """Pure function. headers: lower-cased names. Returns score, grade, reasons, checks."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    https = final_url.lower().startswith("https://")
    reasons, checks = [], {}
    score = 100

    def bad(points, msg):
        nonlocal score
        score -= points
        reasons.append(f"-{points}: {msg}")

    checks["https"] = https
    if not https:
        bad(30, "final URL is not HTTPS")

    hsts = h.get("strict-transport-security", "")
    checks["hsts"] = hsts or None
    if https:
        if not hsts:
            bad(15, "Strict-Transport-Security (HSTS) missing")
        else:
            m = re.search(r"max-age\s*=\s*\"?(\d+)", hsts, re.I)
            age = int(m.group(1)) if m else 0
            if age < HSTS_MIN:
                bad(5, f"HSTS max-age {age} < {HSTS_MIN} (180 days)")

    csp = h.get("content-security-policy", "")
    checks["csp"] = csp or None
    d = _csp_directives(csp) if csp else {}
    if not csp:
        bad(20, "Content-Security-Policy missing")
    else:
        script = d.get("script-src", d.get("default-src", []))
        has_nonce = any(s.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-")) for s in script)
        if "'unsafe-inline'" in script and not has_nonce:
            bad(5, "CSP allows 'unsafe-inline' scripts")
        if "'unsafe-eval'" in script:
            bad(5, "CSP allows 'unsafe-eval'")
        if not script and "default-src" not in d:
            bad(5, "CSP has no script-src/default-src")

    xfo = h.get("x-frame-options", "").strip().upper()
    checks["xFrameOptions"] = xfo or None
    checks["frameAncestors"] = " ".join(d.get("frame-ancestors", [])) or None
    if "frame-ancestors" not in d:
        if not xfo:
            bad(10, "no clickjacking protection (X-Frame-Options or CSP frame-ancestors)")
        elif xfo not in ("DENY", "SAMEORIGIN"):
            bad(5, f"X-Frame-Options has invalid value {xfo!r}")

    xcto = h.get("x-content-type-options", "").strip().lower()
    checks["xContentTypeOptions"] = xcto or None
    if xcto != "nosniff":
        bad(10, "X-Content-Type-Options: nosniff missing")

    rp = h.get("referrer-policy", "").strip().lower()
    checks["referrerPolicy"] = rp or None
    if not rp:
        bad(5, "Referrer-Policy missing")
    elif rp.split(",")[-1].strip() not in SAFE_REFERRER:
        bad(5, f"Referrer-Policy {rp!r} leaks full URLs")

    pp = h.get("permissions-policy", "")
    checks["permissionsPolicy"] = pp or None
    if not pp:
        bad(5, "Permissions-Policy missing")

    coop = h.get("cross-origin-opener-policy", "")
    corp = h.get("cross-origin-resource-policy", "")
    checks["coop"], checks["corp"] = coop or None, corp or None
    if not coop:
        bad(3, "Cross-Origin-Opener-Policy missing")
    if not corp:
        bad(2, "Cross-Origin-Resource-Policy missing")

    cookies = cookies or []
    checks["cookies"] = cookies
    no_secure = [c["name"] for c in cookies if not c.get("secure")]
    no_http = [c["name"] for c in cookies if not c.get("httponly")]
    no_same = [c["name"] for c in cookies if not c.get("samesite")]
    if https and no_secure:
        bad(min(10, 5 * len(no_secure)), f"cookies without Secure: {', '.join(no_secure[:5])}")
    if no_http:
        bad(min(6, 3 * len(no_http)), f"cookies without HttpOnly: {', '.join(no_http[:5])}")
    if no_same:
        bad(min(4, 2 * len(no_same)), f"cookies without SameSite: {', '.join(no_same[:5])}")

    for name in ("server", "x-powered-by"):
        v = h.get(name, "")
        if re.search(r"\d", v):
            bad(2, f"{name} header discloses a version ({v[:40]})")

    score = max(0, score)
    grade = next(g for floor, g in GRADES if score >= floor)
    return {"score": score, "grade": grade, "reasons": reasons, "checks": checks}


class SecurityHeaders(Actor):
    name = "security-headers"
    title = "Security Headers Grader"
    description = "Grade HTTP security headers per origin (CSP, HSTS, XFO, cookies...) A-F."
    input_schema = {
        "type": "object",
        "required": ["startUrls"],
        "properties": {
            "startUrls": START_URLS,
            "minIntervalSecs": MIN_INTERVAL,
            "redactPii": REDACT,
        },
    }

    def run(self, ctx):
        crawler = ctx.crawler()
        firsts = {}
        for u in ctx.input["startUrls"]:
            firsts.setdefault(origin_of(u), u)
        for origin, url in firsts.items():
            ctx.check_abort()
            res = crawler.fetch(url, accept_types=None, read_body=False)
            if res.error and not res.status:
                ctx.record_failure(url, res.error)
                ctx.push_data({"origin": origin, "url": url, "error": res.error})
                continue
            out = grade_headers(res.headers, res.final_url, res.set_cookies)
            ctx.push_data({
                "origin": origin, "url": url, "finalUrl": res.final_url, "status": res.status,
                "httpsRedirect": url.startswith("http://") and res.final_url.startswith("https://"),
                **out,
            })
