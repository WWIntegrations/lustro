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

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .cdp import CDPError, PipeBrowser

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


def _lang_flags(accept_language: str) -> list:
    if not accept_language:
        return []
    first = accept_language.split(",")[0].split(";")[0].strip()
    return [f"--accept-lang={accept_language}", *([f"--lang={first}"] if first else [])]


def _flags(profile: str, proxy_port: int, accept_language: str = "") -> list:
    """Browser flags shared by every rendering path: a throwaway profile, every connection
    through the filtering proxy, no other way out, the crawler's honest User-Agent and,
    when asked, the run's Accept-Language."""
    return [*_lang_flags(accept_language), "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-sync",
            "--disable-background-networking", "--disable-component-update",
            "--disable-domain-reliability", "--metrics-recording-only", "--mute-audio",
            "--hide-scrollbars", f"--user-data-dir={profile}",
            f"--proxy-server=http://127.0.0.1:{proxy_port}",
            "--proxy-bypass-list=<-loopback>",        # loopback goes through the proxy too
            # nothing may leave the browser except through the proxy: no WebRTC/UDP, no
            # QUIC, and no name resolution of its own (the proxy resolves and validates)
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-quic", "--disable-features=WebRtcHideLocalIpsWithMdns,DnsOverHttps",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            f"--user-agent={USER_AGENT}"]


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
        cmd = [browser, *_flags(profile, proxy.port, accept_language), f"--virtual-time-budget={max(0, int(wait_ms))}",
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


# --- interactive rendering over the DevTools pipe ---------------------------------------

MAX_ACTIONS = 20
MAX_WAIT_MS = 15000
MAX_SELECTOR = 300
MAX_SHOT_HEIGHT = 16384
VIEWPORT = (1280, 800)
ACTIONS_HELP = ('allowed actions: {"click": "<css selector>"}, {"scroll": "bottom" or pixels}, '
                '{"wait": milliseconds <= 15000}, {"waitForSelector": "<css selector>"}')


def validate_actions(actions) -> list:
    """Errors for a browserActions list (empty when it is fine). Only clicks, scrolls and
    waits exist: no typing, no form filling, no script of the caller's own."""
    if actions in (None, []):
        return []
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        return [f"browserActions: expected a list of at most {MAX_ACTIONS} actions"]
    errors = []
    for i, act in enumerate(actions):
        if not isinstance(act, dict) or len(act) != 1:
            errors.append(f"browserActions[{i}]: one action per object; {ACTIONS_HELP}")
            continue
        (kind, arg), = act.items()
        if kind in ("click", "waitForSelector"):
            if not isinstance(arg, str) or not arg.strip() or len(arg) > MAX_SELECTOR:
                errors.append(f"browserActions[{i}].{kind}: expected a CSS selector")
        elif kind == "scroll":
            if arg != "bottom" and not (isinstance(arg, int) and not isinstance(arg, bool) and 0 <= arg <= 100000):
                errors.append(f"browserActions[{i}].scroll: expected \"bottom\" or pixels")
        elif kind == "wait":
            if not (isinstance(arg, int) and not isinstance(arg, bool) and 0 <= arg <= MAX_WAIT_MS):
                errors.append(f"browserActions[{i}].wait: expected milliseconds up to {MAX_WAIT_MS}")
        else:
            errors.append(f"browserActions[{i}]: unknown action {kind!r}; {ACTIONS_HELP}")
    return errors


class RenderResult:
    __slots__ = ("html", "screenshot", "pdf", "denied", "notes")

    def __init__(self):
        self.html, self.screenshot, self.pdf, self.denied, self.notes = "", None, None, [], []


def _wait_for(browser, session, selector, until) -> bool:
    expr = f"!!document.querySelector({json.dumps(selector)})"
    while time.monotonic() < until:
        try:
            if browser.evaluate(session, expr):
                return True
        except CDPError:
            pass                                   # the page may be navigating
        time.sleep(0.2)
    return False


def _b64(data: str) -> bytes:
    return base64.b64decode(data)


def render_page(url: str, *, allow_loopback: bool = False, wait_ms: int = 3000, timeout: float = 60,
                max_bytes: int = 5 * 1024 * 1024, wait_for: str = None, actions=None,
                screenshot: str = None, image_format: str = "png", pdf: bool = False,
                accept_language: str = "") -> RenderResult:
    """Render a page over the DevTools pipe: load it, wait wait_ms for its scripts, wait for
    `wait_for` (a CSS selector), run `actions`, then read the DOM and optionally capture a
    screenshot ("viewport" or "fullPage") and a PDF. Same proxy, flags and limits as
    render_dom; raises RenderError on failure."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RenderError("only http(s) URLs can be rendered")
    errors = validate_actions(actions)
    if wait_for is not None and (not isinstance(wait_for, str) or len(wait_for) > MAX_SELECTOR):
        errors.append("waitForSelector: expected a CSS selector")
    if screenshot not in (None, "viewport", "fullPage") or image_format not in ("png", "jpeg"):
        errors.append("screenshot: expected viewport or fullPage, png or jpeg")
    if errors:
        raise RenderError("; ".join(errors))
    browser_path = find_browser()
    if browser_path is None:
        raise RenderError("no Chrome or Chromium found (set WW_SCRAPER_CHROME)")
    extra = shlex.split(os.environ.get("WW_SCRAPER_CHROME_ARGS", ""))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    out = RenderResult()
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="ww-scraper-chrome-") as profile, \
            FilteringProxy(allow_loopback, loopback_port=port) as proxy:
        argv = [browser_path, *_flags(profile, proxy.port, accept_language), f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
                *extra, "about:blank"]
        try:
            with PipeBrowser(argv, deadline) as b:
                target = b.send("Target.createTarget", {"url": "about:blank"})["targetId"]
                session = b.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
                b.send("Page.enable", session=session)
                b.send("Emulation.setDeviceMetricsOverride", {"width": VIEWPORT[0], "height": VIEWPORT[1],
                                                              "deviceScaleFactor": 1, "mobile": False}, session)
                nav = b.send("Page.navigate", {"url": url}, session)
                if nav.get("errorText"):
                    raise RenderError(f"navigation failed: {nav['errorText']}")
                if b.wait_event("Page.loadEventFired", session) is None:
                    out.notes.append("load event not seen before the deadline")
                settle = min(time.monotonic() + max(0, wait_ms) / 1000, deadline - 2)
                while time.monotonic() < settle:
                    time.sleep(0.05)
                if wait_for and not _wait_for(b, session, wait_for, deadline - 2):
                    out.notes.append(f"waitForSelector {wait_for!r} not found")
                for act in actions or []:
                    (kind, arg), = act.items()
                    if kind == "click":
                        hit = b.evaluate(session, f"(() => {{ const e = document.querySelector({json.dumps(arg)}); "
                                                  "if (!e) return false; e.click(); return true; })()")
                        if not hit:
                            out.notes.append(f"click {arg!r}: no such element")
                    elif kind == "scroll":
                        to = "document.documentElement.scrollHeight" if arg == "bottom" else str(int(arg))
                        b.evaluate(session, f"window.scrollTo(0, {to}); true")
                    elif kind == "wait":
                        time.sleep(min(arg / 1000, max(0, deadline - 2 - time.monotonic())))
                    elif kind == "waitForSelector" and not _wait_for(b, session, arg, deadline - 2):
                        out.notes.append(f"waitForSelector {arg!r} not found")
                size = b.evaluate(session, "document.documentElement ? document.documentElement.outerHTML.length : 0")
                if not isinstance(size, int) or size > max_bytes:
                    raise RenderError(f"rendered page exceeds {max_bytes} bytes")
                out.html = b.evaluate(session, "document.documentElement.outerHTML") or ""
                if screenshot:
                    params = {"format": image_format, "captureBeyondViewport": screenshot == "fullPage"}
                    if image_format == "jpeg":
                        params["quality"] = 80
                    if screenshot == "fullPage":
                        m = b.send("Page.getLayoutMetrics", session=session)
                        css = m.get("cssContentSize") or m.get("contentSize") or {}
                        height = min(int(css.get("height") or VIEWPORT[1]), MAX_SHOT_HEIGHT)
                        params["clip"] = {"x": 0, "y": 0, "width": VIEWPORT[0], "height": max(1, height), "scale": 1}
                    out.screenshot = _b64(b.send("Page.captureScreenshot", params, session)["data"])
                if pdf:
                    out.pdf = _b64(b.send("Page.printToPDF", {"printBackground": True}, session)["data"])
        except CDPError as e:
            raise RenderError(str(e)) from None
        out.denied = list(proxy.denied)
    if not out.html.strip():
        raise RenderError("browser returned an empty document")
    if len(out.html.encode("utf-8")) > max_bytes:
        raise RenderError(f"rendered page exceeds {max_bytes} bytes")
    return out


# --- "render": "auto" --------------------------------------------------------------------

_SHELL_IDS = re.compile(r"<div[^>]+id=[\"']?(root|app|__next|__nuxt|svelte|main-app)[\"']?[^>]*>\s*</div>", re.I)
_NOSCRIPT_JS = re.compile(r"<noscript[^>]*>[^<]{0,300}(enable|turn on|włącz)[^<]{0,40}javascript", re.I)
_TAGS = re.compile(r"<(script|style|template|noscript)\b.*?</\1\s*>|<[^>]+>", re.I | re.S)


def visible_words(html: str) -> int:
    """Rough count of words a reader sees (scripts, styles and tags removed)."""
    return len(_TAGS.sub(" ", html or "").split())


def needs_render(html: str) -> bool:
    """True when raw HTML looks built by scripts: an empty app root, a noscript notice
    asking for JavaScript, or almost no text next to scripts."""
    if not html:
        return False
    if _SHELL_IDS.search(html) or _NOSCRIPT_JS.search(html):
        return True
    scripts = html.lower().count("<script")
    return scripts > 0 and visible_words(html) < 40
