"""Post-run checks: conditions a finished run must meet, evaluated after it ends.

    "checks": {"minItems": 1, "maxItems": 5000, "maxFailed": 5, "maxFailedRatio": 0.2,
               "fieldFillRate": {"email": 0.5, "address.city": 0.9},
               "statuses": ["SUCCEEDED"], "maxDurationSecs": 600}

The result is written to the run record as `checks` ({"passed", "results"}); the CLI exits
with code 3 when a check failed, and webhooks get ACTOR.RUN.CHECKS_FAILED. A run whose items
suddenly drop to zero, or whose e-mail column empties, raises an alert instead of passing
silently.
"""
from __future__ import annotations

import math

KNOWN = {"minItems", "maxItems", "maxFailed", "maxFailedRatio", "fieldFillRate", "statuses",
         "maxDurationSecs"}


def _number(v) -> bool:
    """A finite int or float (NaN compares False with every bound, so it is refused)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate(checks) -> list:
    if not isinstance(checks, dict):
        return ["checks: expected an object"]
    errors = [f"checks.{k}: unknown check" for k in checks if k not in KNOWN]
    for k in ("minItems", "maxItems", "maxFailed"):
        if k in checks and (not isinstance(checks[k], int) or isinstance(checks[k], bool) or checks[k] < 0):
            errors.append(f"checks.{k}: expected a whole number >= 0")
    for k in ("maxFailedRatio", "maxDurationSecs"):
        if k in checks and (not _number(checks[k]) or checks[k] < 0):
            errors.append(f"checks.{k}: expected a number >= 0")
    if "maxFailedRatio" in checks and isinstance(checks["maxFailedRatio"], (int, float)) and checks["maxFailedRatio"] > 1:
        errors.append("checks.maxFailedRatio: must be <= 1")
    rates = checks.get("fieldFillRate", {})
    if not isinstance(rates, dict) or not all(_number(v) and 0 <= v <= 1 for v in rates.values()):
        errors.append("checks.fieldFillRate: expected {field: share between 0 and 1}")
    if "statuses" in checks and (not isinstance(checks["statuses"], list)
                                 or not all(isinstance(x, str) for x in checks["statuses"])):
        errors.append("checks.statuses: expected a list of statuses")
    return errors


def _filled(item, path) -> bool:
    cur = item
    if path in item:
        cur = item[path]
    else:
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
    return cur not in (None, "", [], {})


def evaluate(checks: dict, record: dict, items) -> dict:
    """{"passed": bool, "results": [{check, expected, actual, passed}]}. items is an iterable
    of the run's items (read once)."""
    results = []

    def add(name, expected, actual, ok):
        results.append({"check": name, "expected": expected, "actual": actual, "passed": bool(ok)})

    stats = record.get("stats") or {}
    count = record.get("itemCount", 0)
    if "statuses" in checks:
        add("statuses", checks["statuses"], record.get("status"), record.get("status") in checks["statuses"])
    if "minItems" in checks:
        add("minItems", checks["minItems"], count, count >= checks["minItems"])
    if "maxItems" in checks:
        add("maxItems", checks["maxItems"], count, count <= checks["maxItems"])
    failed = stats.get("failed", 0)
    if "maxFailed" in checks:
        add("maxFailed", checks["maxFailed"], failed, failed <= checks["maxFailed"])
    if "maxFailedRatio" in checks:
        tried = max(1, stats.get("pages", 0) + failed)
        ratio = round(failed / tried, 4)
        add("maxFailedRatio", checks["maxFailedRatio"], ratio, ratio <= checks["maxFailedRatio"])
    if "maxDurationSecs" in checks:
        secs = round((stats.get("durationMillis") or 0) / 1000, 3)
        add("maxDurationSecs", checks["maxDurationSecs"], secs, secs <= checks["maxDurationSecs"])
    rates = checks.get("fieldFillRate") or {}
    if rates:
        filled = dict.fromkeys(rates, 0)
        n = 0
        for it in items:
            n += 1
            for f in rates:
                filled[f] += _filled(it, f)
        for f, want in rates.items():
            got = round(filled[f] / n, 4) if n else 0.0
            add(f"fieldFillRate.{f}", want, got, got >= want)
    return {"passed": all(r["passed"] for r in results), "results": results}
