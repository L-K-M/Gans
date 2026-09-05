"""Minimal synchronous HTTP client for the Ente API (``urllib``, no third-party HTTP
library). Stateless apart from the optional auth token, which is attached as
``X-Auth-Token`` on authenticated requests. Every request advertises the Ente Auth client
package, matching the official clients.

Calls block — run them on a worker thread, never on the GTK main loop.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["APIError", "EnteAPI", "DEFAULT_BASE_URL"]

#: Canonical production host. (``api.ente.com`` is an alias of the same backend.)
DEFAULT_BASE_URL = "https://api.ente.io"

# Ente's servers redirect nothing we call; refusing redirects keeps the token from
# ever being replayed to another host.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        return None


class APIError(Exception):
    """``kind`` ∈ {"http", "decoding", "transport"}; ``status``/``body`` are set for "http"."""

    def __init__(self, kind: str, status: int = 0, body: str = "", message: str = ""):
        self.kind = kind
        self.status = status
        self.body = body
        self.message = message
        super().__init__(self._description())

    def _description(self) -> str:
        if self.kind == "http":
            if self.status == 401:
                return "Authentication failed or expired (HTTP 401)."
            if self.status == 404:
                return "Not found (HTTP 404). The account may not exist."
            trimmed = self.body[:200]
            return f"Ente returned HTTP {self.status}." + (f" {trimmed}" if trimmed else "")
        if self.kind == "decoding":
            return f"Couldn't read the server response ({self.message})."
        return f"Network error: {self.message}"


class EnteAPI:
    CLIENT_PACKAGE = "io.ente.auth"
    TIMEOUT = 30

    def __init__(self, base_url: str = DEFAULT_BASE_URL, opener: Optional[urllib.request.OpenerDirector] = None):
        self._base_url = base_url.rstrip("/")
        self._auth_token: Optional[str] = None
        self._lock = threading.Lock()
        self._opener = opener or urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    @property
    def base_url(self) -> str:
        return self._base_url

    def set_auth_token(self, token: Optional[str]) -> None:
        with self._lock:
            self._auth_token = token

    # MARK: Requests

    def get(self, path: str, query: Sequence[Tuple[str, Optional[str]]] = (), authenticated: bool = False) -> Any:
        return self._send("GET", path, query, None, authenticated)

    def post(self, path: str, body: Any, authenticated: bool = False) -> Any:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        return self._send("POST", path, (), encoded, authenticated)

    # MARK: Query encoding

    # Everything in RFC 3986's query-allowed set except `+`, `&`, and `=`.
    _QUERY_SAFE = "!$'()*,-./:;?@_~"

    @classmethod
    def encode_query_component(cls, raw: str) -> str:
        """Percent-encodes one query name/value. ``urllib`` would leave ``+`` literal (RFC 3986
        allows it), but Ente's Go server form-decodes queries, turning ``+`` into a space —
        so ``alice+ente@example.com`` used to arrive as ``alice ente@example.com`` and 404
        during login. ``&`` and ``=`` are escaped too since we splice the encoded pairs
        together ourselves."""
        return urllib.parse.quote(raw, safe=cls._QUERY_SAFE)

    def _send(self, method: str, path: str, query: Sequence[Tuple[str, Optional[str]]],
              body: Optional[bytes], authenticated: bool) -> Any:
        url = f"{self._base_url}/{path.lstrip('/')}"
        if query:
            pairs = []
            for name, value in query:
                encoded_name = self.encode_query_component(name)
                pairs.append(encoded_name if value is None else f"{encoded_name}={self.encode_query_component(value)}")
            url += "?" + "&".join(pairs)

        headers: Dict[str, str] = {
            "Accept": "application/json",
            "X-Client-Package": self.CLIENT_PACKAGE,
            "User-Agent": "Gans (Linux)",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        with self._lock:
            token = self._auth_token
        if authenticated and token:
            headers["X-Auth-Token"] = token

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.TIMEOUT) as response:
                status = response.status
                data = response.read()
        except urllib.error.HTTPError as error:
            try:
                error_body = error.read().decode("utf-8", "replace")
            except Exception:
                error_body = ""
            raise APIError("http", status=error.code, body=error_body) from None
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as error:
            reason = getattr(error, "reason", None)
            raise APIError("transport", message=str(reason if reason is not None else error)) from None

        if not 200 <= status < 300:
            raise APIError("http", status=status, body=data.decode("utf-8", "replace"))
        if not data.strip():
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise APIError("decoding", message=str(error)) from None
