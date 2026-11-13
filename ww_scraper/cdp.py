"""A small Chrome DevTools Protocol client over --remote-debugging-pipe (stdlib only).

Chrome reads commands on file descriptor 3 and writes replies and events on descriptor 4,
each message a JSON object ended by a NUL byte. Nothing listens on a port. A tiny helper
process moves the two pipe ends to descriptors 3 and 4 before it becomes Chrome, so no
code runs between fork and exec in this (threaded) process.

Only what rendering needs is here: open a tab, navigate, evaluate an expression, capture
a screenshot or a PDF, close. Every wait has a deadline.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

MAX_MESSAGE = 96 * 1024 * 1024       # a full-page screenshot in base64 stays well below this

# moves the inherited pipe ends to 3 and 4 (via descriptors >= 100, so neither clobbers the
# other), closes the originals and becomes the browser
_EXEC_HELPER = (
    "import fcntl,os,sys\n"
    "r,w=int(sys.argv[1]),int(sys.argv[2])\n"
    "a=fcntl.fcntl(r,fcntl.F_DUPFD,100);b=fcntl.fcntl(w,fcntl.F_DUPFD,100)\n"
    "os.close(r);os.close(w)\n"
    "os.dup2(a,3);os.dup2(b,4);os.close(a);os.close(b)\n"
    "os.execv(sys.argv[3],sys.argv[3:])\n"
)


class CDPError(RuntimeError):
    pass


class PipeBrowser:
    """A browser process driven over the DevTools pipe. Use as a context manager."""

    def __init__(self, argv: list, deadline: float):
        self.deadline = deadline
        cmd_r, self._cmd_w = os.pipe()            # we write commands, Chrome reads fd 3
        self._evt_r, evt_w = os.pipe()            # Chrome writes fd 4, we read
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _EXEC_HELPER, str(cmd_r), str(evt_w), argv[0],
                 *argv[1:], "--remote-debugging-pipe"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                pass_fds=(cmd_r, evt_w))
        except OSError as e:
            for fd in (cmd_r, evt_w, self._cmd_w, self._evt_r):
                os.close(fd)
            raise CDPError(f"cannot start the browser: {e}") from None
        os.close(cmd_r)
        os.close(evt_w)
        self._next = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._replies: dict = {}
        self.events: list = []
        self._closed = False
        self._error = None
        self.stderr_tail = b""
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    # -- plumbing ---------------------------------------------------------------------------
    def _drain_stderr(self):
        for chunk in iter(lambda: self.proc.stderr.read(4096), b""):
            self.stderr_tail = (self.stderr_tail + chunk)[-4096:]

    def _read(self):
        buf = bytearray()
        try:
            while True:
                chunk = os.read(self._evt_r, 1 << 20)
                if not chunk:
                    break
                buf += chunk
                while True:
                    end = buf.find(b"\0")
                    if end < 0:
                        break
                    raw = bytes(buf[:end])
                    del buf[:end + 1]
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    with self._cond:
                        if "id" in msg:
                            self._replies[msg["id"]] = msg
                        elif len(self.events) < 10000:
                            self.events.append(msg)
                        self._cond.notify_all()
                if len(buf) > MAX_MESSAGE:
                    self._error = "a DevTools message was too large"
                    break
        except OSError as e:
            self._error = f"pipe: {e}"
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def _left(self) -> float:
        return self.deadline - time.monotonic()

    def send(self, method: str, params: dict = None, session: str = None):
        with self._lock:
            self._next += 1
            mid = self._next
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        data = json.dumps(msg).encode("utf-8") + b"\0"
        try:
            while data:
                n = os.write(self._cmd_w, data)
                data = data[n:]
        except OSError as e:
            raise CDPError(f"browser closed the pipe: {e}") from None
        with self._cond:
            while mid not in self._replies:
                if self._closed:
                    raise CDPError(self._error or "browser exited")
                left = self._left()
                if left <= 0:
                    raise CDPError(f"no answer to {method} before the deadline")
                self._cond.wait(min(left, 0.5))
            reply = self._replies.pop(mid)
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result") or {}

    def wait_event(self, method: str, session: str = None, until: float = None):
        """The first event `method` (for `session`) received so far or before `until`."""
        until = min(until or self.deadline, self.deadline)
        with self._cond:
            while True:
                for i, ev in enumerate(self.events):
                    if ev.get("method") == method and (session is None or ev.get("sessionId") == session):
                        return self.events.pop(i)
                if self._closed:
                    raise CDPError(self._error or "browser exited")
                left = until - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(min(left, 0.5))

    def evaluate(self, session: str, expression: str):
        """Value of a JavaScript expression in the page (awaited when it is a promise)."""
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "awaitPromise": True}, session)
        if res.get("exceptionDetails"):
            text = res["exceptionDetails"].get("exception", {}).get("description") or res["exceptionDetails"].get("text")
            raise CDPError(f"script error: {str(text)[:200]}")
        return (res.get("result") or {}).get("value")

    def close(self):
        if self.proc.poll() is None:
            try:
                self.send("Browser.close")
            except CDPError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for fd in (self._cmd_w, self._evt_r):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
