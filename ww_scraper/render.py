"""Optional JavaScript rendering with a locally installed Chrome or Chromium.

render_dom(url) starts a headless browser with a throwaway profile, lets the page's
scripts run for a bounded time and returns the resulting DOM as HTML. It is off by
default (`"render": true` in an actor input turns it on).

What stays true while rendering:
- the page URL itself was already checked by the crawler (SSRF guard, robots.txt, rate
  limit) before the browser is started;
- every connection the browser opens goes through ww_scraper/proxy.py, which refuses
  private, loopback and link-local addresses;
- the browser announces the same honest User-Agent as the crawler; nothing is done to
  look like a person or to get past a block.

What rendering cannot promise: the page's own sub-requests (scripts, XHR) are made by the
browser as any visitor's browser would make them; they are not checked against robots.txt
one by one.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

from .compliance import USER_AGENT
from .proxy import FilteringProxy

_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)


class RenderError(RuntimeError):
    pass


def find_browser():
    """Path of a usable browser binary, or None. $WW_SCRAPER_CHROME overrides the search."""
    override = os.environ.get("WW_SCRAPER_CHROME")
    for cand in ((override,) if override else _CANDIDATES):
        if not cand:
            continue
        path = cand if os.path.isabs(cand) else shutil.which(cand)
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _run_capped(cmd, timeout: float, max_bytes: int):
    """Run the browser, reading stdout in chunks; kill it as soon as the output passes
    max_bytes or the time is up. Returns (stdout, exit code, last stderr line)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        raise RenderError(f"cannot start the browser: {e}") from None
    err = bytearray()

    def drain():
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            del err[:max(0, len(err) + len(chunk) - 8192)]      # keep only the tail
            err.extend(chunk)

    threading.Thread(target=drain, daemon=True).start()
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    out = bytearray()
    too_big = False
    try:
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            out.extend(chunk)
            if len(out) > max_bytes:
                too_big = True
                proc.kill()
                break
        code = proc.wait()
    finally:
        timed_out = not killer.is_alive() and not too_big and proc.poll() is not None and proc.returncode < 0
        killer.cancel()
        proc.stdout.close()
    if too_big:
        raise RenderError(f"rendered page exceeds {max_bytes} bytes")
    if timed_out:
        raise RenderError(f"browser did not finish in {timeout:.0f}s")
    tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    return bytes(out), code, tail[0]


def render_dom(url: str, *, allow_loopback: bool = False, wait_ms: int = 3000,
               timeout: float = 45, max_bytes: int = 5 * 1024 * 1024,
               accept_language: str = "") -> str:
    """HTML of the page after its scripts ran. Raises RenderError when no browser is
    installed, the browser fails, times out or returns more than max_bytes."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RenderError("only http(s) URLs can be rendered")
    browser = find_browser()
    if browser is None:
        raise RenderError("no Chrome or Chromium found (set WW_SCRAPER_CHROME)")
    extra = shlex.split(os.environ.get("WW_SCRAPER_CHROME_ARGS", ""))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    with tempfile.TemporaryDirectory(prefix="ww-scraper-chrome-") as profile, \
            FilteringProxy(allow_loopback, loopback_port=port) as proxy:
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
               "--no-default-browser-check", "--disable-extensions", "--disable-sync",
               "--disable-background-networking", "--disable-component-update",
               "--disable-domain-reliability", "--metrics-recording-only", "--mute-audio",
               "--hide-scrollbars", f"--user-data-dir={profile}",
               f"--proxy-server=http://127.0.0.1:{proxy.port}",
               "--proxy-bypass-list=<-loopback>",        # loopback goes through the proxy too
               # nothing may leave the browser except through the proxy: no WebRTC/UDP, no
               # QUIC, and no name resolution of its own (the proxy resolves and validates)
               "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
               "--webrtc-ip-handling-policy=disable_non_proxied_udp",
               "--disable-quic", "--disable-features=WebRtcHideLocalIpsWithMdns,DnsOverHttps",
               "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
               f"--user-agent={USER_AGENT}",
               *([f"--accept-lang={accept_language}", f"--lang={accept_language.split(',')[0].split(';')[0].strip()}"]
                 if accept_language else []),
               f"--virtual-time-budget={max(0, int(wait_ms))}",
               *extra, "--dump-dom", url]
        out, code, err_tail = _run_capped(cmd, timeout, max_bytes)
        if code != 0:
            # Chrome sometimes dies during start-up on a busy machine: one more try,
            # with a fresh profile directory, before reporting the failure
            with tempfile.TemporaryDirectory(prefix="ww-scraper-chrome-") as retry_profile:
                cmd = [f"--user-data-dir={retry_profile}" if c.startswith("--user-data-dir=") else c
                       for c in cmd]
                out, code, err_tail = _run_capped(cmd, timeout, max_bytes)
        denied = list(proxy.denied)
    if code != 0:
        raise RenderError(f"browser exited with {code}: {err_tail[:200]}")
    html = out.decode("utf-8", "replace")
    if not html.strip():
        raise RenderError("browser returned an empty document")
    render_dom.last_denied = denied              # for the run log and tests
    return html


render_dom.last_denied = []
