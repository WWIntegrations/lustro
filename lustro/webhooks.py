"""Run webhooks: after a run ends, POST a JSON event to configured URLs.

Hard rules: the target host must be on an allowlist (LUSTRO_WEBHOOK_HOSTS, comma
separated; empty = no webhooks are sent), HTTPS is required (plain http only for a
loopback IP under the explicit test switch), the SSRF guard applies, redirects are not
followed, and every request is signed:

    X-Lustro-Timestamp: <unix seconds>
    X-Lustro-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + body)>

The secret comes from LUSTRO_WEBHOOK_SECRET; without it nothing is sent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlparse

import requests

from .compliance import USER_AGENT, check_target, host_allowed, is_loopback_host

EVENTS = {"SUCCEEDED": "ACTOR.RUN.SUCCEEDED", "FAILED": "ACTOR.RUN.FAILED",
          "ABORTED": "ACTOR.RUN.ABORTED"}


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, max_age: int = 300) -> bool:
    """Receiver-side helper: constant-time check plus a replay window."""
    try:
        if abs(time.time() - int(timestamp)) > max_age:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature or "")


def config_from_env():
    hosts = [h.strip().lower() for h in os.environ.get("LUSTRO_WEBHOOK_HOSTS", "").split(",") if h.strip()]
    return frozenset(hosts), os.environ.get("LUSTRO_WEBHOOK_SECRET", "")


def send(url, payload, *, secret, allow_hosts, allow_loopback=False, timeout=10, session=None):
    """POST payload; returns (ok: bool, info: str). Never raises for network errors."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not secret:
        return False, "refused: LUSTRO_WEBHOOK_SECRET is not set"
    if not allow_hosts or not host_allowed(host, allow_hosts):
        return False, f"refused: host {host!r} not in webhook allowlist"
    loop_ok = allow_loopback and is_loopback_host(host)
    if p.scheme != "https" and not (loop_ok and p.scheme == "http"):
        return False, "refused: webhook URL must be https"
    reason = check_target(url, allow_loopback)
    if reason:
        return False, f"refused: {reason}"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
               "X-Lustro-Timestamp": ts, "X-Lustro-Signature": sign(secret, ts, body)}
    try:
        r = (session or requests).post(url, data=body, headers=headers, timeout=timeout,
                                       allow_redirects=False)
    except requests.RequestException as e:
        return False, f"error: {type(e).__name__}"
    return 200 <= r.status_code < 300, f"HTTP {r.status_code}"


def dispatch(record, webhooks, *, allow_loopback=False, allow_hosts=None, secret=None):
    """Send the run-finished event to each webhook {requestUrl, eventTypes?}.
    Returns log lines; failures never change the run's status."""
    env_hosts, env_secret = config_from_env()
    allow_hosts = env_hosts if allow_hosts is None else frozenset(allow_hosts)
    secret = env_secret if secret is None else secret
    event = EVENTS.get(record.get("status"))
    lines = []
    if not event:
        return lines
    payload = {"eventType": event,
               "eventData": {"actorId": record.get("actId"), "actorRunId": record.get("id")},
               "createdAt": record.get("finishedAt"), "resource": record}
    for wh in webhooks or []:
        if isinstance(wh, str):
            wh = {"requestUrl": wh}
        url = wh.get("requestUrl", "")
        types = wh.get("eventTypes") or list(EVENTS.values())
        if event not in types:
            continue
        ok, info = send(url, payload, secret=secret, allow_hosts=allow_hosts,
                        allow_loopback=allow_loopback)
        lines.append(f"webhook {event} -> {urlparse(url).hostname}: {info}")
    return lines
