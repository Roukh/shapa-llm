"""The hosted-remote HTTP layer: standard library ``urllib`` only.

A *transport* is any callable ``transport(method, url, headers, body, timeout)
-> (status, headers, body_bytes)``; :func:`urllib_transport` is the real one
and tests inject an in-process fake, so no test needs a socket. :class:`Client`
adds the rules every call shares:

- HTTPS is required, except for ``http://127.0.0.1`` and ``http://localhost``.
- A redirect is never followed (it could carry the token to another host).
- A response is size-capped, parsed defensively, and any error text the
  server sends is stripped of control characters and of the client's own
  secrets before it can reach a message.
- A call never outlives the caller's *deadline*.

The token and the owner key travel only in the ``Authorization`` header; they
are never put in a URL, a log line, an exception or ``--json`` output.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from shapa import __version__

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


class RemoteError(Exception):
    """A failed call to the hosted store. ``status`` is the HTTP status (0
    when there was no response); ``code`` the server's error code."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def clean_text(text, limit: int = 200) -> str:
    """One printable line: control characters dropped, whitespace collapsed."""
    return " ".join(_CONTROL_RE.sub(" ", str(text)).split())[:limit]


def check_url(url: str) -> str:
    """The normalized base URL, or :class:`RemoteError` for one the client
    must not talk to (plain HTTP to a non-local host, credentials or a query
    in the URL, no host)."""
    try:
        parts = urlsplit((url or "").strip())
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise RemoteError(f"invalid server URL: {exc}") from exc
    if parts.scheme not in ("https", "http") or not host:
        raise RemoteError("the server URL must start with https://")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise RemoteError("plain http:// is only allowed for localhost; use https://")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise RemoteError("the server URL must not carry credentials, a query or a fragment")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(method, url, headers, body, timeout):
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        resp = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        resp = exc
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise RemoteError(f"server unreachable ({type(exc).__name__})") from None
    try:
        data = resp.read(MAX_RESPONSE_BYTES + 1)
        headers_out = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status if hasattr(resp, "status") else resp.code, headers_out, data
    except (http.client.HTTPException, OSError) as exc:
        raise RemoteError(f"server unreachable ({type(exc).__name__})") from None
    finally:
        resp.close()


class Client:
    def __init__(self, url: str, token: str, *, transport=None, deadline: float | None = None):
        self.url = check_url(url)
        self._token = token
        self.transport = transport or urllib_transport
        self.deadline = deadline  # absolute time.monotonic() value

    def __repr__(self) -> str:
        return f"Client({self.url!r})"

    def _scrub(self, text) -> str:
        text = clean_text(text)
        return text.replace(self._token, "[redacted]") if self._token else text

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def request(self, method: str, path: str, payload=None, *, timeout: float = 15.0,
                accept: str = "application/json"):
        """Call ``METHOD path``; returns the parsed JSON object, or the text
        for a non-JSON ``accept``. Raises :class:`RemoteError`."""
        left = self.remaining()
        if left is not None:
            if left <= 0:
                raise RemoteError("time budget exhausted")
            timeout = min(timeout, left)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": accept,
                   "User-Agent": f"shapa/{__version__}"}
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, _hdrs, data = self.transport(method, self.url + path, headers, body, timeout)
        if not isinstance(status, int) or not isinstance(data, (bytes, bytearray)):
            raise RemoteError("invalid response from the server")
        if len(data) > MAX_RESPONSE_BYTES:
            raise RemoteError("the server's response is too large")
        if 300 <= status < 400:
            raise RemoteError("the server redirected the request; redirects are not followed", status)
        if status >= 400:
            raise self._http_error(status, data)
        if accept != "application/json":
            return bytes(data).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(bytes(data).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise RemoteError("the server sent a response that is not JSON", status) from None
        if not isinstance(parsed, dict):
            raise RemoteError("unexpected response shape from the server", status)
        return parsed

    def _http_error(self, status: int, data) -> RemoteError:
        code = message = ""
        try:
            err = json.loads(bytes(data).decode("utf-8")).get("error")
            if isinstance(err, dict):
                code, message = self._scrub(err.get("code", ""))[:40], self._scrub(err.get("message", ""))
        except (ValueError, UnicodeDecodeError, AttributeError):
            pass
        hint = {401: "not authorized - run `shapa remote login` again",
                403: "forbidden for this device", 429: "rate limited, try again later"}.get(status, "")
        text = f"server answered {status}" + (f": {hint}" if hint else "")
        if message:
            text += f" ({message})"
        return RemoteError(text, status, code)
