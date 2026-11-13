"""A small filtering forward proxy on 127.0.0.1, used only by the headless browser.

When a page is rendered, the browser fetches the page's scripts and data itself. Without
a guard, a hostile page could make it read http://169.254.169.254/ or a router's admin
page (SSRF through the browser). Every browser connection therefore goes through this
proxy, which applies the same rule as the crawler: the host is resolved once, every
address must be public, and the socket goes to that validated address.

Supports CONNECT (HTTPS tunnels) and plain HTTP requests. It is not a general proxy: it
binds to loopback, serves one render, and counts what it refused.
"""
from __future__ import annotations

import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

from .compliance import SSRFBlocked, resolve_validated

MAX_HEADER = 64 * 1024
IDLE_SECS = 30
ALLOWED_PORTS = (80, 443)


def _connect_validated(host: str, port: int, allow_loopback: bool):
    last = None
    for ip in resolve_validated(host, allow_loopback):
        try:
            return socket.create_connection((ip, port), timeout=15)
        except OSError as e:
            last = e
    raise OSError(f"cannot connect: {last}")


# hop-by-hop headers dropped from a plain-HTTP request; "Connection: close" is sent instead
_HOP_HEADERS = {b"connection", b"proxy-connection", b"keep-alive", b"te", b"upgrade"}


def _relay(a: socket.socket, b: socket.socket, budget: list, a_limit: int = None) -> None:
    """Copy bytes both ways until one side closes. a_limit: at most this many more bytes
    go from a (the browser) to b; after that a is no longer read, so a second request on a
    kept-alive browser connection never reaches the first request's host."""
    socks = [b] if a_limit == 0 else [a, b]
    while True:
        ready, _, bad = select.select(socks, [], socks, IDLE_SECS)
        if bad or not ready:
            return
        for s in ready:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            if s is a and a_limit is not None:
                data, a_limit = data[:a_limit], a_limit - min(a_limit, len(data))
                if a_limit == 0:
                    socks = [b]             # the one request is sent: read only the answer
                if not data:
                    continue
            budget[0] -= len(data)
            if budget[0] < 0:
                return                      # byte cap for the whole render reached
            try:
                (b if s is a else a).sendall(data)
            except OSError:
                return


def _one_request(head: bytes, method: str, path: str):
    """(bytes to send upstream, body bytes still to come from the browser) for a plain-HTTP
    proxy request, rewritten to origin form with "Connection: close"; or (None, reason).
    One request per connection: the upstream connection is pinned to the first host, so
    nothing after this request's body may be relayed on it."""
    headers, _, body = head.partition(b"\r\n\r\n")
    lines = headers.split(b"\r\n")[1:]
    kept, length = [], 0
    for line in lines:
        name = line.split(b":", 1)[0].strip().lower()
        if name in _HOP_HEADERS:
            continue
        if name == b"transfer-encoding":
            return None, "chunked request bodies are not supported"
        if name == b"content-length":
            value = line.split(b":", 1)[1].strip() if b":" in line else b""
            if not value.isdigit():
                return None, "bad Content-Length"
            length = int(value)
        kept.append(line)
    kept.append(b"Connection: close")
    out = f"{method} {path} HTTP/1.1".encode("latin-1") + b"\r\n" + b"\r\n".join(kept) + b"\r\n\r\n"
    return out + body[:length], max(0, length - len(body))


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        proxy = self.server.proxy
        conn = self.request
        conn.settimeout(IDLE_SECS)
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(8192)
                if not chunk:
                    return
                head += chunk
                if len(head) > MAX_HEADER:
                    return self._deny(conn, proxy, "?", "header too large")
        except OSError:
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split(" ")
        if len(parts) != 3:
            return self._deny(conn, proxy, "?", "bad request line")
        method, target, _ = parts
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            rest = b""
        else:
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                return self._deny(conn, proxy, target[:80], "only http and CONNECT")
            host, port = u.hostname, str(u.port or 80)
            path = (u.path or "/") + (("?" + u.query) if u.query else "")
            rest, more = _one_request(head, method, path)
            if rest is None:
                return self._deny(conn, proxy, host, more, code=400)
        host = host.strip("[]")
        try:
            port_n = int(port)
        except ValueError:
            return self._deny(conn, proxy, host, "bad port")
        if port_n not in ALLOWED_PORTS and not (proxy.allow_loopback and port_n == proxy.loopback_port):
            return self._deny(conn, proxy, host, f"port {port_n} not allowed")
        try:
            upstream = _connect_validated(host, port_n, proxy.allow_loopback)
        except SSRFBlocked as e:
            return self._deny(conn, proxy, host, f"ssrf: {e}")
        except OSError as e:
            return self._deny(conn, proxy, host, str(e), code=502, count=False)
        with proxy.lock:
            proxy.allowed.append(f"{host}:{port_n}")
        try:
            if method == "CONNECT":
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                _relay(conn, upstream, proxy.budget)
            else:
                upstream.sendall(rest)
                _relay(conn, upstream, proxy.budget, a_limit=more)
        except OSError:
            pass
        finally:
            upstream.close()

    @staticmethod
    def _deny(conn, proxy, host, reason, code=403, count=True):
        if count:
            with proxy.lock:
                proxy.denied.append({"host": host, "reason": reason})
        try:
            conn.sendall(f"HTTP/1.1 {code} Blocked\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FilteringProxy:
    """with FilteringProxy(...) as p: browser uses http://127.0.0.1:<p.port>."""

    def __init__(self, allow_loopback: bool = False, loopback_port: int = 0,
                 max_bytes: int = 50 * 1024 * 1024):
        self.allow_loopback = bool(allow_loopback)
        self.loopback_port = loopback_port      # the one loopback port tests may reach
        self.budget = [max_bytes]
        self.allowed: list = []
        self.denied: list = []
        self.lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.proxy = self
        self.port = self._server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
